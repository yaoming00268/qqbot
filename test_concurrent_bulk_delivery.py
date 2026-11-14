"""Offline checks for concurrent vision, console updates and bulk delivery."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import codecs
import contextlib
import io
import json
import os
from test_ai_fixture import ConsoleAgentMock

from pathlib import Path
import re
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from PIL import Image

from config import AppConfig
from conversation_runtime import EventContext
from delivery_policy import DeliveryTooLarge, MAX_FILE_BYTES
from media_jobs import MediaService
from media_models import MediaAsset, MessageEnvelope, ReplyWindowExpired
from messaging import SendUnknown
from skills.base import SkillContext
from skills.task_skills import RunTaskSkill


class BulkDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_bulk_")
        self.previous = Path.cwd()
        os.chdir(self.tmp.name)
        cfg = AppConfig().media.model_copy(update={"cache_dir": str(Path("cache").resolve()),
            "database_path": "media.sqlite3"})
        self.service = MediaService(cfg)
        self.ctx = MessageEnvelope("onebot", "fixture", "123", "456", "bulk", "交付全部文件",
            AsyncMock(), is_group=True, send_action=AsyncMock(return_value={"status": "ok", "data": {"message_id": "accepted"}}))
        self.job, _ = self.service.store.create_job(self.ctx.owner, self.ctx.scope, self.ctx.message_id)

    def tearDown(self):
        self.service.cancel_all()
        os.chdir(self.previous)
        self.tmp.cleanup()

    def assets(self, count, *, size=12, random=False):
        output = []
        for index in range(count):
            path = Path(self.service.config.cache_dir) / f"input-{index}.txt"
            path.write_bytes(os.urandom(size) if random else f"第{index}份".encode() + b"x" * size)
            output.append(MediaAsset(kind="file", title="同名文件.txt", local_path=str(path)))
        return output

    async def test_twenty_files_are_all_sent_without_a_count_limit(self):
        assets = self.assets(20)
        await self.service.send_assets(self.ctx, self.job, assets)
        self.assertEqual(self.ctx.send_action.await_count, 20)
        self.assertEqual(len(self.service.store.sends(self.job["id"])), 20)
        self.assertFalse(list(Path("cache").rglob("*.zip")))

    async def test_more_than_twenty_files_zip_without_loss_and_reuse_after_restart(self):
        assets = self.assets(127)
        prepared, skipped = await self.service.prepare_delivery(self.ctx, self.job, assets)
        self.assertFalse(skipped)
        self.assertEqual(len(prepared), 1)
        with zipfile.ZipFile(prepared[0].local_path) as archive:
            self.assertEqual(len(archive.namelist()), 127)
            self.assertEqual(len(set(archive.namelist())), 127)
            for item, name in zip(assets, archive.namelist()):
                self.assertEqual(archive.read(name), Path(item.local_path).read_bytes())
                self.assertNotIn("/", name)
        # Simulate a new service process: the SQLite plan preserves ZIP IDs.
        restarted = MediaService(self.service.config)
        with patch("delivery_policy.zip_assets", side_effect=AssertionError("repacked a durable result")):
            again, _ = await restarted.prepare_delivery(self.ctx, self.job, assets)
        self.assertEqual([item.model_dump() for item in again], [item.model_dump() for item in prepared])
        await restarted.send_assets(self.ctx, self.job, again)
        await restarted.collect_ready(self.ctx, restarted.store.job(self.ctx.owner, self.job["id"]))
        self.assertEqual(self.ctx.send_action.await_count, 1)
        self.assertFalse(restarted.processor.active_paths)

    async def test_zip_batches_stay_under_limit_and_exact_boundary_file_is_retained(self):
        assets = self.assets(21, size=1700, random=True)
        boundary = Path(assets[0].local_path)
        boundary.write_bytes(os.urandom(4096))
        with patch("delivery_policy.MAX_FILE_BYTES", 4096):
            prepared, skipped = await self.service.prepare_delivery(self.ctx, self.job, assets)
            self.assertFalse(skipped)
            self.assertGreater(len(prepared), 1)
            originals = []
            for asset in prepared:
                self.assertLessEqual(Path(asset.local_path).stat().st_size, 4096)
                if asset.provider == "delivery_zip":
                    with zipfile.ZipFile(asset.local_path) as archive:
                        self.assertIsNone(archive.testzip())
                        originals.extend(archive.read(name) for name in archive.namelist())
                else:
                    originals.append(Path(asset.local_path).read_bytes())
            self.assertCountEqual(originals, [Path(a.local_path).read_bytes() for a in assets])
            await self.service.send_assets(self.ctx, self.job, prepared)
            self.assertEqual(self.ctx.send_action.await_count, len(prepared))

    async def test_oversize_skips_only_that_file_and_checks_before_hash_or_network(self):
        self.assertEqual(MAX_FILE_BYTES, 1024 ** 3)
        assets = self.assets(2)
        Path(assets[0].local_path).write_bytes(b"a" * 1025)
        Path(assets[1].local_path).write_bytes(b"a" * 1024)
        self.ctx.wait_transport = AsyncMock()
        with patch("delivery_policy.MAX_FILE_BYTES", 1024), patch("media_processor.sha256_file") as digest:
            with self.assertRaises(DeliveryTooLarge):
                await self.service.processor.accept_artifact(assets[0], self.service.config.cache_dir)
            digest.assert_not_called()
            with self.assertRaises(DeliveryTooLarge):
                await self.service.sender.send(self.ctx, assets[0])
            self.ctx.wait_transport.assert_not_awaited()
            self.ctx.send_action.assert_not_awaited()
            ready, skipped = await self.service.prepare_delivery(self.ctx, self.job, assets)
            self.assertEqual([a.asset_id for a in ready], [assets[1].asset_id])
            self.assertEqual(len(skipped), 1)
            await self.service.send_assets(self.ctx, self.job, ready)
        self.assertEqual(self.ctx.send_action.await_count, 1)
        self.assertIn("跳过", self.service.store.job(self.ctx.owner, self.job["id"])["detail"])

    async def test_unknown_zip_send_is_never_repeated(self):
        prepared, _ = await self.service.prepare_delivery(self.ctx, self.job, self.assets(21))
        self.ctx.send_action.side_effect = SendUnknown("receipt lost")
        with self.assertRaises(SendUnknown):
            await self.service.send_assets(self.ctx, self.job, prepared)
        self.assertEqual(self.service.store.sends(self.job["id"])[prepared[0].asset_id], "unknown")
        self.ctx.send_action.side_effect = None
        with self.assertRaises(SendUnknown):
            await self.service.send_assets(self.ctx, self.job, prepared)
        self.assertEqual(self.ctx.send_action.await_count, 1)

    async def test_cancelled_zip_writer_is_joined_and_leases_released(self):
        entered, release = threading.Event(), threading.Event()
        actual = zipfile.ZipFile
        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("writer test was not released")
            return actual(*args, **kwargs)
        with patch("delivery_policy.zipfile.ZipFile", side_effect=paused):
            task = asyncio.create_task(self.service.prepare_delivery(self.ctx, self.job, self.assets(21)))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertTrue(self.service.processor.active_paths)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        self.assertFalse(self.service.processor.active_paths)
        self.assertFalse(list(Path("cache").rglob("*.zip")))
        self.assertFalse(list(Path("cache").rglob("*.part")))

    async def test_native_task_skips_large_file_and_returns_zip_to_main_before_any_send(self):
        from test_ai_fixture import mock_agent
        ai = mock_agent()
        async def work(key, prompt, **options):
            directory = Path(re.search(r"本任务输出目录：([^\n]+)", prompt)[1])
            files = []
            for i in range(23):
                path = directory / f"{i}.txt"
                path.write_bytes(b"x" * (9000 if i == 0 else 10))
                files.append({"path": str(path), "kind": "file"})
            return json.dumps({"answer": "完整结果", "files": files})
        ai.chat = ConsoleAgentMock(side_effect=work)
        self.ctx.message_id = "native-task"
        context = SkillContext("main", "123", "456", media_context=self.ctx, media_service=self.service, ai_client=ai)
        with patch("delivery_policy.MAX_FILE_BYTES", 8192):
            result = await RunTaskSkill().execute({"task": "制作全部文件", "_defer_delivery": True}, context)
        self.assertTrue(result.success)
        self.ctx.send_action.assert_not_awaited()
        self.assertEqual(len(result.data["skipped_files"]), 1)
        self.assertEqual(len(result.data["files"]), 1)
        with zipfile.ZipFile(result.data["files"][0]["local_path"]) as archive:
            self.assertEqual(len(archive.namelist()), 22)
        self.assertEqual(len(json.loads(Path(result.data["archive"]).read_text(encoding="utf-8"))["original_files"]), 22)

    async def test_official_reply_budget_preserves_rest_of_prepared_batch(self):
        assets = self.assets(20)
        official = MessageEnvelope("official", "fixture", "123", "456", "official", "files", AsyncMock(), is_group=True)
        job, _ = self.service.store.create_job(official.owner, official.scope, official.message_id)
        async def accepted(ctx, asset, **options):
            from messaging import SendReceipt
            ctx.allocate_sequence()
            options["before_submit"]()
            return SendReceipt(message_id="accepted")
        self.service.sender.send = AsyncMock(side_effect=accepted)
        prepared, _ = await self.service.prepare_delivery(official, job, assets)
        with self.assertRaises(ReplyWindowExpired):
            await self.service.send_assets(official, job, prepared)
        self.service.update(job, "ready")
        self.assertEqual(len(self.service.store.sends(job["id"])), 5)
        fresh = MessageEnvelope("official", "fixture", "123", "456", "renewed", "领取", AsyncMock(), is_group=True)
        await self.service.collect_ready(fresh, self.service.store.job(fresh.owner, job["id"]))
        self.assertEqual(self.service.store.job(fresh.owner, job["id"])["state"], "ready")
        self.assertEqual(len(self.service.store.sends(job["id"])), 10)
        self.assertEqual(self.service.sender.send.await_count, 10)


class ConcurrentVisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import test_startup_participation
        self.fixture = test_startup_participation.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_console_delivers_complete_zip_after_main_approval(self):
        from test_agent_artifacts import task_result
        from test_ai_fixture import drain_runtime
        bot, key = self.fixture.bot(stored_cid="main-preserved")
        bot._is_ws_connected = lambda: True
        async def agent(session_key, prompt, **options):
            if session_key.startswith("__work__:task:"):
                return task_result(prompt, response_trace=options["response_trace"], kind="file", count=25)
            if '"type": "task_result"' in prompt:
                bot.send_action.assert_not_awaited()
                return "审核完成"
            return '{"should_reply":false,"tasks":[{"type":"task","task":"整理全部25份文件"}]}'
        bot.ai.chat.side_effect = agent
        await self.fixture.receive(bot, False, "task-request", "帮我整理一下资料")
        await drain_runtime(bot)
        self.assertEqual(bot.ai.chat.await_count, 3)
        uploads = [call for call in bot.send_action.await_args_list if call.args[0] == "upload_group_file"]
        self.assertEqual(len(uploads), 1)
        with zipfile.ZipFile(uploads[0].args[1]["file"]) as archive:
            self.assertEqual(len(archive.namelist()), 25)
        bot.send_group_message.assert_awaited_once()

    async def test_long_search_and_two_incoming_images_run_independently_both_transports(self):
        for official in (False, True):
            bot, key = self.fixture.bot(official, stored_cid="main-preserved")
            search_entered, release_search, release_vision = asyncio.Event(), asyncio.Event(), asyncio.Event()
            vision_entered, descriptions, archived = [], [], []
            picture = Path("data/cache/images/fixture.png").resolve()
            picture.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (640, 400), "orange").save(picture)
            if not official:
                bot._qq_capture = SimpleNamespace(capture_message=AsyncMock(return_value={"file": str(picture), "capture_mode": "qq_chat_screenshot"}))
            async def agent(session_key, prompt, **options):
                if ":view:" in session_key:
                    vision_entered.append(session_key)
                    await release_vision.wait()
                    path = prompt.split("指定图片绝对路径：", 1)[1].splitlines()[0]
                    options["response_trace"].extend([
                        {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": path}}]},
                        {"type": "TOOL_RESULT", "status": "DONE", "content": "Displayed image"}])
                    return '{"description":"橙色游戏统计页与折线图"}'
                descriptions.append(prompt)
                return '{"should_reply":false}'
            bot.ai.chat.side_effect = agent
            bot.ai.archive_session = AsyncMock(side_effect=lambda branch, path: archived.append(branch) or True)
            runtime = bot._get_runtime()
            scope = f"{'official:' + bot.app_config.qq.appid if official else 'onebot:' + bot.account_key}:group:12345678"
            async def search():
                search_entered.set()
                await release_search.wait()
            runtime.submit_work(EventContext(scope, "search", owner="456"), "long search", search)
            await asyncio.wait_for(search_entered.wait(), 1)
            messages = []
            feed = io.StringIO()
            def download(url, target, timeout):
                shutil.copyfile(picture, target)
                return True
            try:
                with contextlib.redirect_stdout(feed), patch("image_handler._download_image_sync", side_effect=download):
                    for index in range(2):
                        if official:
                            message = SimpleNamespace(id=f"image-{index}", group_openid="12345678", content="有段时间没玩了",
                                author=SimpleNamespace(member_openid="456", username="Fixture user", bot=False), mentions=[],
                                attachments=[SimpleNamespace(url="https://example.com/fixture.png", filename="fixture.png")],
                                message_reference=None, reply=AsyncMock())
                            bot._queue_official_group(message, False)
                        else:
                            message = {"post_type": "message", "message_type": "group", "group_id": 12345678,
                                "user_id": 456, "message_id": f"image-{index}", "raw_message": "有段时间没玩了",
                                "message": [{"type": "text", "data": {"text": "有段时间没玩了"}},
                                    {"type": "image", "data": {"file": "fixture", "subType": str(index)}}], "sender": {"nickname": "Fixture user"}}
                            bot._queue_event(message)
                        messages.append(message)
                    async def wait_for_vision():
                        while len(vision_entered) < 2:
                            await asyncio.sleep(.01)
                    await asyncio.wait_for(wait_for_vision(), 2)
                    initial = [json.loads(line[len("[FEED_MSG] "):]) for line in feed.getvalue().splitlines() if line.startswith("[FEED_MSG] ")]
                    self.assertEqual({item["message_id"] for item in initial}, {"image-0", "image-1"})
                    self.assertTrue(all("识别中" in item["content"] for item in initial))
                    await self.fixture.receive(bot, official, "ordinary", "群聊仍然正常")
                    self.assertTrue(any("群聊仍然正常" in prompt for prompt in descriptions))
                    release_vision.set()
                    async def wait_for_archives():
                        while len(archived) < 2 or len(descriptions) < 3:
                            await asyncio.sleep(.01)
                    await asyncio.wait_for(wait_for_archives(), 2)
                    self.assertFalse(release_search.is_set())
                    self.assertEqual(len(set(vision_entered)), 2)
                    self.assertTrue(any("橙色游戏统计页" in prompt for prompt in descriptions))
                    self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "main-preserved")
                    for index in range(2):
                        self.assertTrue(bot._get_image_context_store().get(bot._image_scope("12345678"), f"image-{index}"))
                    final = [json.loads(line[len("[FEED_MSG] "):]) for line in feed.getvalue().splitlines() if line.startswith("[FEED_MSG] ")]
                    self.assertEqual({item["message_id"] for item in final if "橙色游戏统计页" in item["content"]}, {"image-0", "image-1"})
            finally:
                release_vision.set()
                release_search.set()
                await asyncio.wait_for(runtime.work_worker, 3)


class MonitorStreamTests(unittest.TestCase):
    def test_byte_split_unicode_updates_same_message_and_keeps_full_content(self):
        from PySide6.QtWidgets import QApplication
        from ui.main_window import BotStudioGUI
        from ui.components.monitor_sidebar import GroupChatMonitorWidget
        app = QApplication.instance() or QApplication([])
        monitor = GroupChatMonitorWidget()
        sink = SimpleNamespace(_stdout_buffer="", _stdout_decoder=codecs.getincrementaldecoder("utf-8")(errors="replace"),
            sidebar_monitor=monitor, console_tab_widget=SimpleNamespace(append_log=lambda text: None))
        content = "中文🙂 &amp; <图片>\n" + "完整识别结果" * 400
        events = [
            {"group_id": "1", "message_id": "img", "sender": "用户<甲>", "content": "识别中", "time": "10:00:00"},
            {"group_id": "1", "message_id": "other", "sender": "乙", "content": "继续聊天"},
            {"group_id": "1", "message_id": "img", "sender": "用户<甲>", "content": content, "time": "10:00:09"},
        ]
        raw = "\n".join("[FEED_MSG] " + json.dumps(row, ensure_ascii=False) for row in events).encode()
        for byte in raw:
            BotStudioGUI._consume_process_output(sink, bytes([byte]))
        BotStudioGUI._consume_process_output(sink, b"", final=True)
        self.assertEqual(len(monitor._all_messages), 2)
        self.assertEqual(monitor._all_messages[0]["content"], content)
        self.assertEqual(monitor._all_messages[0]["time"], "10:00:00")
        self.assertEqual(monitor._all_messages[1]["message_id"], "other")
        rendered = monitor.feed_browser.toPlainText()
        self.assertIn(content, rendered)
        self.assertIn("用户<甲>", rendered)
        self.assertNotIn("识别中", rendered)
        monitor.close()


if __name__ == "__main__":
    unittest.main()
