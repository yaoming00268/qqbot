"""Offline behavioral regression checks. Never connects or sends to real QQ."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
import os
from test_ai_fixture import ConsoleAgentMock

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from antigravity_client import AntigravityClient
from config import AppConfig
from conversation_runtime import ConversationRuntime, EventContext, RuntimeStore, TextOutbox, current_event
from group_memory import GroupMemoryManager
from media_downloader import MediaError
from media_jobs import MediaService
from media_models import MediaAsset, MediaIntent, MessageEnvelope
from messaging import SendUnknown, SendUnavailable
from onebot_client import OneBotClient
from persona_manager import PersonaManager
from qq_window_capture import QQCaptureService, QQCaptureTarget, QQWindow
from visual_verification import inspect_image, viewed_image


def image_trace(path, *, tool="read_file", status="DONE", content="Showing binary file of type image/png"):
    return [{"type": "PLANNER_RESPONSE", "tool_calls": [{"name": tool, "args": {"Path": json.dumps(str(Path(path).resolve()))}}]},
            {"type": "GENERIC", "source": "MODEL", "status": status, "content": content}]


class BackgroundRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_runtime_")
        self.previous = os.getcwd()
        os.chdir(self.tmp.name)
        self.stores = []
        self.runtimes = []

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            await runtime.close()
        for store in self.stores:
            store.db.close()
        os.chdir(self.previous)
        self.tmp.cleanup()

    def store(self, namespace="test"):
        store = RuntimeStore("runtime.sqlite3", namespace)
        self.stores.append(store)
        return store

    def runtime(self, **kwargs):
        runtime = ConversationRuntime(self.store(), **kwargs)
        self.runtimes.append(runtime)
        return runtime

    def bot(self):
        cfg = AppConfig()
        cfg.bot.search_enabled = False
        cfg.bot.chat_settle_seconds = 0  # These tests exercise lane routing, not burst timing.
        cfg.bot.chat_buffer_seconds = 0
        ai = AntigravityClient(sessions_file="sessions.json")
        ai.chat = ConsoleAgentMock(return_value='{"should_reply":true,"reply":"回复","action":"none"}')
        bot = OneBotClient(cfg, ai, PersonaManager("personas.yaml"), GroupMemoryManager("memories"))
        bot.self_id = "999"
        bot.send_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "receipt"}})
        return bot

    async def test_work_runs_concurrently_while_chat_continues(self):
        runtime = self.runtime()
        entered, release, chatted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []
        async def first():
            order.append("first_started")
            entered.set()
            await release.wait()
            order.append("first_finished")
        async def second():
            order.append("second")
        async def chat():
            order.append("chat")
            chatted.set()
        event = EventContext("group:1", "m1", time.time(), owner="u1")
        runtime.submit_work(event, "first", first)
        runtime.submit_work(event, "second", second)
        await entered.wait()
        runtime.submit_chat(event, chat)
        await asyncio.wait_for(chatted.wait(), 1)
        self.assertIn("second", order)
        self.assertNotIn("first_finished", order)
        release.set()
        await runtime.work_worker
        self.assertEqual(order, ["first_started", "second", "chat", "first_finished"])

    async def test_passive_backlog_is_coalesced_but_directed_messages_and_context_survive(self):
        runtime = self.runtime(passive_backlog=1)
        entered, release = asyncio.Event(), asyncio.Event()
        replied, recorded = [], []
        event = EventContext("g", "m", time.time())
        async def block():
            entered.set()
            await release.wait()
        runtime.submit_chat(event, block)
        await entered.wait()
        async def reply(n):
            replied.append(n)
        for n in range(20):
            runtime.submit_chat(event, lambda n=n: reply(n), directed=False, record_only=lambda n=n: recorded.append(n))
        runtime.submit_chat(event, lambda: reply("directed"), directed=True)
        release.set()
        await runtime.chat_workers["g"]
        self.assertEqual(replied, [19, "directed"])
        self.assertEqual(recorded, list(range(19)))

    async def test_cancel_owner_leaves_other_users_work_running(self):
        runtime = self.runtime()
        entered = asyncio.Event()
        order = []
        async def block():
            entered.set()
            await asyncio.Event().wait()
        async def next_user():
            order.append("b")
        runtime.submit_work(EventContext("g", owner="a"), "a", block)
        runtime.submit_work(EventContext("g", owner="b"), "b", next_user)
        await entered.wait()
        runtime.cancel(owner="a")
        await runtime.work_worker
        self.assertEqual(order, ["b"])
        self.assertEqual(runtime.store.jobs("a")[0]["state"], "cancelled")

    async def test_ready_work_preserves_media_job_for_collection(self):
        from skills.base import SkillResult
        runtime = self.runtime()
        async def prepare():
            return SkillResult(False, "ready for collection", data={"state": "ready", "job_id": "media-job"})
        key = runtime.submit_work(EventContext("g", owner="a"), "prepare", prepare)
        await runtime.work_worker
        self.assertEqual(runtime.store.jobs("a")[0]["state"], "ready")
        self.assertEqual(runtime.store.result(key)["job_id"], "media-job")

    async def test_disconnected_text_survives_new_outbox_instance(self):
        store = self.store()
        send = AsyncMock(return_value={"status": "ok"})
        offline = TextOutbox(store, lambda: False, send)
        result = await offline.enqueue("g", "send_group_msg", {"group_id": 1, "message": "cached"})
        self.assertEqual(result["status"], "queued")
        send.assert_not_awaited()
        online = TextOutbox(store, lambda: True, send)
        online.wake()
        await online.worker
        self.assertEqual(send.await_count, 1)
        self.assertEqual(store.db.execute("SELECT state FROM outbox").fetchone()[0], "accepted")

    async def test_unknown_send_is_not_replayed(self):
        store = self.store()
        send = AsyncMock(side_effect=SendUnknown("unconfirmed"))
        outbox = TextOutbox(store, lambda: True, send)
        with self.assertRaises(SendUnknown):
            await outbox.enqueue("g", "send_group_msg", {})
        await outbox.worker
        outbox.wake()
        await outbox.worker
        self.assertEqual(send.await_count, 1)
        self.assertEqual(store.db.execute("SELECT state FROM outbox").fetchone()[0], "unknown")

    async def test_buffered_reply_does_not_gain_implicit_quote_when_stale(self):
        store = self.store()
        send = AsyncMock(return_value={"status": "ok"})
        outbox = TextOutbox(store, lambda: False, send)
        await outbox.enqueue("g", "send_group_msg", {"message": [{"type": "text", "data": {"text": "reply"}}]},
            source_message_id="source", received_at=time.time() - 60, quote_after=8)
        restored = TextOutbox(store, lambda: True, send)
        restored.wake()
        await restored.worker
        payload = send.call_args.args[1]
        self.assertNotIn("_outbox_metadata", payload)
        self.assertEqual(payload["message"], [{"type": "text", "data": {"text": "reply"}}])

    async def test_reset_cancels_buffered_text(self):
        store = self.store()
        send = AsyncMock()
        outbox = TextOutbox(store, lambda: False, send)
        await outbox.enqueue("g1", "send_group_msg", {})
        outbox.cancel("g1")
        outbox.connected = lambda: True
        outbox.wake()
        await outbox.worker
        send.assert_not_awaited()

    async def test_disconnect_before_submission_retains_pending_send(self):
        store = self.store()
        send = AsyncMock(side_effect=SendUnavailable("offline before send"))
        outbox = TextOutbox(store, lambda: True, send)
        result = await outbox.enqueue("g", "send_group_msg", {})
        self.assertEqual(result["status"], "queued")
        self.assertEqual(store.db.execute("SELECT state FROM outbox").fetchone()[0], "pending")
        send.side_effect = None
        send.return_value = {"status": "ok"}
        outbox.wake()
        await outbox.worker
        self.assertEqual(store.db.execute("SELECT state FROM outbox").fetchone()[0], "accepted")

    async def test_live_dispatch_routes_media_and_images_to_work_lane(self):
        bot = self.bot()
        runtime = bot._get_runtime()
        entered, release, chatted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []
        async def handle(data):
            lane = current_event.get().lane
            order.append((data["message_id"], lane))
            if data["message_id"] == "cmd":
                async def work():
                    entered.set()
                    await release.wait()
                runtime.submit_work(current_event.get(), "search", work)
            elif data["message_id"] == "chat":
                chatted.set()
        bot._handle_group_message = handle
        def event(key, content, segments=None):
            return {"post_type": "message", "message_type": "group", "group_id": 1,
                "user_id": 2, "message_id": key, "raw_message": "[CQ:at,qq=999] " + content,
                "message": segments}
        bot._queue_event(event("cmd", "/image 猫"))
        await entered.wait()
        bot._queue_event(event("image", "看看这张图", [{"type": "at", "data": {"qq": "999"}},
            {"type": "image", "data": {"file": "fixture.png"}}]))
        bot._queue_event(event("chat", "你好"))
        await asyncio.wait_for(chatted.wait(), 1)
        self.assertIn(("image", "work"), order)
        self.assertFalse(runtime.work_worker.done())
        release.set()
        await runtime.work_worker
        self.assertEqual(order[0], ("cmd", "chat"))
        self.assertCountEqual(order[1:], [("chat", "chat"), ("image", "work")])
        await bot._close_runtime()

    async def test_chat_skill_returns_queued_then_work_delivers_once(self):
        from skills.base import SkillContext, SkillResult
        bot = self.bot()
        bot._is_ws_connected = lambda: True
        runtime = bot._get_runtime()
        bot._get_media_service().emit = AsyncMock()
        ctx = bot._make_media_context(session_key="onebot_group_1", user_id="2", conversation_id="1",
            message_id="request", text="search", is_group=True)
        context = SkillContext("onebot_group_1", "1", "2", media_context=ctx, app_config=bot.app_config)
        entered, release = asyncio.Event(), asyncio.Event()
        async def execute(name, params, work_context):
            self.assertEqual(current_event.get().lane, "work")
            self.assertTrue(work_context.session_key.startswith("__work__:"))
            entered.set()
            await release.wait()
            return SkillResult(True, "verified answer", action_type="text")
        token = current_event.set(EventContext(ctx.scope, "request", time.time()))
        with patch("skills.manager.default_skill_manager.execute_skill", new=AsyncMock(side_effect=execute)):
            result = await bot._execute_skill("web_search", {"query": "topic"}, context)
            current_event.reset(token)
            self.assertEqual(result.action_type, "queued")
            self.assertFalse(result.success)
            await entered.wait()
            release.set()
            await runtime.work_worker
        emitted = [call.args[1] for call in bot.media.emit.await_args_list]
        self.assertEqual(emitted.count("任务完成，结果已发送 (´▽`)"), 1)
        self.assertNotIn("verified answer", emitted)
        self.assertEqual(bot.send_action.await_args.args[0], "upload_group_file")
        path = Path(bot.send_action.await_args.args[1]["file"])
        self.assertEqual(path.read_text(encoding="utf-8"), "verified answer")
        await bot._close_runtime()

    async def test_native_media_goes_directly_to_agent_without_early_crawl(self):
        from skills.base import SkillContext
        from skills.media_skills import SendImageSkill
        from test_agent_artifacts import task_result
        bot = self.bot()
        service = bot._get_media_service()
        service.searcher.search = AsyncMock(side_effect=AssertionError("legacy crawl called"))
        ctx = bot._make_media_context(session_key="onebot_group_1", user_id="2", conversation_id="1",
            message_id="request", text="find image", is_group=True)
        ctx.send_text = AsyncMock()
        bot.ai.chat = ConsoleAgentMock(side_effect=lambda key, prompt, **kwargs:
            task_result(prompt, response_trace=kwargs["response_trace"]))
        async def accepted(ctx, asset, before_submit=None, **kwargs):
            from messaging import SendReceipt
            before_submit()
            return SendReceipt(message_id="fixture")
        service.sender.send = AsyncMock(side_effect=accepted)
        context = SkillContext("__work__:onebot_group_1", "1", "2", media_context=ctx,
            app_config=bot.app_config, ai_client=bot.ai, media_service=service)
        result = await SendImageSkill().execute({"query": "specific character"}, context)
        self.assertTrue(result.success)
        service.searcher.search.assert_not_awaited()
        bot.ai.chat.assert_awaited_once()
        self.assertIn("specific character", bot.ai.chat.call_args.args[1])

    async def test_delayed_chat_and_work_do_not_force_quotes(self):
        bot = self.bot()
        for lane in ("chat", "work"):
            token = current_event.set(EventContext("g", "old-message", time.time() - 60, lane))
            try:
                await bot.send_group_message(1, "reply")
            finally:
                current_event.reset(token)
            self.assertEqual(bot.send_action.call_args.args[1]["message"][0], {"type": "text", "data": {"text": "reply"}})

    async def test_non_poke_notify_and_missing_or_other_targets_are_ignored(self):
        bot = self.bot()
        bot._handle_poke_event = AsyncMock()
        await bot._handle_notice_event({"notice_type": "notify", "sub_type": "honor"})
        bot._handle_poke_event.assert_not_awaited()
        original = OneBotClient._handle_poke_event.__get__(bot)
        for target in (None, "", "123"):
            await original({"target_id": target, "user_id": "1", "group_id": "2"})
        bot.ai.chat.assert_not_awaited()
        bot.send_action.assert_not_awaited()

    async def test_no_completed_image_tool_prevents_sending(self):
        Image.new("RGB", (100, 200), "red").save("image.png")
        ai = MagicMock(chat=AsyncMock(return_value='{"matched":true,"description":"cat"}'))
        with self.assertRaises(MediaError):
            await inspect_image(ai, "vision", "image.png", "match cat")
        self.assertIn("宽 100 像素，高 200 像素", ai.chat.call_args.args[1])
        self.assertFalse(viewed_image(image_trace("image.png", status="FAILED"), "image.png"))
        self.assertFalse(viewed_image(image_trace("other.png"), "image.png"))
        self.assertFalse(viewed_image(image_trace("image.png", content="some file text"), "image.png"))

    async def test_actual_binary_image_read_counts_as_viewing(self):
        Image.new("RGB", (100, 200), "red").save("image.png")
        async def read(*args, **kwargs):
            viewed = args[1].split("指定图片绝对路径：", 1)[1].split("\n", 1)[0]
            kwargs["response_trace"].extend(image_trace(viewed))
            return '{"matched":true,"description":"red rectangle"}'
        result = await inspect_image(MagicMock(chat=AsyncMock(side_effect=read)), "vision", "image.png", "identify")
        self.assertTrue(result["matched"])
        self.assertEqual(len(result["view_evidence"]["sha256"]), 64)

    async def test_wrong_image_is_rejected_and_next_candidate_is_viewed_before_send(self):
        cfg = AppConfig()
        cfg.media.search_provider = "auto"
        service = MediaService(cfg.media)
        Image.new("RGB", (300, 600), "red").save("data/cache/media/wrong.png")
        Image.new("RGB", (300, 600), "blue").save("data/cache/media/right.png")
        assets = [MediaAsset(kind="image", local_path=f"data/cache/media/{name}.png", title=name,
            source_url="https://example.com/character") for name in ("wrong", "right")]
        service.searcher.search = AsyncMock(return_value=assets)
        viewed = []
        async def vision(key, prompt, **kwargs):
            path = Path(prompt.split("指定图片绝对路径：", 1)[1].splitlines()[0])
            with Image.open(path) as picture:
                matched = picture.getpixel((0, 0)) == (0, 0, 255)
            name = "right.png" if matched else "wrong.png"
            viewed.append(name)
            kwargs["response_trace"].extend(image_trace(path))
            return json.dumps({"matched": matched, "description": name, "reason": "checked subject"})
        send = AsyncMock(return_value={"status": "ok", "data": {"message_id": "receipt"}})
        ctx = MessageEnvelope("onebot", "test", "1", "2", "original", "find blue image", AsyncMock(),
            is_group=True, send_action=send, ai_client=MagicMock(chat=AsyncMock(side_effect=vision)), ai_session_key="g")
        await service.handle(ctx, MediaIntent(operation="search_send", kind="image", query="blue image"))
        self.assertEqual(viewed, ["wrong.png", "right.png"])
        self.assertEqual(send.await_count, 1)
        message = send.call_args.args[1]["message"]
        self.assertEqual([item["type"] for item in message], ["image"])
        self.assertIn("right.png", message[0]["data"]["file"])

    async def test_archive_failure_restores_messages_and_does_not_delete_conversation(self):
        memory = GroupMemoryManager("memories")
        memory.record_session_message("g", "u", "User", "message")
        ai = MagicMock(chat=AsyncMock(return_value='{"summary":"summary"}'), archive_session=AsyncMock())
        with patch("group_memory.os.replace", side_effect=OSError("disk full")):
            result = await memory.summarize_and_save_session(ai)
        self.assertEqual(result, {})
        self.assertEqual(memory._session_messages["g"][0].content, "message")
        ai.archive_session.assert_not_awaited()

    async def test_concurrent_messages_survive_summary_and_organizer_is_retained(self):
        memory = GroupMemoryManager("memories")
        memory.record_session_message("g", "u", "User", "old")
        async def summarize(*args, **kwargs):
            memory.record_session_message("g", "u", "User", "new arrival")
            return '{"summary":"old summarized","member_portraits":{}}'
        ai = MagicMock(chat=AsyncMock(side_effect=summarize), archive_session=AsyncMock())
        saved = await memory.summarize_and_save_session(ai)
        self.assertIn("g", saved)
        self.assertEqual([m.content for m in memory._session_messages["g"]], ["new arrival"])
        ai.archive_session.assert_not_awaited()
        payload = json.loads(Path(saved["g"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"], "old summarized")
        source = Path(payload["messages_file"])
        self.assertTrue(source.is_file())
        self.assertIn('"author_id": "u"', source.read_text(encoding="utf-8"))

    async def test_failed_summary_retains_conversation_and_archives_raw_source(self):
        memory = GroupMemoryManager("memories")
        memory.record_session_message("g", "u", "User", "source")
        ai = MagicMock(chat=AsyncMock(side_effect=TimeoutError()), archive_session=AsyncMock())
        saved = await memory.summarize_and_save_session(ai)
        self.assertFalse(json.loads(Path(saved["g"]).read_text(encoding="utf-8"))["summary_complete"])
        ai.archive_session.assert_not_awaited()

    async def test_semantic_summary_is_packaged_without_deleting_organizer_or_using_file_tools(self):
        memory = GroupMemoryManager("memories")
        memory.record_session_message("g", "u", "User", "original source")
        async def agent(key, prompt, **kwargs):
            self.assertIn("original source", prompt)
            self.assertTrue(kwargs["cascade_config"]["plannerConfig"]["toolConfig"]["forceDisableToolCalling"])
            return '{"summary":"agent wrote file","key_topics":["topic"]}' 
        ai = MagicMock(chat=AsyncMock(side_effect=agent), archive_session=AsyncMock(return_value=True))
        saved = await memory.summarize_and_save_session(ai)
        payload = json.loads(Path(saved["g"]).read_text(encoding="utf-8"))
        self.assertTrue(payload["summary_complete"])
        self.assertEqual(payload["summary"], "agent wrote file")
        self.assertTrue((Path(payload["package_dir"]) / "agent_summary.json").is_file())
        ai.archive_session.assert_not_awaited()

    async def test_cancelled_archive_restores_unvisited_groups_before_new_arrivals(self):
        memory = GroupMemoryManager("memories")
        memory.record_session_message("g1", "u", "User", "old first")
        memory.record_session_message("g2", "u", "User", "old second")
        entered = asyncio.Event()
        async def agent(*args, **kwargs):
            memory.record_session_message("g2", "u", "User", "new second")
            entered.set()
            await asyncio.Event().wait()
        ai = MagicMock(chat=AsyncMock(side_effect=agent))
        task = asyncio.create_task(memory.summarize_and_save_session(ai))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([m.content for m in memory._session_messages["g1"]], ["old first"])
        self.assertEqual([m.content for m in memory._session_messages["g2"]], ["old second", "new second"])

    async def test_screenshot_uses_only_algorithmically_bound_chat_or_existing_viewer(self):
        class Backend:
            window = QQWindow(1, 7, "QQ", (0, 0, 800, 600))
            def locate_target(self, request, item, *, index=0):
                sticker = item.get("subType") == "1"
                return QQCaptureTarget(self.window, (200, 80, 700, 500), "999", "123", "group", "m1", index,
                    "qq_chat_screenshot" if sticker else "qq_image_viewer_screenshot", "verified_local_accessibility")
            def capture(self, window): return Image.new("RGB", (800, 600), "orange")
        request = {"scope": "onebot:default:group:123", "account_id": "999", "conversation_id": "123", "message_id": "m1"}
        for sticker in (True, False):
            backend = Backend()
            agent = MagicMock(chat=AsyncMock())
            service = QQCaptureService(agent, backend=backend)
            result = await service.capture_message(request, {"subType": "1" if sticker else "0"})
            self.assertTrue(Path(result["file"]).is_file())
            self.assertEqual(result["capture_mode"], "qq_chat_screenshot" if sticker else "qq_image_viewer_screenshot")
            agent.chat.assert_not_awaited()

    async def test_wrong_chat_without_search_coordinates_cannot_be_captured(self):
        backend = MagicMock()
        backend.locate_target.side_effect = MediaError("current chat cannot be verified")
        service = QQCaptureService(None, backend=backend)
        with self.assertRaises(MediaError):
            await service.capture_message({"scope": "onebot:default:group:123", "account_id": "999", "conversation_id": "123", "message_id": "m"}, {})
        backend.capture.assert_not_called()
        backend.click.assert_not_called()
        backend.search_digits.assert_not_called()

    async def test_image_interpretation_keeps_main_session(self):
        bot = self.bot()
        expected = bot._chat_session_key("onebot_group_1")
        self.assertIn("::persona:", expected)
        token = current_event.set(EventContext("g", lane="work"))
        try:
            self.assertEqual(bot._chat_session_key("onebot_group_1"), expected)
            self.assertEqual(bot._chat_options(), {"timeout": 0})
        finally:
            current_event.reset(token)
        self.assertEqual(bot._chat_session_key("onebot_group_1"), expected)

    async def test_archive_will_not_delete_missing_empty_or_foreign_sessions(self):
        ai = AntigravityClient(sessions_file="sessions.json")
        Path("archive.json").write_text('{"summary":"saved"}', encoding="utf-8")
        self.assertFalse(await ai.archive_session("unowned", "archive.json"))
        with self.assertRaises(FileNotFoundError):
            await ai.archive_session("unowned", "missing.json")
        self.assertFalse(ai._is_ready)


if __name__ == "__main__":
    unittest.main()
