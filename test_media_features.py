"""Regression and workflow tests for media delivery and review fixes.

All provider and QQ responses are fixtures. No real QQ messages are sent.
"""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import contextlib
import io
import json
import os
from test_ai_fixture import ConsoleAgentMock

from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import zipfile

from PIL import Image

from antigravity_client import AntigravityClient, SessionInvalidatedError
from bot_client import GeminiQQBot
from config import AppConfig, MediaConfig
from group_memory import GroupMemoryManager
from image_handler import analyze_image_item
from media_downloader import MediaError, PublicResolver, clean_cache, contained_path, file_signature, http_request, public_url
from media_jobs import MediaService
from media_models import MediaAsset, MessageEnvelope
from media_processor import MediaProcessor, run_process
from media_router import parse_media_intent
from media_sender import MediaSender
from media_search import MediaSearcher
from messaging import OutboundText, SendError, SendReceipt, SendUnknown
from onebot_client import OneBotClient
from persona_manager import PersonaManager, PersonaPreset
from runtime_control import RuntimeControl
from test_ai_fixture import mock_agent, drain_runtime, orchestrated_response


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Transfer/receipt tests use a vision fixture; integration checks for
        # actual image viewing live in test_background_runtime.py.
        vision = patch("visual_verification.verify_media_image", new=AsyncMock(return_value={"matched": True}))
        vision.start()
        self.addCleanup(vision.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_feature_test_")
        self.previous = os.getcwd()
        os.chdir(self.tmp.name)
        self.config = AppConfig()
        # These are explicit public-downloader transport/processing tests.
        # Native agent artifact workflows have independent coverage.
        self.config.media.search_provider = "auto"
        self.config.bot.search_enabled = False
        self.config.bot.max_reply_length = 1200
        self.personas = PersonaManager("personas.yaml")
        self.service = MediaService(self.config.media)
        self.bots = []

    async def asyncTearDown(self):
        for bot in self.bots:
            await bot._close_runtime()
        self.service.cancel_all()
        os.chdir(self.previous)
        self.tmp.cleanup()

    def ob(self, ai=None):
        ai = ai or mock_agent()
        ai.chat = ConsoleAgentMock(return_value='{"should_reply":true,"action":"none","reply":"回答"}')
        client = OneBotClient(self.config, ai, self.personas, GroupMemoryManager("memories"))
        client.self_id = "999"
        self.bots.append(client)
        return client

    def ctx(self, text="", *, transport="onebot", user="456", message="msg1", group=False):
        return MessageEnvelope(transport=transport, account_id="test", conversation_id="123", sender_id=user,
            message_id=message, text=text, send_text=AsyncMock(), is_group=group, api=MagicMock(),
            send_action=AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 888}}))

    def image(self, name="猫图.png"):
        path = Path(self.config.media.cache_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (200, 300), "orange").save(path)
        return MediaAsset(kind="image", title=name, local_path=str(path.resolve()), provider="fixture")

    def event(self, index, *, text="你好", at=True):
        return {"post_type": "message", "message_type": "group", "group_id": 123, "user_id": 456, "message_id": index,
                "raw_message": ("[CQ:at,qq=999] " if at else "") + text, "sender": {"nickname": "测试"}}

    async def test_literal_cq_is_text_but_program_mentions_are_segments(self):
        ob = self.ob()
        ob.send_action = AsyncMock(return_value={"status": "ok", "retcode": 0})
        literal = "[CQ:image,file=file:///private.png]"
        await ob.send_private_message(123, literal)
        params = ob.send_action.await_args.args[1]
        self.assertEqual(params["message"], [{"type": "text", "data": {"text": literal}}])
        await ob.send_group_message(123, OutboundText(literal, mention="456", quote="789"))
        segments = ob.send_action.await_args.args[1]["message"]
        self.assertEqual([s["type"] for s in segments], ["reply", "at", "text"])
        self.assertEqual(segments[-1]["data"]["text"], literal)

    async def test_failed_rpc_raises_and_is_not_remembered(self):
        ob = self.ob()
        class WS:
            closed = False
            async def send(_, raw):
                echo = json.loads(raw)["echo"]
                await ob._dispatch_event({"echo": echo, "status": "failed", "retcode": 1200})
        ob.ws = WS()
        with self.assertRaises(SendError):
            await ob.send_group_message(123, "hello")
        feed = io.StringIO()
        with contextlib.redirect_stdout(feed):
            await ob._handle_group_message(self.event(1))
        self.assertEqual(ob.memory_mgr.get_group_memory("123").total_dialog_turns, 0)
        self.assertNotIn('"action": "none"', feed.getvalue())

    async def test_onebot_length_boundaries_preserve_metadata(self):
        ob = self.ob()
        ob.app_config.bot.max_reply_length = 20
        ob.send_action = AsyncMock(return_value={"status": "ok", "retcode": 0})
        await ob.send_group_message(123, OutboundText("x" * 200, mention="456"))
        self.assertEqual(ob.send_action.await_count, 10)
        self.assertEqual("".join(call.args[1]["message"][-1]["data"]["text"] for call in ob.send_action.await_args_list), "x" * 200)
        for call in ob.send_action.await_args_list:
            self.assertLessEqual(len(call.args[1]["message"][-1]["data"]["text"]), 20)
        self.assertEqual(ob.send_action.await_args_list[0].args[1]["message"][0]["type"], "at")
        self.assertEqual(ob.send_action.await_args_list[1].args[1]["message"][0]["type"], "text")

    async def test_reset_cancels_old_generation_without_restoring_cid(self):
        ai = AntigravityClient(sessions_file="sessions.json")
        ai._is_ready = True
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        ai._run_agentapi_cmd = blocked
        task = asyncio.create_task(ai.chat("group_123", "old request"))
        await entered.wait()
        ai.reset_session("group_123")
        with self.assertRaises(SessionInvalidatedError):
            await asyncio.wait_for(task, 2)
        self.assertIsNone(ai.get_or_create_session("group_123").conversation_id)
        self.assertFalse(ai.get_or_create_session("group_123").lock.locked())

    async def test_model_switch_invalidates_waiting_response(self):
        ai = AntigravityClient(sessions_file="sessions.json")
        ai._is_ready = True
        ai._run_agentapi_cmd = AsyncMock(return_value={"response": {"newConversation": {"conversationId": "old"}}})
        waiting = asyncio.Event()
        async def wait_reply(*args, **kwargs):
            waiting.set()
            await asyncio.Event().wait()
        ai._wait_for_model_response = wait_reply
        task = asyncio.create_task(ai.chat("group_123", "old request"))
        await waiting.wait()
        ai.set_session_model("group_123", "pro")
        with self.assertRaises(SessionInvalidatedError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(ai.get_or_create_session("group_123").model, "pro")
        self.assertIsNone(ai.get_or_create_session("group_123").conversation_id)

    async def test_onebot_persona_reload_and_official_global_switch(self):
        self.personas.upsert_preset(PersonaPreset(name="B", description="B", prompt="New persona B"))
        ob = self.ob()
        writer = PersonaManager("personas.yaml")
        writer.set_active("B")
        os.utime("personas.yaml", (time.time() + 5, time.time() + 5))
        ob.sync_system_prompt()
        self.assertIn("New persona B", ob.ai.system_prompt)
        ob.ai.reset_all_sessions.assert_not_called()
        ai = AntigravityClient(sessions_file="official_sessions.json")
        ai.chat = ConsoleAgentMock(return_value="内部命令审核")
        bot = GeminiQQBot(self.config, ai, self.personas, memory_manager=GroupMemoryManager("official_memories"))
        self.bots.append(bot)
        ai.get_or_create_session("c2c_alice").conversation_id = "alice"
        ai.get_or_create_session("c2c_bob").conversation_id = "bob"
        self.config.bot.master_id = "alice"
        await bot._handle_message_core("c2c_alice", "/persona B", AsyncMock(), user_openid="alice")
        await drain_runtime(bot)
        self.assertEqual(ai.get_or_create_session("c2c_bob").conversation_id, "bob")
        self.assertIn("New persona B", ai.system_prompt)

    async def test_queued_conversation_replies_are_not_stopped_by_legacy_speech_limits(self):
        self.config.bot.autonomous_enabled = True
        self.config.bot.reply_probability = 0
        self.config.bot.cooldown_seconds = 60
        self.config.bot.chat_settle_seconds = 0
        self.config.bot.chat_buffer_seconds = 0
        ob = self.ob()
        started = asyncio.Event()
        release = asyncio.Event()
        async def delayed(*args, **kwargs):
            started.set()
            await release.wait()
            return '{"should_reply":true,"reply":"hi","action":"none"}'
        ob.ai.chat = ConsoleAgentMock(side_effect=delayed)
        ob.send_group_message = AsyncMock()
        try:
            ob._queue_event(self.event(1, at=False))
            await asyncio.wait_for(started.wait(), 2)
            ob._queue_event(self.event(2, at=False))
            ob._queue_event(self.event(3, at=False))
            release.set()
            await asyncio.wait_for(asyncio.gather(*list(ob._runtime.chat_workers.values())), 2)
            # Only queued follow-ups share a turn. The active reply finishes
            # before that next turn; legacy limits cannot silence either.
            self.assertEqual(ob.ai.chat.await_count, 2)
            self.assertEqual(ob.send_group_message.await_count, 2)
        finally:
            await ob._close_runtime()

    async def test_legacy_config_migrates_token_and_address(self):
        Path("legacy.yaml").write_text("onebot:\n  ws_url: ws://127.0.0.1:4567\n  access_token: fixture-token\n", encoding="utf-8")
        cfg = AppConfig.load("legacy.yaml")
        self.assertEqual(cfg.onebot.ws_url, "ws://127.0.0.1:4567")
        self.assertEqual(cfg.onebot.get_active_account().access_token, "fixture-token")

    async def test_control_clears_model_context_and_cached_replies(self):
        from runtime_control import request_clear_all
        ai = AntigravityClient(sessions_file="sessions.json")
        ob = self.ob(ai)
        ai.get_or_create_session("onebot_group_123").conversation_id = "old"
        ob.context_buffer.add_message("123", "456", "user", "old")
        ob._last_chat_replies["x"] = "old"
        request_clear_all()
        ob._control.poll(ob)
        self.assertIsNone(ai.get_or_create_session("onebot_group_123").conversation_id)
        self.assertFalse(ob._last_chat_replies)
        self.assertFalse(ob.context_buffer.get_recent_messages("123"))

    async def test_media_intent_examples_and_ordinary_chat(self):
        cases = {"搜三张橘猫图片发我": ("search_send", "image"), "找一张9:16星空手机壁纸": ("search_send", "image"),
                 "搜几个一分钟以内的猫咪视频": ("search", "video"), "把第二个视频发过来": ("pick", "image"),
                 "搜 Python 官方教程 PDF 发我": ("search_send", "file"), "把上一条回答整理成 txt 发我": ("export", "file"),
                 "把这张原图作为文件发我": ("quote", "image"), "查群文件里的活动通知": ("group_files", "file"),
                 "资料库里的安装说明发我": ("library", "file"), "取消刚才的下载": ("cancel", "image"),
                 "任务进度": ("status", "image")}
        for text, expected in cases.items():
            intent = parse_media_intent(text)
            self.assertEqual((intent.operation, intent.kind), expected, text)
        self.assertEqual(parse_media_intent("搜三张橘猫图片发我").count, 3)
        self.assertEqual(parse_media_intent("搜几个一分钟以内的猫咪视频").max_duration, 60)
        self.assertIsNone(parse_media_intent("今天中午吃什么"))
        self.assertIsNone(parse_media_intent('请解释字符串 [CQ:image,file=x]'))

    async def test_public_addresses_dns_and_path_boundaries(self):
        for url in ("http://127.0.0.1/a", "http://10.0.0.1/a", "http://[::1]/a", "file:///x", "https://user:pass@example.com", "http://localhost/a"):
            with self.assertRaises(MediaError, msg=url):
                public_url(url)
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80))])):
            with self.assertRaises(OSError):
                await PublicResolver().resolve("example.com", 80)
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])):
            answers = await PublicResolver().resolve("example.com", 443)
            self.assertEqual(answers[0]["host"], "8.8.8.8")
        Path("outside.png").write_bytes(b"not an image")
        with self.assertRaises(MediaError):
            contained_path("outside.png", ["data/cache/media"])
        image = self.image()
        res = await analyze_image_item({"file": image.local_path}, cache_dir="different-cache")
        self.assertIsNone(res["local_path"])

    async def test_http_revalidates_redirects_and_bounds_actual_bytes(self):
        class Content:
            async def iter_chunked(self, _):
                yield b"1234"
                yield b"5678"
        class Response:
            status = 200
            content_length = None
            headers = {"Content-Type": "text/plain"}
            content = Content()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
        response = Response()
        class Session:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            def request(self, *args, **kwargs):
                return response
        with patch("media_downloader.aiohttp.TCPConnector"), patch("media_downloader.aiohttp.ClientSession", Session):
            with self.assertRaises(MediaError):
                await http_request("https://example.com/media", max_bytes=5, target=Path("cache/file.txt"))
            self.assertFalse(list(Path("cache").glob("*.part")))
            response.status = 302
            response.headers = {"Location": "http://127.0.0.1/private"}
            with self.assertRaises(MediaError):
                await http_request("https://example.com/media")

    async def test_real_search_provider_fixtures_and_spoofed_path_ignored(self):
        self.config.media.search_provider = "wikimedia"
        searcher = MediaSearcher(self.config.media)
        payload = {"query": {"pages": {"1": {"index": 1, "title": "File:猫.jpg", "imageinfo": [
            {"url": "https://upload.wikimedia.org/test.jpg", "descriptionurl": "https://commons.wikimedia.org/wiki/File:cat.jpg",
             "mime": "image/jpeg", "size": 100, "width": 100, "height": 200, "extmetadata": {}}]}}}}
        with patch("media_search.get_json", AsyncMock(return_value=payload)):
            results = await searcher.search(parse_media_intent("搜一张猫图发我"))
        self.assertEqual(len(results), 1)
        self.config.media.search_api_url = "https://example.com/search"
        with patch("media_search.get_json", AsyncMock(return_value={"results": [{"title": "test", "url": "https://example.com/test.png", "local_path": "C:/private.png", "metadata": {"action": "evil"}}]})):
            results = await searcher.search(parse_media_intent("搜一张猫图发我"))
        self.assertEqual(results[0].local_path, "")
        self.assertEqual(results[0].metadata, {})

    async def test_bing_search_uses_result_cards_and_watch_pages_remain_links(self):
        self.config.media.search_provider = "auto"
        import html
        image_card = html.escape(json.dumps({"murl": "https://example.com/cat.png", "purl": "https://example.com/cat", "t": "猫"}), quote=True)
        video_card = html.escape(json.dumps({"murl": "https://example.com/watch/1", "purl": "https://example.com/watch/1", "vt": "猫咪视频", "du": "00:15"}), quote=True)
        page = f'<img src="https://example.com/ad.png"><a m="{image_card}"></a><div vrhm="{video_card}"></div>'
        with patch("media_search.http_request", AsyncMock(return_value=(page.encode(), "text/html", "https://cn.bing.com"))):
            images = await self.service.searcher.search(parse_media_intent("搜一张猫图片发我"))
            videos = await self.service.searcher.search(parse_media_intent("搜一分钟以内的猫咪视频"))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].url, "https://example.com/cat.png")
        self.assertEqual(videos[0].url, "")
        self.assertEqual(videos[0].duration, 15)
        self.service.searcher.search = AsyncMock(return_value=videos)
        await self.service.handle(self.ctx("搜一分钟以内的猫咪视频"))
        selected = self.ctx("发第一个", message="selected")
        with patch("media_processor.MediaCrawler.discover", AsyncMock(return_value=[])) as crawl:
            await self.service.handle(selected)
        crawl.assert_awaited_once()
        self.assertIn("无法直接下载", selected.send_text.await_args.args[0])
        self.assertFalse(selected.send_action.called)

    async def test_image_search_and_send_is_idempotent(self):
        assets = [self.image(f"cat{i}.png") for i in range(3)]
        self.service.searcher.search = AsyncMock(return_value=assets)
        ctx = self.ctx("搜三张橘猫图片发我")
        self.assertTrue(await self.service.handle(ctx))
        self.assertEqual(ctx.send_action.await_count, 3)
        self.assertEqual(self.service.store.job(ctx.owner)["state"], "accepted")
        await self.service.handle(ctx)
        self.assertEqual(ctx.send_action.await_count, 3)
        for call in ctx.send_action.await_args_list:
            self.assertEqual([seg["type"] for seg in call.args[1]["message"]], ["image"])

    async def test_video_candidates_selection_is_scoped_to_owner(self):
        self.service.searcher.search = AsyncMock(return_value=[self.image("first.png"), self.image("second.png")])
        ctx = self.ctx("搜几个猫咪视频")
        await self.service.handle(ctx)
        self.assertFalse(ctx.send_action.called)
        pick = self.ctx("发第二个", message="msg2")
        await self.service.handle(pick)
        self.assertEqual(pick.send_action.await_count, 1)
        other = self.ctx("发第二个", user="other", message="msg3")
        await self.service.handle(other)
        self.assertFalse(other.send_action.called)
        self.assertEqual(self.service.store.job(other.owner)["state"], "failed")

    async def test_export_utf8_csv_and_quote_as_file(self):
        ctx = self.ctx("把上一条回答整理成 txt 发我")
        ctx.last_reply = "中文回答\n" + "x" * 3000
        await self.service.handle(ctx)
        action, params = ctx.send_action.await_args.args
        self.assertEqual(action, "upload_private_file")
        self.assertEqual(Path(params["file"]).read_text(encoding="utf-8"), ctx.last_reply)
        csv_asset = self.service.processor.export("姓名,表达式\nAlice,=1+2", "csv")
        self.assertIn("'=1+2", Path(csv_asset.local_path).read_text(encoding="utf-8-sig"))
        quoted = self.ctx("把这张原图作为文件发我", message="msg2", group=True)
        quoted.quoted_images = [{"local_path": self.image().local_path}]
        await self.service.handle(quoted)
        self.assertEqual(quoted.send_action.await_args.args[0], "upload_group_file")

    async def test_library_and_current_group_files(self):
        Path("data/library").mkdir(parents=True)
        Path("data/library/安装说明.txt").write_text("安装说明", encoding="utf-8")
        ctx = self.ctx("资料库里的安装说明发我")
        await self.service.handle(ctx)
        self.assertEqual(ctx.send_action.await_args.args[0], "upload_private_file")
        self.assertEqual(ctx.send_action.await_args.args[1]["name"], "安装说明.txt")
        group = self.ctx("查群文件里的活动通知", group=True, message="msg2")
        group.send_action = AsyncMock(return_value={"status": "ok", "data": {"files": [
            {"file_name": "活动通知.pdf", "file_id": "file1", "file_size": 123, "upload_time": 10}]}})
        await self.service.handle(group)
        self.assertEqual(group.send_action.await_args.args[0], "get_group_root_files")
        self.assertEqual(group.send_action.await_args.args[1]["group_id"], 123)
        self.assertIn("活动通知.pdf", group.send_text.await_args.args[0])

    async def test_cancel_download_and_unknown_send_are_not_retried(self):
        ctx = self.ctx("搜三张橘猫图片发我")
        entered = asyncio.Event()
        async def wait_search(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.service.searcher.search = wait_search
        active = asyncio.create_task(self.service.handle(ctx))
        await entered.wait()
        await self.service.handle(self.ctx("取消刚才的下载", message="msg2"))
        await active
        self.assertEqual(self.service.store.job(ctx.owner)["state"], "cancelled")
        self.service.searcher.search = AsyncMock(return_value=[self.image()])
        unknown = self.ctx("搜一张猫图发我", message="msg3")
        unknown.send_action = AsyncMock(side_effect=SendUnknown("状态未知"))
        await self.service.handle(unknown)
        self.assertEqual(self.service.store.job(unknown.owner)["state"], "unknown")
        status = self.ctx("任务进度", message="msg4")
        await self.service.handle(status)
        self.assertFalse(status.send_action.called)

    async def test_official_upload_and_sequence_budget(self):
        asset = self.image()
        asset.mime = "image/png"
        asset.url = "https://example.com/cat.png"
        ctx = self.ctx(transport="official", group=True)
        ctx.sequence = 1
        ctx.api.post_group_file = AsyncMock(return_value={"file_info": "uploaded", "ttl": 60})
        ctx.api.post_group_message = AsyncMock(return_value={"id": "sent"})
        receipt = await self.service.sender.send(ctx, asset)
        self.assertEqual(receipt.message_id, "sent")
        self.assertFalse(ctx.api.post_group_file.await_args.kwargs["srv_send_msg"])
        self.assertEqual(ctx.api.post_group_message.await_args.kwargs["msg_seq"], 2)
        self.assertEqual(ctx.api.post_group_message.await_args.kwargs["media"], {"file_info": "uploaded"})
        ctx.sequence = 5
        with self.assertRaises(MediaError):
            ctx.allocate_sequence()
        ctx.received_at = time.time() - 301
        with self.assertRaises(MediaError):
            ctx.allocate_sequence()

    async def test_official_file_fallback_is_link_not_media_claim(self):
        ctx = self.ctx(transport="official", group=False)
        asset = MediaAsset(kind="file", title="doc.txt", local_path=self.service.processor.export("doc", "txt").local_path,
                           url="https://example.com/doc.txt", source_url="https://example.com/docs")
        receipt = await self.service.sender.send(ctx, asset)
        self.assertEqual(receipt.status, "link")
        self.assertIn("来源", ctx.send_text.await_args.args[0])
        self.assertFalse(ctx.api.post_c2c_file.called)

    async def test_python_tutorial_is_extracted_from_verified_archive(self):
        archive = Path(self.config.media.cache_dir) / "docs.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("docs-pdf/tutorial.pdf", b"%PDF-1.4\nfixture")
            zipped.writestr("../../bad.txt", "must not extract")
        asset = MediaAsset(kind="file", title="Python教程.pdf", local_path=str(archive.resolve()),
                           metadata={"extract_pdf": "tutorial.pdf"})
        result = await self.service.processor.prepare(asset)
        self.assertEqual(result.mime, "application/pdf")
        self.assertFalse(Path("bad.txt").exists())

    async def test_spoofed_mime_and_overlimit_file_rejected(self):
        path = Path(self.config.media.cache_dir) / "fake.pdf"
        path.write_bytes(b"MZ executable fixture")
        asset = MediaAsset(kind="file", local_path=str(path.resolve()), mime="application/pdf")
        with self.assertRaises(MediaError):
            await self.service.processor.prepare(asset)
        self.config.media.max_file_mb = 1
        path = path.with_suffix(".txt")
        path.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaises(MediaError):
            await self.service.processor.prepare(MediaAsset(kind="file", local_path=str(path.resolve())))

    async def test_onebot_natural_language_media_is_classified_by_main_agent(self):
        ob = self.ob()
        ob._media_service = self.service
        self.service.searcher.search = AsyncMock(return_value=[self.image()])
        ob.send_group_message = AsyncMock()
        ob.send_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 7}})
        await ob._handle_group_message(self.event(1, text="搜一张猫图发我"))
        ob.ai.chat.assert_awaited_once()
        self.assertIn("搜一张猫图发我", ob.ai.chat.await_args.args[1])
        self.assertEqual(ob.send_group_message.await_args.args[1], "回答")
        await ob._close_runtime()

    async def test_video_is_probed_and_transcoded_with_real_ffmpeg(self):
        # Local generated colour frames only. Validation harness permits exactly
        # ffmpeg/ffprobe for this test, never a network URL or shell command.
        path = Path(self.config.media.cache_dir) / "local video.webm"
        await run_process(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=160x120:d=1", "-c:v", "libvpx-vp9", str(path)], timeout=20)
        result = await self.service.processor.prepare(MediaAsset(kind="video", title="视频", local_path=str(path.resolve())), max_duration=2)
        self.assertEqual(Path(result.local_path).suffix, ".mp4")
        self.assertTrue(result.metadata["converted"])
        self.assertGreater(result.duration, 0)
        with self.assertRaises(MediaError):
            await self.service.processor.video(Path(result.local_path), 0.2)

    async def test_global_persona_requires_configured_owner_in_both_transports(self):
        self.personas.upsert_preset(PersonaPreset(name="B", description="B", prompt="new"))
        self.config.bot.master_id = "owner; another"
        ob = self.ob()
        ob.send_private_message = AsyncMock()
        original = self.personas.active_persona_name
        await ob._handle_command("persona B", user_id="intruder", is_group=False)
        await drain_runtime(ob)
        self.assertEqual(self.personas.active_persona_name, original)
        await ob._handle_command("persona B", user_id="owner", is_group=False)
        await drain_runtime(ob)
        self.assertEqual(self.personas.active_persona_name, "B")
        ai = AntigravityClient(sessions_file="official.json")
        ai.chat = ConsoleAgentMock(return_value="内部命令审核")
        bot = GeminiQQBot(self.config, ai, self.personas, memory_manager=GroupMemoryManager("mem2"))
        self.bots.append(bot)
        ai.get_or_create_session("c2c_owner").conversation_id = "unchanged"
        await bot._handle_message_core("c2c_intruder", "/persona B", AsyncMock(), user_openid="intruder")
        await drain_runtime(bot)
        self.assertEqual(ai.get_or_create_session("c2c_owner").conversation_id, "unchanged")

    async def test_private_quote_images_are_scoped_and_bot_images_are_cached(self):
        # Explicitly exercise legacy original-file quoting and its peer checks.
        # Default screenshot quoting is covered in test_qq_capture_priority.py.
        self.config.bot.qq_window_capture = False
        ob = self.ob()
        image = self.image()
        receipt = SendReceipt(message_id="original")
        original = ob._make_media_context(session_key="onebot_private_456", user_id="456", conversation_id="456")
        original.cache_sent(image, receipt)
        event = {"user_id": 456, "message_id": 2, "raw_message": "[CQ:reply,id=original]/file 把这张图作为文件发我"}
        ob._media_service = self.service
        ob._is_ws_connected = lambda: True
        ob.send_private_message = AsyncMock()
        ob.send_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {}})
        await ob._handle_private_message(event)
        try:
            await drain_runtime(ob)
        finally:
            await ob._close_runtime()
        self.assertEqual(ob.send_action.await_args.args[0], "upload_private_file")
        ob.ai.chat.assert_awaited_once()
        self.assertIn("task_result", ob.ai.chat.await_args.args[1])
        self.assertFalse(await ob._private_quoted_images(event, "other"))
        foreign = {"raw_message": "[CQ:reply,id=99]引用图片"}
        ob.send_action = AsyncMock(return_value={"status": "ok", "data": {"user_id": 456, "group_id": 999}})
        self.assertFalse(await ob._private_quoted_images(foreign, "456"))
        ob.send_action = AsyncMock(return_value={"status": "ok", "data": {"user_id": 456, "message_type": "private", "message": [{"type": "image", "data": {"url": "https://example.com/cat.png"}}]}})
        with patch("onebot_client.process_multimodal_message", AsyncMock(return_value=("图片", [{"local_path": image.local_path}]))):
            self.assertEqual((await ob._private_quoted_images(foreign, "456"))[0]["local_path"], image.local_path)

    async def test_official_reply_budget_retains_remaining_media_for_new_request(self):
        assets = [self.image(f"{i}.png") for i in range(5)]
        for asset in assets:
            asset.url = "https://example.com/" + asset.title
        self.service.searcher.search = AsyncMock(return_value=assets)
        request = self.ctx("搜五张猫图片发我", transport="official", group=True)
        request.api.post_group_file = AsyncMock(return_value={"file_info": "fixture"})
        request.api.post_group_message = AsyncMock(return_value={"id": "sent"})
        await self.service.handle(request)
        self.assertEqual(request.sequence, 5)
        self.assertEqual(request.api.post_group_message.await_count, 4)
        job = self.service.store.job(request.owner)
        self.assertEqual(job["state"], "ready")
        self.assertEqual(len(json.loads(job["assets"])), 5)
        fresh = self.ctx("任务进度", transport="official", group=True, message="fresh")
        fresh.api = request.api
        await self.service.handle(fresh)
        self.assertEqual(fresh.api.post_group_message.await_count, 5)
        self.assertEqual(self.service.store.job(request.owner)["state"], "accepted")
        await self.service.handle(self.ctx("任务进度", transport="official", group=True, message="again"))
        self.assertEqual(fresh.api.post_group_message.await_count, 5)

    async def test_expired_window_creates_no_send_ledger_and_can_be_collected(self):
        self.service.searcher.search = AsyncMock(return_value=[self.image()])
        request = self.ctx("搜一张猫图片发我", transport="official", group=True)
        request.received_at = time.time() - 301
        await self.service.handle(request)
        job = self.service.store.job(request.owner)
        self.assertEqual(job["state"], "ready")
        self.assertEqual(self.service.store.sends(job["id"]), {})
        self.assertEqual(len(json.loads(job["assets"])), 1)

    async def test_selection_inherits_duration_and_only_visible_candidates(self):
        self.service.searcher.search = AsyncMock(return_value=[self.image(f"{i}.png") for i in range(8)])
        request = self.ctx("搜一分钟以内的猫视频")
        await self.service.handle(request)
        self.assertEqual(len(json.loads(self.service.store.results(request.owner)["assets"])), 5)
        pick = self.ctx("发第二个", message="pick")
        self.service.processor.prepare = AsyncMock(return_value=self.image())
        await self.service.handle(pick)
        self.assertEqual(self.service.processor.prepare.await_args.kwargs["max_duration"], 60)
        hidden = self.ctx("发第六个", message="hidden")
        await self.service.handle(hidden)
        self.assertFalse(hidden.send_action.called)

    async def test_gateway_stream_checks_final_receipt_and_uses_remote_path(self):
        ob = self.ob()
        ob.account_config.ws_url = "ws://192.0.2.1:3001"
        asset = await self.service.processor.prepare(self.image())
        packets = []
        async def action(_, name, params, **kwargs):
            packets.append((name, params))
            if name == "upload_file_stream":
                if params.get("is_complete"):
                    return {"status": "ok", "data": {"status": "file_complete", "file_path": "/napcat/tmp/cat.png",
                        "file_size": asset.size, "sha256": asset.sha256}}
                return {"status": "ok", "data": {"status": "chunk_received"}}
            return {"status": "ok", "retcode": 0, "data": {"message_id": 88}}
        import types
        ob.send_action = types.MethodType(action, ob)
        ctx = ob._make_media_context(session_key="onebot_private_456", user_id="456", conversation_id="456")
        await self.service.sender.send(ctx, asset)
        self.assertEqual(packets[-1][1]["message"][0]["data"]["file"], "/napcat/tmp/cat.png")
        self.assertEqual(packets[0][1]["expected_sha256"], asset.sha256)
        self.assertEqual(packets[-2][1]["is_complete"], True)

    async def test_streaming_rpc_ignores_progress_until_final_and_fails_on_disconnect(self):
        ob = self.ob()
        class WS:
            closed = False
            async def send(_, raw):
                echo = json.loads(raw)["echo"]
                await ob._dispatch_event({"echo": echo, "status": "ok", "data": {"status": "merging"}})
                self.assertFalse(ob._pending_echoes[echo].done())
                await ob._dispatch_event({"echo": echo, "status": "ok", "data": {"status": "file_complete"}})
        ob.ws = WS()
        response = await ob.send_action("upload_file_stream", {}, terminal_status="file_complete")
        self.assertEqual(response["data"]["status"], "file_complete")
        self.assertFalse(ob._echo_terminal_status)
        async def drop(raw):
            ob._fail_pending()
        ob.ws.send = drop
        with self.assertRaises(SendUnknown):
            await ob.send_action("send_private_msg", {})

    async def test_recovery_and_cache_protection_do_not_cross_scope_boundaries(self):
        a = self.ctx(group=True)
        a.account_id = "account_%_1"
        b = self.ctx(group=True)
        b.account_id = "account_XX_1"
        job_a, _ = self.service.store.create_job(a.owner, a.scope, "a")
        job_b, _ = self.service.store.create_job(b.owner, b.scope, "b")
        self.service.store.recover(a.scope)
        self.assertEqual(self.service.store.job(a.owner)["state"], "failed")
        self.assertEqual(self.service.store.job(b.owner)["state"], "queued")
        asset = self.image()
        self.service.store.put_asset(a.scope, asset)
        self.service.store.put_results(a.owner, "{}", "image", 0, [asset], 60)
        self.assertIn(asset.local_path, self.service.store.active_paths())
        clean_cache(self.config.media.cache_dir, max_bytes=0, ttl_seconds=0, protected=lambda: [asset.local_path])
        self.assertTrue(Path(asset.local_path).exists())

    async def test_cancelling_model_request_invalidates_retained_cid(self):
        ai = AntigravityClient(sessions_file="cancel.json")
        ai._is_ready = True
        session = ai.get_or_create_session("x")
        session.conversation_id = "old"
        entered = asyncio.Event()
        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        ai._run_agentapi_cmd = wait
        task = asyncio.create_task(ai.chat("x", "hello"))
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertIsNone(session.conversation_id)
        self.assertEqual(session.generation, 1)

    async def test_media_gui_saves_settings_and_shows_verified_preview(self):
        from PySide6.QtWidgets import QApplication
        from gui import BotStudioGUI
        app = QApplication.instance() or QApplication([])
        window = BotStudioGUI("gui-test.yaml")
        try:
            self.assertGreaterEqual(window.tabs.indexOf(window.emotion_tab_widget), 0)
            self.assertGreaterEqual(window.tabs.indexOf(window.companion_tab_widget), 0)
            self.assertGreaterEqual(window.tabs.indexOf(window.actor_tab_widget), 0)
            window.media_numbers["max_image_mb"].setValue(7)
            window._on_save_config(show_msg=False)
            self.assertEqual(AppConfig.load("gui-test.yaml").media.max_image_mb, 7)
            asset = self.image()
            window._show_media_asset({"title": "猫图", "kind": "image", "path": asset.local_path,
                "size": 100, "duration": 0, "state": "accepted"})
            self.assertFalse(window.media_preview.pixmap().isNull())
            self.assertIn("平台已受理", window.media_preview_info.text())
        finally:
            window.close()
            app.processEvents()

    async def test_control_request_before_start_is_applied_once_per_transport(self):
        from runtime_control import request_clear_all
        request_clear_all()
        ai = AntigravityClient(sessions_file="restart.json")
        ob = self.ob(ai)
        ai.get_or_create_session("onebot_group_123").conversation_id = "previous"
        ob._control.poll(ob)
        self.assertIsNone(ai.get_or_create_session("onebot_group_123").conversation_id)
        ai.get_or_create_session("onebot_group_123").conversation_id = "new"
        replacement = RuntimeControl("onebot:" + ob.account_key)
        replacement.poll(ob)
        self.assertEqual(ai.get_or_create_session("onebot_group_123").conversation_id, "new")

    async def test_failure_before_submission_has_no_unknown_send_and_cache_is_isolated(self):
        request = self.ctx("搜一张猫图发我", transport="official", group=True)
        self.service.searcher.search = AsyncMock(return_value=[self.image()])
        await self.service.handle(request)
        job = self.service.store.job(request.owner)
        self.assertEqual(job["state"], "failed")
        self.assertFalse(self.service.store.sends(job["id"]))
        first = MediaService(self.config.media, namespace="official:app")
        second = MediaService(self.config.media, namespace="onebot:account")
        self.assertNotEqual(first.processor.cache, second.processor.cache)
        self.assertEqual(first.store.path, second.store.path)

    async def test_group_file_download_without_url_extension_uses_verified_filename(self):
        target = MediaAsset(kind="file", title="活动通知.txt", url="https://example.com/download?id=1")
        async def download(url, path, **kwargs):
            path.write_text("活动通知", encoding="utf-8")
            return "text/plain", url
        with patch("media_processor.download_url", download):
            ready = await self.service.processor.prepare(target)
        self.assertEqual(ready.mime, "text/plain")
        self.assertEqual(Path(ready.local_path).suffix, ".txt")

    async def test_search_result_export_uses_latest_candidates_instead_of_old_chat(self):
        ob = self.ob()
        ob._media_service = self.service
        ob.send_private_message = AsyncMock()
        ob.send_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {}})
        ctx = ob._make_media_context(session_key="onebot_private_456", user_id="456", conversation_id="456")
        ob._remember_chat_reply(ctx, "older answer")
        self.service.searcher.search = AsyncMock(return_value=[self.image("候选图.png")])
        await ob._route_media(ob._make_media_context(session_key="onebot_private_456", user_id="456", conversation_id="456", message_id="search", text="搜猫图片候选列表"))
        await ob._route_media(ob._make_media_context(session_key="onebot_private_456", user_id="456", conversation_id="456", message_id="export", text="把上一条回答整理成 txt"))
        exported = Path(ob.send_action.await_args.args[1]["file"]).read_text(encoding="utf-8")
        self.assertIn("候选图.png", exported)
        self.assertNotIn("older answer", exported)

    async def test_official_c2c_and_generated_file_publication(self):
        ctx = self.ctx(transport="official")
        asset = await self.service.processor.prepare(self.image())
        asset.url = "https://example.com/cat.png"
        ctx.api.post_c2c_file = AsyncMock(return_value={"file_info": "c2c-info"})
        ctx.api.post_c2c_message = AsyncMock(return_value={"id": "c2c-sent"})
        await self.service.sender.send(ctx, asset)
        self.assertEqual(ctx.api.post_c2c_file.await_args.kwargs["openid"], "123")
        self.assertEqual(ctx.api.post_c2c_message.await_args.kwargs["media"]["file_info"], "c2c-info")
        self.config.media.publish_upload_url = "https://storage.example.org/upload"
        generated = self.service.processor.export("完整中文回答", "txt")
        with patch("media_sender.http_request", AsyncMock(return_value=(b'{"url":"https://storage.example.org/file"}', "application/json", ""))):
            result = await self.service.sender.send(ctx, generated)
        self.assertEqual(result.status, "link")
        self.assertIn("https://storage.example.org/file", ctx.send_text.await_args.args[0])

    async def test_clear_before_lazy_service_load_removes_old_results_and_ready_jobs(self):
        from runtime_control import request_clear_all
        ob = self.ob()
        ctx = ob._make_media_context(session_key="onebot_private_456", user_id="456", conversation_id="456")
        self.service.store.remember_reply(ctx.owner, "old private answer")
        job, _ = self.service.store.create_job(ctx.owner, ctx.scope, "ready")
        self.service.store.update_job(job["id"], "ready")
        request_clear_all()
        ob._control.poll(ob)
        self.assertIsNone(ob.media)
        self.assertEqual(self.service.store.last_reply(ctx.owner), "")
        self.assertEqual(self.service.store.job(ctx.owner)["state"], "cancelled")

    async def test_search_backfills_failed_image_and_chinese_ratio_is_parsed(self):
        self.assertEqual(parse_media_intent("找一张9:16星空手机壁纸").aspect_ratio, "9:16")
        assets = [self.image(f"cat{i}.png") for i in range(3)]
        self.service.searcher.search = AsyncMock(return_value=assets)
        self.service.processor.prepare = AsyncMock(side_effect=[MediaError("第一项下载失败"), assets[1], assets[2]])
        ctx = self.ctx("搜两张猫图片发我")
        await self.service.handle(ctx)
        self.assertEqual(ctx.send_action.await_count, 2)
        self.assertEqual(self.service.processor.prepare.await_count, 3)


if __name__ == "__main__":
    unittest.main()
