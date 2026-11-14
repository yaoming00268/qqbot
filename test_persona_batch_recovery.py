"""User-visible isolation, timed batches, explicit quoting and blocked-page recovery."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import asyncio
import json
from test_ai_fixture import prompt_object
from pathlib import Path
import re
import time
import unittest
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

from antigravity_client import AntigravityClient, ContentBlockedError
from console_tools.cli import execute
from conversation_runtime import ConversationRuntime, EventContext, RuntimeStore, current_event
from onebot_client import OneBotClient
from persona_isolation import persona_scope
from sandbox_runtime import ActivityDirectionChanged, SandboxRuntime
from test_browser_requests import BrowserFrameBackend
from test_ai_fixture import drain_runtime
import test_startup_participation as fixtures

QT_APP = None


class PersonaAndBatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_two_presets_have_separate_cids_files_and_buffers_and_restore_on_switch_back(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, key_a = self.fixture.bot(official, stored_cid="persona-a")
                bot.app_config.bot.memory_enabled = True
                name_a = bot.personas.active_persona_name
                name_b = next(name for name in bot.personas.presets if name != name_a)
                old = bot.memory_mgr.manager("12345678")
                legacy = Path(bot.memory_mgr.storage_dir) / "group_12345678.json"
                legacy.write_text('{"summary":"混合旧身份不得自动载入"}', encoding="utf-8")
                await self.fixture.receive(bot, official, "a1", "我只告诉角色甲：苹果")
                bot.personas.set_active(name_b)
                key_b = bot._chat_session_key("group_12345678" if official else "onebot_group_12345678")
                self.assertNotEqual(key_a, key_b)
                self.assertIsNone(bot.ai.get_or_create_session(key_b).conversation_id)
                await self.fixture.receive(bot, official, "b1", "我只告诉角色乙：香蕉", elapsed=1)
                new = bot.memory_mgr.manager("12345678")
                self.assertNotEqual(old.storage_dir, new.storage_dir)
                self.assertIn("香蕉", str(new._session_messages))
                self.assertNotIn("苹果", str(new._session_messages))
                self.assertNotIn("苹果", bot.ai.chat.await_args.args[1])
                self.assertNotIn("混合旧身份", bot.ai.chat.await_args.args[1])
                self.assertEqual(bot.ai.chat.await_args.args[0], key_b)
                bot.ai.get_or_create_session(key_b).conversation_id = "persona-b"
                bot.personas.set_active(name_a)
                await self.fixture.receive(bot, official, "a2", "还记得我刚说的吗", elapsed=2)
                self.assertEqual(bot.ai.chat.await_args.args[0], key_a)
                self.assertEqual(bot.ai.get_or_create_session(key_a).conversation_id, "persona-a")
                self.assertEqual(bot.ai.get_or_create_session(key_b).conversation_id, "persona-b")
                recent = bot.context_buffer.format_for_prompt("12345678")
                self.assertIn("苹果", recent)
                self.assertNotIn("香蕉", recent)
                self.assertTrue(legacy.is_file())

    async def test_switch_during_await_keeps_old_records_scoped_and_suppresses_old_reply(self):
        for official in (False, True):
            bot, key = self.fixture.bot(official, stored_cid="a")
            bot.app_config.bot.memory_enabled = True
            entered, release = asyncio.Event(), asyncio.Event()
            async def agent(*args, **kwargs):
                entered.set()
                await release.wait()
                return "旧人设的回复不应发到当前群聊"
            bot.ai.chat.side_effect = agent
            pending = asyncio.create_task(self.fixture.receive(bot, official, "old", "先给旧角色留个消息"))
            await entered.wait()
            bot.personas.set_active(next(n for n in bot.personas.presets if n != bot.personas.active_persona_name))
            release.set()
            message = await pending
            self.fixture.assert_not_sent(bot, official, message)
            self.assertFalse(bot.memory_mgr.manager("12345678")._session_messages)
            self.assertNotIn("先给旧角色", bot.context_buffer.format_for_prompt("12345678"))

    def ingress(self, mid, text, user=456):
        return {"post_type": "message", "message_type": "group", "group_id": 12345678,
            "user_id": user, "message_id": mid, "raw_message": text, "sender": {"nickname": f"成员{user}"}}

    async def test_fixed_window_collects_different_speakers_and_cli_selects_actual_quote(self):
        bot, _ = self.fixture.bot(stored_cid="a")
        bot.app_config.bot.chat_buffer_seconds = .4
        bot.app_config.bot.memory_enabled = True
        bot.send_group_message = OneBotClient.send_group_message.__get__(bot)
        bot._is_ws_connected = lambda: True
        prompts = []
        async def agent(key, prompt, **kwargs):
            prompts.append(prompt)
            context = re.search(r'"console_context_file":\s*"([^"]+)"', prompt)[1].replace('\\\\', '\\')
            view = execute("messages", context)
            self.assertEqual([row["id"] for row in view["messages"]], ["first", "second", "third"])
            self.assertTrue(all("label" not in row for row in view["messages"]))
            execute("speech", context, operation="ready")
            execute("message", context, text="把三条一起读完后回复。", reply_to="first")
            with self.assertRaises(ValueError):
                execute("message", context, text="引用无效目标", reply_to="someone-else")
            return "把三条一起读完后回复。"
        bot.ai.chat.side_effect = agent
        bot._queue_event(self.ingress("first", "第一段", 456))
        await asyncio.sleep(.02)
        bot._queue_event(self.ingress("second", "补充第二段", 789))
        await asyncio.sleep(.02)
        bot._queue_event(self.ingress("third", "最后一个问题", 456))
        bot.ai.chat.assert_not_awaited()
        await drain_runtime(bot)
        self.assertEqual(len(prompts), 1)
        packet = next(call.args[1] for call in bot.send_action.await_args_list if call.args[0] == "send_group_msg")
        self.assertEqual(packet["message"][0], {"type": "reply", "data": {"id": "first"}})
        rows = bot.memory_mgr.manager("12345678")._session_messages["12345678"]
        self.assertEqual([row.content for row in rows if not row.is_bot], ["第一段", "补充第二段", "最后一个问题"])

    async def test_new_messages_wait_a_whole_window_after_previous_reply_completes(self):
        store = RuntimeStore(Path("timing.sqlite3"), "test")
        runtime = ConversationRuntime(store, buffer_seconds=.06)
        gate, first, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
        batches, times = [], []
        async def run():
            batches.append([row["id"] for row in current_event.get().batch])
            times.append(time.monotonic())
            if len(batches) == 1:
                first.set()
                await gate.wait()
            else:
                second.set()
        try:
            runtime.submit_chat(EventContext("g", "1", owner="g:a"), run, coalesce=True)
            await first.wait()
            runtime.submit_chat(EventContext("g", "2", owner="g:b"), run, coalesce=True)
            await asyncio.sleep(.08)
            self.assertFalse(second.is_set())
            completed = time.monotonic()
            gate.set()
            await asyncio.sleep(.02)
            self.assertFalse(second.is_set())
            await asyncio.wait_for(second.wait(), 1)
            self.assertGreaterEqual(times[1] - completed, .055)
            self.assertEqual(batches, [["1"], ["2"]])
        finally:
            await runtime.close()
            store.db.close()

    async def test_official_batch_can_reply_to_earlier_directed_message(self):
        bot, _ = self.fixture.bot(True, stored_cid="official")
        bot.app_config.bot.chat_buffer_seconds = .05
        bot.app_config.bot.autonomous_enabled = False
        def message(mid, user, content):
            return SimpleNamespace(id=mid, group_openid="12345678", content=content,
                author=SimpleNamespace(member_openid=user, username=user, bot=False), mentions=[],
                attachments=None, message_reference=None, reply=AsyncMock(return_value={"id": "accepted"}))
        first = message("first", "456", "请一起看我接下来的消息")
        last = message("last", "789", "补充一下细节")
        async def agent(key, prompt, **kwargs):
            file = re.search(r'"console_context_file":\s*"([^"]+)"', prompt)[1].replace('\\\\', '\\')
            execute("speech", file, operation="ready")
            execute("message", file, text="两条消息一起回复。", reply_to="first")
            return "两条消息一起回复。"
        bot.ai.chat.side_effect = agent
        bot._queue_official_group(first, True)
        bot._queue_official_group(last, False)
        await drain_runtime(bot)
        bot.ai.chat.assert_awaited_once()
        self.assertEqual(prompt_object(bot.ai.chat.await_args.args[1], "turn")["turn"]["messages"][0]["id"], "first")
        first.reply.assert_not_awaited()
        self.assertEqual(last.reply.await_args.kwargs["message_reference"]["message_id"], "first")

    async def test_cached_image_is_recognized_before_one_combined_main_turn(self):
        import test_image_handoff as images
        fixture = images.ImageHandoffTests()
        fixture.setUp()
        try:
            bot = fixture.onebot()
            bot.app_config.bot.chat_buffer_seconds = .15
            bot._queue_event(fixture.event("original-1", "这张图里的统计", image=True, at=True))
            bot._queue_event(fixture.event("followup", "结合图片讲一下这个统计"))
            await fixture.drain(bot)
            main = fixture.calls(bot.ai, "onebot_group_123")
            self.assertEqual(len(main), 1)
            self.assertIn(images.DESCRIPTION, main[0].args[1])
            self.assertEqual(fixture.timeline.count("vision"), 1)
            self.assertIn("original-1", [row["id"] for row in prompt_object(main[0].args[1], "turn")["turn"]["messages"]])
        finally:
            await fixture.asyncTearDown()

    async def test_quoting_owner_message_does_not_grant_owner_rights_to_last_speaker(self):
        bot, _ = self.fixture.bot(stored_cid="a")
        bot.app_config.bot.master_id = "456"
        bot.app_config.bot.chat_buffer_seconds = .02
        async def agent(key, prompt, **kwargs):
            context = re.search(r'"console_context_file":\s*"([^"]+)"', prompt)[1].replace('\\\\', '\\')
            execute("speech", context, operation="ready")
            execute("message", context, text="回应主人消息", reply_to="owner")
            with self.assertRaises(PermissionError):
                execute("sandbox", context, operation="restart", params={"session_id": "foreign"})
            return ""
        bot.ai.chat.side_effect = agent
        bot._queue_event(self.ingress("owner", "普通问题", 456))
        bot._queue_event(self.ingress("guest", "接着聊", 789))
        await drain_runtime(bot)
        bot.ai.chat.assert_awaited_once()

    async def test_shared_memory_root_still_isolates_accounts_with_the_same_preset_and_group(self):
        from group_memory import GroupMemoryManager
        from persona_isolation import PersonaMemory
        bot, _ = self.fixture.bot()
        root = GroupMemoryManager("shared-memories")
        other = SimpleNamespace(account_key="other-account", send_action=None, app_config=bot.app_config, personas=bot.personas)
        first, second = PersonaMemory(bot, root), PersonaMemory(other, root)
        first.record_session_message("123", "owner", "主人", "只属于账号一的记忆")
        self.assertNotEqual(first.manager("123").storage_dir, second.manager("123").storage_dir)
        self.assertFalse(second.manager("123")._session_messages)


class RecoveryWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_pause_before_first_model_turn_preserves_session_without_failure(self):
        bot, _ = self.fixture.bot(stored_cid="parent")
        bot.app_config.bot.master_id = "456"
        bot.app_config.sandbox.directory = "early-pause-sandbox"
        exe = Path("browser/chrome.exe").resolve()
        exe.parent.mkdir()
        exe.touch()
        backend = BrowserFrameBackend()
        backend.launch = lambda target: {"pid": 77}
        original_observe = SandboxRuntime.observe
        request_id = ""

        def observe_and_pause(runtime, metadata, sid):
            observation = original_observe(runtime, metadata, sid)
            runtime.request_end(metadata, sid, "pause", "窗口就绪后立即暂停，保留当前页面")
            return observation

        async def agent(key, prompt, **options):
            nonlocal request_id
            self.assertFalse(key.startswith("__work__:"))
            file = re.search(r'"console_context_file":\s*"([^"]+)"', prompt)[1].replace('\\\\', '\\')
            request_id = execute("sandbox", file, operation="use", params={"executable": str(exe)})["id"]
            return ""

        bot.ai.chat.side_effect = agent
        with patch("sandbox_runtime.SandboxRuntime.device", return_value=backend), \
                patch.object(SandboxRuntime, "observe", observe_and_pause):
            await self.fixture.receive(bot, False, "start", "使用 " + str(exe))
            await drain_runtime(bot)
        row = bot._get_main_coordinator().broker.record(request_id)
        self.assertEqual(row["state"], "paused")
        self.assertIn("立即暂停", json.loads(row["result"])["summary"])

    async def test_content_block_skips_page_resets_cid_and_rejects_old_worker_inputs(self):
        bot, _ = self.fixture.bot(stored_cid="parent")
        bot.app_config.bot.master_id = "456"
        bot.app_config.sandbox.directory = "recovery-sandbox"
        exe = Path("browser/chrome.exe").resolve()
        exe.parent.mkdir()
        exe.touch()
        backend = BrowserFrameBackend()
        launches = []
        backend.launch = lambda target: launches.append(target["id"]) or {"pid": 77}
        worker_calls, original_context, original_frame = [], [], []
        request_id = ""
        async def agent(key, prompt, **options):
            nonlocal request_id
            context = re.search(r'"console_context_file":\s*"([^"]+)"', prompt)
            file = context[1].replace('\\\\', '\\') if context else re.search(r"当前上下文文件：([^\n]+)", prompt)[1]
            if key.startswith("__work__:task:sandbox:"):
                worker_calls.append(prompt)
                if len(worker_calls) == 1:
                    bot.ai.get_or_create_session(key).conversation_id = "filtered-cid"
                    original_context.append(file)
                    original_frame.append(execute("desktop", file, operation="screenshot"))
                    raise ContentBlockedError("service content filter")
                self.assertIsNone(bot.ai.get_or_create_session(key).conversation_id)
                self.assertNotIn("filtered-cid", prompt)
                self.assertNotIn("previous-page", prompt)
                self.assertIn('"page_url": "https://example.com/"', prompt)
                with self.assertRaises(ActivityDirectionChanged):
                    execute("desktop", original_context[0], operation="key", params={
                        "observation_id": original_frame[0]["observation_id"], "key": "enter"})
                execute("sandbox", file, operation="pause", params={"text": "验证完毕"})
                return "恢复成功，已到其他入口。"
            if not request_id:
                request_id = execute("sandbox", file, operation="use", params={"executable": str(exe)})["id"]
            return ""
        bot.ai.chat.side_effect = agent
        with patch("sandbox_runtime.SandboxRuntime.device", return_value=backend):
            await self.fixture.receive(bot, False, "start", "使用 " + str(exe))
            await drain_runtime(bot)
            row = bot._get_main_coordinator().broker.record(request_id)
            self.assertEqual(row["state"], "paused")
            self.assertEqual(len(worker_calls), 2)
            self.assertEqual(len(launches), 1)
            self.assertEqual(backend.page_url, "https://example.com/")
            self.assertEqual(json.loads(row["result"])["recovery"]["state"], "applied")

    async def test_repeat_filter_pauses_and_manual_skip_resumes_same_session(self):
        bot, _ = self.fixture.bot(stored_cid="parent")
        bot.app_config.bot.master_id = "456"
        bot.app_config.sandbox.directory = "repeat-sandbox"
        exe = Path("browser/chrome.exe").resolve()
        exe.parent.mkdir()
        exe.touch()
        backend = BrowserFrameBackend()
        backend.launch = lambda target: {"pid": 77}
        request_id, owner_file, calls = "", "", 0
        async def agent(key, prompt, **options):
            nonlocal request_id, owner_file, calls
            if key.startswith("__work__:task:sandbox:"):
                calls += 1
                if calls <= 2:
                    raise ContentBlockedError("safety filter")
                file = re.search(r"当前上下文文件：([^\n]+)", prompt)[1]
                execute("sandbox", file, operation="pause", params={"text": "已从新的入口恢复"})
                return "新入口可用。"
            owner_file = re.search(r'"console_context_file":\s*"([^"]+)"', prompt)[1].replace('\\\\', '\\')
            request_id = execute("sandbox", owner_file, operation="use", params={"executable": str(exe)})["id"]
            return ""
        bot.ai.chat.side_effect = agent
        with patch("sandbox_runtime.SandboxRuntime.device", return_value=backend):
            await self.fixture.receive(bot, False, "start", "使用 " + str(exe))
            await drain_runtime(bot)
            broker = bot._get_main_coordinator().broker
            record = broker.record(request_id)
            sid = json.loads(record["result"])["sandbox_session_id"]
            self.assertEqual(record["state"], "paused")
            self.assertEqual(calls, 2)
            # Recovery remains usable after the original chat's 24-hour
            # capability expires; the GUI supplies a new bound controller.
            with broker.connect() as db:
                db.execute("UPDATE contexts SET expires=0 WHERE token=?", (record["token"],))
            from PySide6.QtWidgets import QApplication
            from ui.components.companion_tab import CompanionTabWidget
            global QT_APP
            QT_APP = QApplication.instance() or QApplication([])
            widget = CompanionTabWidget(bot.app_config)
            widget.refresh_learning()
            widget.recovery_url.setText("https://example.net/")
            widget.recover_activity("skip")
            request = json.loads(widget.recovery_result.text())
            self.assertEqual(request["state"], "recovery_requested")
            await bot._get_main_coordinator().poll()
            await drain_runtime(bot)
            self.assertEqual(calls, 3)
            self.assertEqual(json.loads(broker.record(request_id)["result"])["sandbox_session_id"], sid)
            self.assertEqual(backend.page_url, "https://example.net/")
            with self.assertRaisesRegex(ValueError, "过期"):
                execute("tasks", owner_file)
            console = bot._get_main_coordinator()
            key = bot._chat_session_key("onebot_group_12345678")
            feedback = console.activity_feedback(key, console.contexts[key].scope, True)
            controller = feedback[0]["controller_context_file"]
            execute("sandbox", controller, operation="restart", params={"session_id": sid, "url": "https://example.org/"})
            await bot._get_main_coordinator().poll()
            await drain_runtime(bot)
            self.assertEqual(calls, 4)
            self.assertEqual(backend.page_url, "https://example.org/")
            widget.close()


class BlockClassificationTests(unittest.TestCase):
    def test_only_explicit_service_blocks_are_classified(self):
        for text in ("Content filtered", "SAFETY filter blocked generation", "finish_reason: SAFETY", "触发安全拦截"):
            self.assertTrue(AntigravityClient._content_is_blocked(text), text)
        for text in ("connection refused", "timeout", "模型回复为空", "ordinary content", "Safety tips"):
            self.assertFalse(AntigravityClient._content_is_blocked(text), text)
        with self.assertRaises(ContentBlockedError):
            AntigravityClient._check_agentapi_response(["send-message", "cid"], {"error": {"code": "CONTENT_FILTER", "message": "blocked"}})


class ScopedActivityTests(unittest.TestCase):
    from test_unlimited_activity import UnlimitedDesktopCapabilityTests
    setUp = UnlimitedDesktopCapabilityTests.setUp

    def test_other_persona_cannot_read_notes_control_software_or_list_old_tasks(self):
        first = {**self.metadata, "session_key": "onebot_group_1::persona:first"}
        second = {**self.metadata, "session_key": "onebot_group_1::persona:second"}
        file_a = self.broker.issue_context(first, self.root / "persona-a")
        file_b = self.broker.issue_context(second, self.root / "persona-b")
        game = self.runtime.register(first, str(self.exe), kind="software")
        broker, token_a = self.broker.from_context(file_a)
        request = broker._enqueue(token_a, first, "sandbox_play", "阅读", {"target_id": game["id"]})
        sid, _ = self.runtime.begin(first, game["id"], request["id"])
        observation = self.runtime.observe(first, sid)
        self.runtime.note(first, sid, "只属于人设甲的阅读记录", [observation["id"]], "observation")
        self.assertTrue(self.runtime.notes(first))
        self.assertEqual(self.runtime.notes(second), [])
        self.assertEqual(execute("sandbox", file_b, operation="apps")["apps"], [])
        self.assertEqual(execute("tasks", file_b)["tasks"], [])
        with self.assertRaises(PermissionError):
            self.runtime.observe(second, sid)
        with self.assertRaises(PermissionError):
            _, token_b = broker.from_context(file_b)
            broker.status(token_b, request["id"])


class BlockTranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_transcript_filter_raises_typed_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ai = AntigravityClient(sessions_file=str(root / "sessions.json"))
            ai._brain_dir = str(root / "brain")
            path = root / "brain/cid/.system_generated/logs/transcript.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"step_index": 0, "type": "SYSTEM_ERROR", "status": "ERROR",
                "error": "generation blocked by safety filter"}) + "\n", encoding="utf-8")
            with self.assertRaises(ContentBlockedError):
                await ai._wait_for_model_response("cid", expected_step_min=0, timeout=1)
