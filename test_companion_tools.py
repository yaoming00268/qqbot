"""Offline regressions for quiet controls, tagged media and game capabilities."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from chat_turns import plain_reply
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from sandbox_runtime import SandboxRuntime
from sticker_library import StickerLibrary
from test_ai_fixture import drain_runtime


class QuietOutputTests(unittest.TestCase):
    def test_game_summary_accepts_prose_and_older_json_workers(self):
        from companion_tasks import game_summary
        summary = "读到主角作出选择，记下了耐心倾听的感受；尚未通关。"
        for response in (summary, json.dumps({"summary": summary}), "```json\n" + json.dumps({"summary": summary}) + "\n```"):
            self.assertEqual(game_summary(response), summary)

    def test_screenshot_marker_and_variants_are_never_dialogue(self):
        for text in ("[保持安静]", "【保持沉默】", "（不回复）", "[不发言]", "[wait]", "[SILENT]", "[不插话]"):
            with self.subTest(text=text):
                self.assertFalse(plain_reply(text, 100).should_reply)

    def test_control_line_is_removed_without_eating_real_content(self):
        reply = plain_reply("先等等。\n[保持安静]\n完整的下一句话。", 100)
        self.assertEqual([item["text"] for item in reply.messages], ["先等等。", "完整的下一句话。"])

    def test_legacy_json_cannot_bypass_the_control_filter(self):
        self.assertFalse(plain_reply('{"should_reply":true,"reply":"[保持安静]"}', 100).should_reply)

    def test_normal_mentions_of_brackets_or_quiet_are_preserved(self):
        for text in ("请保持安静，听听音乐。", "示例里的[保持安静]不应该发出。", "[游戏剧情]", "[保持安静的少女]"):
            self.assertEqual(plain_reply(text, 100).reply_text, text)


class GameStartupFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_failure_is_preserved_without_model_input_or_game_restarts(self):
        from background_window import BackgroundWindowError
        from companion_tasks import play_game
        from renpy_bridge import RenpyBridgeError
        with tempfile.TemporaryDirectory() as directory:
            for error, attempts in ((RenpyBridgeError("RENPY_BRIDGE_RESTART_REQUIRED", "需先允许重启。"), 1),
                                    (BackgroundWindowError("BACKGROUND_WINDOW_LOADING", "游戏仍在加载。"), 5)):
                with self.subTest(code=error.code):
                    runtime = Mock(root=Path(directory))
                    runtime.begin.return_value = ("session", {"id": "game"})
                    runtime.direction.return_value = {"revision": 0}
                    runtime.observe.side_effect = error
                    runtime.session.return_value = {"state": "running"}
                    broker = Mock()
                    broker.record.return_value = {"state": "running", "result": json.dumps({"target_id": "game"})}
                    cfg = SimpleNamespace(directory=directory, max_actions=40, mode="background", vm=Mock(), startup_timeout_seconds=1)
                    console = SimpleNamespace(broker=broker, client=SimpleNamespace(
                        ai=SimpleNamespace(chat=AsyncMock()), app_config=SimpleNamespace(sandbox=cfg)))
                    with patch("sandbox_runtime.SandboxRuntime", return_value=runtime), \
                            patch("companion_tasks.asyncio.sleep", new=AsyncMock()):
                        with self.assertRaises(type(error)) as caught:
                            await play_game(console, {"id": "request"}, Mock(), {})
                    self.assertIs(caught.exception, error)
                    runtime.launch.assert_called_once()
                    self.assertEqual(runtime.observe.call_count, attempts)
                    runtime.act.assert_not_called()
                    console.client.ai.chat.assert_not_awaited()
                    self.assertEqual(broker.update.call_args.args[2]["code"], error.code)
                    self.assertEqual(broker.update.call_args.args[2]["error"], str(error))


class StickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = StickerLibrary(self.root / "stickers")
        self.image = self.root / "real.png"
        Image.new("RGB", (40, 40), "yellow").save(self.image)

    def test_real_images_are_selected_by_tags_and_keep_original_bytes(self):
        identity = self.library.import_file(self.image, ["开心", "赞同"], "表达开心和同意")
        self.assertEqual(self.library.search("很开心呀")[0]["id"], identity)
        self.assertFalse(self.library.search("难过"))
        destination, tags = self.library.copy_for_send(identity, self.root / "cache")
        self.assertEqual(destination.read_bytes(), self.image.read_bytes())
        self.assertEqual(tags, ["开心", "赞同"])

    def test_gif_animation_is_not_reencoded_to_a_still_image(self):
        gif = self.root / "animated.gif"
        Image.new("RGB", (20, 20), "red").save(gif, save_all=True,
            append_images=[Image.new("RGB", (20, 20), "blue")], duration=100, loop=0)
        identity = self.library.import_file(gif, ["开心"])
        copied, _ = self.library.copy_for_send(identity, self.root / "cache")
        self.assertEqual(copied.read_bytes(), gif.read_bytes())
        with Image.open(copied) as image:
            self.assertEqual(image.n_frames, 2)

    def test_file_extension_does_not_make_executable_bytes_a_sticker(self):
        self.image.write_bytes(b"not an image")
        with self.assertRaises((OSError, ValueError)):
            self.library.import_file(self.image, ["开心"])

    def test_no_unlabelled_stickers_or_stale_modified_assets_are_sent(self):
        with self.assertRaises(ValueError):
            self.library.import_file(self.image, [""])
        identity = self.library.import_file(self.image, ["开心"])
        with self.library.connect() as db:
            path = Path(db.execute("SELECT path FROM stickers WHERE id=?", (identity,)).fetchone()[0])
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "已变更"):
            self.library.copy_for_send(identity, self.root / "cache")

    def context(self, broker, scope="group:1"):
        return broker.issue_context({"namespace": "account", "scope": scope, "sender_id": "owner",
            "sticker_config": {"enabled": True, "directory": str(self.library.root), "cooldown_seconds": 60}}, self.root / "contexts")

    def test_per_turn_idempotency_and_group_cooldown_are_enforced(self):
        identity = self.library.import_file(self.image, ["开心"])
        broker = ConsoleBroker(self.root / "console.db")
        one, two = self.context(broker), self.context(broker)
        first = execute("sticker", one, sticker_id=identity)
        self.assertEqual(execute("sticker", one, sticker_id=identity)["id"], first["id"])
        with self.assertRaisesRegex(ValueError, "间隔"):
            execute("sticker", two, sticker_id=identity)
        self.assertEqual(execute("sticker", self.context(broker, "group:2"), sticker_id=identity)["state"], "submitted")


class CompanionUITests(unittest.TestCase):
    def test_import_config_save_and_validation_use_the_same_controls(self):
        from PySide6.QtWidgets import QApplication
        from config import AppConfig
        from ui.main_window import BotStudioGUI
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_file = root / "config.yaml"
            cfg = AppConfig()
            cfg.stickers.directory = str(root / "stickers")
            cfg.sandbox.directory = str(root / "sandbox")
            cfg.sandbox.vm.cpus = 3
            cfg.sandbox.vm.disk_path = "custom/windows.qcow2"
            cfg.save(str(config_file))
            picture = root / "sticker.gif"
            Image.new("RGB", (20, 20), "red").save(picture)
            window = BotStudioGUI(str(config_file))
            try:
                tab = window.companion_tab_widget
                tab.tags.setText("开心,赞同")
                tab.description.setText("表达开心和同意")
                with patch("ui.components.companion_tab.QFileDialog.getOpenFileNames", return_value=([str(picture)], "")):
                    tab.import_stickers()
                self.assertEqual(tab.stickers.rowCount(), 1)
                tab.refresh_learning()
                self.assertIn("最近游玩", tab.learning.toPlainText())
                tab.actions.setValue(12)
                tab.mode.setCurrentIndex(tab.mode.findData("qemu"))
                tab.autonomous.setChecked(True)
                tab.profiles.setPlainText(json.dumps({"资料": str(root)}))
                window._on_save_config(show_msg=False)
                saved = AppConfig.load(str(config_file))
                self.assertEqual(saved.sandbox.max_actions, 12)
                self.assertEqual(saved.sandbox.mode, "qemu")
                self.assertEqual(saved.sandbox.vm.cpus, 3)
                self.assertEqual(saved.sandbox.vm.disk_path, "custom/windows.qcow2")
                self.assertTrue(saved.sandbox.autonomous_enabled)
                self.assertEqual(saved.sandbox.profiles["资料"], str(root))
                before = config_file.read_bytes()
                tab.profiles.setPlainText("[]")
                with patch("ui.main_window.QMessageBox.warning") as warning:
                    window._on_save_config(show_msg=False)
                    warning.assert_called_once()
                self.assertEqual(config_file.read_bytes(), before)
            finally:
                window.close()
                app.processEvents()


class FakeGame:
    def __init__(self):
        self.events = []
        self.error = None

    def launch(self, target):
        self.events.append("launch")
        return {"pid": 100, "attached": False}

    def observe(self, target, path):
        Image.new("RGB", (40, 30), "blue").save(path)
        return {"hwnd": 7, "pid": 100, "title": "Fake game", "size": [40, 30], "rect": [0, 0, 40, 30]}

    def act(self, target, observation, action, **params):
        self.events.append((action, params))
        if self.error:
            raise self.error
        return {"posted": True, "verified": False}


class GameCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / "mygame" / "Game.exe"
        self.exe.parent.mkdir()
        self.exe.write_bytes(b"fixture not executed")
        self.context = {"namespace": "account", "scope": "group:1", "sender_id": "master", "is_master": True,
            "source_text": "游玩 " + str(self.exe)}
        self.game = FakeGame()
        self.runtime = SandboxRuntime(self.root / "sandbox", backend=self.game, max_actions=2)
        self.target = self.runtime.register(self.context, str(self.exe))
        self.sid, _ = self.runtime.begin(self.context, self.target["id"], "request")

    def test_only_owner_can_authorize_paths_from_their_current_message(self):
        for context in ({**self.context, "is_master": False}, {**self.context, "source_text": "hello"},
                        {**self.context, "sandbox_session_id": self.sid}):
            with self.subTest(context=context):
                with self.assertRaises(PermissionError):
                    self.runtime.register(context, str(self.exe))

    def test_system_command_interpreters_are_not_registered_as_games(self):
        exe = self.exe.with_name("cmd.exe")
        exe.write_bytes(b"fixture")
        with self.assertRaises(ValueError):
            self.runtime.register({**self.context, "source_text": str(exe)}, str(exe))

    def test_other_accounts_groups_and_owners_cannot_operate_game(self):
        for changed in ({"namespace": "account2"}, {"scope": "group:2"}, {"sender_id": "other"}):
            with self.assertRaises(PermissionError):
                self.runtime.observe({**self.context, **changed}, self.sid)

    def test_repeated_tool_submission_never_repeats_a_window_input(self):
        observation = self.runtime.observe(self.context, self.sid)
        one = self.runtime.act(self.context, self.sid, observation["id"], "key", key="enter")
        two = self.runtime.act(self.context, self.sid, observation["id"], "key", key="enter")
        self.assertEqual(one["state"], "posted")
        self.assertFalse(two["replayed"])
        self.assertEqual(len(self.game.events), 1)

    def test_invalid_input_does_not_consume_observation_or_action_budget(self):
        observation = self.runtime.observe(self.context, self.sid)
        for action, params in (("key", {"key": "alt+f4"}), ("click", {"x": 1, "y": .5}),
                               ("click", {"x": float("nan"), "y": .5})):
            with self.assertRaises(ValueError):
                self.runtime.act(self.context, self.sid, observation["id"], action, **params)
        self.assertFalse(self.game.events)
        self.assertEqual(self.runtime.session(self.context, self.sid)["actions"], 0)
        self.runtime.act(self.context, self.sid, observation["id"], "key", key="enter")

    def test_stop_cancels_active_and_not_yet_started_games(self):
        broker = ConsoleBroker(self.root / "console.db")
        context_file = broker.issue_context({**self.context, "sandbox_config": {"enabled": True,
            "directory": str(self.runtime.root)}}, self.root / "contexts")
        _, token = ConsoleBroker.from_context(context_file)
        receipt = broker.sandbox(token, "play", target_id=self.target["id"])
        broker.sandbox(token, "autoplay", target_id=self.target["id"], enabled=True)
        broker.sandbox(token, "stop")
        self.assertEqual(broker.record(receipt["id"])["state"], "cancel_requested")
        self.assertEqual(self.runtime.session(self.context, self.sid)["state"], "cancelled")
        self.assertFalse(self.runtime.game(self.context, self.target["id"])["autoplay"])
        with self.assertRaises(ValueError):
            self.runtime.observe(self.context, self.sid)

    def test_mcp_delivers_actual_observation_image_and_revokes_stopped_worker(self):
        import base64
        from console_tools.server import handle
        broker = ConsoleBroker(self.root / "console.db")
        metadata = {**self.context, "sandbox_config": {"enabled": True, "directory": str(self.runtime.root)}}
        main_file = broker.issue_context(metadata, self.root / "contexts")
        _, token = ConsoleBroker.from_context(main_file)
        receipt = broker.sandbox(token, "play", target_id=self.target["id"])
        broker.update(receipt["id"], "running", {"target_id": self.target["id"], "sandbox_session_id": self.sid})
        with self.runtime.connect() as db:
            db.execute("UPDATE sessions SET request_id=? WHERE id=?", (receipt["id"], self.sid))
        worker_file = broker.issue_context({**metadata, "sandbox_session_id": self.sid}, self.root / "worker")
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "qqbot_sandbox", "arguments": {"context_file": worker_file, "operation": "observe"}}}
        with patch("sandbox_runtime.SandboxRuntime.device", return_value=self.game):
            result = handle(call)["result"]
        self.assertFalse(result["isError"], result)
        image = next(item for item in result["content"] if item["type"] == "image")
        self.assertTrue(base64.b64decode(image["data"]).startswith(b"\x89PNG\r\n\x1a\n"))
        broker.sandbox(token, "stop")
        self.assertTrue(handle(call)["result"]["isError"])

    def test_game_input_is_serialized_across_worker_threads_and_released(self):
        from concurrent.futures import ThreadPoolExecutor
        from game_window import game_input_lock
        def attempt():
            with game_input_lock(timeout_ms=0):
                return True
        with ThreadPoolExecutor(max_workers=1) as pool:
            with game_input_lock():
                with self.assertRaises(ValueError):
                    pool.submit(attempt).result(timeout=2)
            self.assertTrue(pool.submit(attempt).result(timeout=2))

    def test_one_observation_cannot_drive_two_different_actions(self):
        observation = self.runtime.observe(self.context, self.sid)
        self.runtime.act(self.context, self.sid, observation["id"], "key", key="enter")
        with self.assertRaisesRegex(ValueError, "重新观察"):
            self.runtime.act(self.context, self.sid, observation["id"], "key", key="space")

    def test_missing_stale_and_tampered_screenshots_block_actions(self):
        with self.assertRaises(ValueError):
            self.runtime.act(self.context, self.sid, "fake", "click", x=.5, y=.5)
        observation = self.runtime.observe(self.context, self.sid)
        with patch("sandbox_runtime.time.time", return_value=time.time() + 100):
            with self.assertRaises(ValueError):
                self.runtime.act(self.context, self.sid, observation["id"], "key", key="enter")
        Path(observation["image_path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "截图已变更"):
            self.runtime.act(self.context, self.sid, observation["id"], "key", key="enter")

    def test_unknown_action_is_not_replayed_after_a_local_failure(self):
        self.game.error = RuntimeError("input interrupted")
        observation = self.runtime.observe(self.context, self.sid)
        with self.assertRaises(RuntimeError):
            self.runtime.act(self.context, self.sid, observation["id"], "key", key="enter")
        result = self.runtime.act(self.context, self.sid, observation["id"], "key", key="enter")
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(len(self.game.events), 1)

    def test_action_budget_is_enforced_and_cancellation_stops_further_input(self):
        for _ in range(2):
            observation = self.runtime.observe(self.context, self.sid)
            self.runtime.act(self.context, self.sid, observation["id"], "key", key="enter")
        observation = self.runtime.observe(self.context, self.sid)
        with self.assertRaisesRegex(ValueError, "额度"):
            self.runtime.act(self.context, self.sid, observation["id"], "key", key="enter")
        self.runtime.finish(self.context, self.sid, "cancelled")
        with self.assertRaisesRegex(ValueError, "已结束"):
            self.runtime.observe(self.context, self.sid)

    def test_one_game_cannot_have_two_active_players(self):
        with self.assertRaisesRegex(ValueError, "已有游玩任务"):
            self.runtime.begin(self.context, self.target["id"], "another_request")
        self.runtime.finish(self.context, self.sid, "completed")
        self.assertTrue(self.runtime.begin(self.context, self.target["id"], "new_request")[0])

    def test_fictional_notes_require_real_evidence_and_stay_in_their_group(self):
        with self.assertRaises(ValueError):
            self.runtime.note(self.context, self.sid, "虚构心得", ["invented"])
        observation = self.runtime.observe(self.context, self.sid)
        self.runtime.note(self.context, self.sid, "角色在剧情中感到开心。", [observation["id"]], "plot")
        self.runtime.note(self.context, self.sid, "我更喜欢耐心倾听的选项。", [observation["id"]], "preference")
        notes = self.runtime.notes(self.context)
        self.assertEqual(len(notes), 2)
        self.assertTrue(all(note["source"] == "fictional_game" for note in notes))
        self.assertFalse(self.runtime.notes({**self.context, "scope": "group:2"}))

    def test_work_files_do_not_escape_or_overwrite_existing_materials(self):
        with self.assertRaises(PermissionError):
            self.runtime.files(self.context, self.sid, "write", relative="../../outside.txt", content="x")
        result = self.runtime.files(self.context, self.sid, "write", relative="notes/整理.md", content="资料整理")
        self.assertEqual(Path(result["path"]).read_text(encoding="utf-8"), "资料整理")
        with self.assertRaises(ValueError):
            self.runtime.files(self.context, self.sid, "write", relative="notes/整理.md", content="覆盖")

    def test_explicit_extra_path_is_readonly_unless_owner_grants_writing(self):
        path = self.root / "materials"
        path.mkdir()
        (path / "info.txt").write_text("资料", encoding="utf-8")
        context = {**self.context, "source_text": "阅读 " + str(path)}
        grant = self.runtime.grant(context, str(path))
        self.assertEqual(self.runtime.files(context, self.sid, "read", relative="info.txt", grant_id=grant["id"])["text"], "资料")
        with self.assertRaises(PermissionError):
            self.runtime.files(context, self.sid, "write", relative="new.txt", grant_id=grant["id"], content="x")
        with self.assertRaises(PermissionError):
            self.runtime.grant(context, str(path), writable=True)


class CompanionIngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_quiet_placeholder_is_not_sent_in_normal_or_idle_chat(self):
        bot, _ = self.fixture.bot(stored_cid="fixture")
        bot.ai.chat.return_value = "[保持安静]"
        await self.fixture.receive(bot, False, "quiet", "大家在聊自己的事情")
        bot.send_group_message.assert_not_awaited()
        from test_proactive_chat import ProactiveChatTests
        other = ProactiveChatTests()
        other.fixture, other.now = self.fixture, self.fixture.baseline
        bot, _ = other.bot()
        bot.ai.chat.return_value = "[保持安静]"
        bot._proactive_chat.observe("12345678", "quiet", received_at=other.now)
        await other.tick(bot, 60)
        bot.send_action.assert_not_awaited()

    async def test_sticker_only_reply_sends_image_without_json_or_completion_text(self):
        bot, _ = self.fixture.bot(stored_cid="fixture")
        bot._is_ws_connected = lambda: True
        image = Path("meme.png")
        Image.new("RGB", (40, 40), "yellow").save(image)
        identity = StickerLibrary(bot.app_config.stickers.directory).import_file(image, ["开心"])
        async def chat(key, prompt, **options):
            context = list(bot._get_main_coordinator().requests)[-1]
            self.assertEqual(execute("stickers", context, query="开心")["stickers"][0]["id"], identity)
            execute("speech", context, operation="ready")
            execute("sticker", context, sticker_id=identity)
            return "[保持安静]"
        bot.ai.chat.side_effect = chat
        await self.fixture.receive(bot, False, "meme", "开心")
        await drain_runtime(bot)
        bot.ai.chat.assert_awaited_once()
        packets = [call.args[1] for call in bot.send_action.await_args_list if call.args[0] == "send_group_msg"]
        self.assertEqual(len(packets), 1)
        self.assertEqual([segment["type"] for segment in packets[0]["message"]], ["image"])
        bot.send_group_message.assert_not_awaited()

    async def test_wait_cancels_an_already_queued_sticker(self):
        bot, _ = self.fixture.bot(stored_cid="fixture")
        bot._is_ws_connected = lambda: True
        image = Path("meme.png")
        Image.new("RGB", (40, 40), "yellow").save(image)
        identity = StickerLibrary(bot.app_config.stickers.directory).import_file(image, ["开心"])
        async def chat(key, prompt, **options):
            context = list(bot._get_main_coordinator().requests)[-1]
            execute("speech", context, operation="ready")
            execute("sticker", context, sticker_id=identity)
            execute("wait", context)
            return ""
        bot.ai.chat.side_effect = chat
        await self.fixture.receive(bot, False, "meme", "因为我觉得")
        await drain_runtime(bot)
        bot.send_action.assert_not_awaited()

    async def test_game_learning_runs_outside_main_chat_and_cleans_completed_branch(self):
        bot, key = self.fixture.bot(stored_cid="fixture")
        bot.app_config.bot.master_id = "456"
        exe = Path("game/Game.exe").resolve()
        exe.parent.mkdir()
        exe.write_bytes(b"offline")
        backend = FakeGame()
        main_calls, work_calls = [], []
        async def chat(session, prompt, **options):
            if session.startswith("activity_summary_"):
                self.assertTrue(options["cascade_config"]["plannerConfig"]["toolConfig"]["forceDisableToolCalling"])
                return json.dumps({"summary": "阅读了虚构剧情，并记录了更喜欢倾听对方的感受。", "preferences": ["倾听对方"]}, ensure_ascii=False)
            if session.startswith("__work__:"):
                work_calls.append(session)
                root = Path(bot.app_config.sandbox.directory)
                files = list(root.glob("sessions/*/contexts/*.json"))
                context = str(files[-1])
                observed = execute("sandbox", context, operation="observe")
                execute("sandbox", context, operation="key", params={"observation_id": observed["id"], "key": "enter"})
                after = execute("sandbox", context, operation="observe")
                execute("sandbox", context, operation="note", params={"text": "我更喜欢先听对方说完。", "evidence": [after["id"]]})
                execute("sandbox", context, operation="finish", params={"text": "本轮阅读一段剧情，并记录倾听的感受；尚未通关。"})
                return "本轮阅读一段剧情，并记录倾听的感受；尚未通关。"
            main_calls.append(session)
            context = list(bot._get_main_coordinator().requests)[-1]
            receipt = execute("sandbox", context, operation="play", params={"executable": str(exe), "text": "游玩一段"})
            self.assertEqual(receipt["state"], "submitted")
            return "这就读一段剧情。"
        bot.ai.chat.side_effect = chat
        bot.ai.archive_session = AsyncMock(return_value=True)
        with patch("sandbox_runtime.SandboxRuntime.device", return_value=backend):
            await self.fixture.receive(bot, False, "game", "玩一下 " + str(exe))
            await drain_runtime(bot)
        self.assertEqual(main_calls, [key])
        self.assertEqual(len(work_calls), 1)
        bot.ai.archive_session.assert_awaited_once()
        self.assertEqual(bot.ai.archive_session.await_args.args[0], work_calls[0])
        self.assertTrue(Path(bot.ai.archive_session.await_args.args[1]).is_file())
        runtime = SandboxRuntime(bot.app_config.sandbox.directory)
        with runtime.connect() as db:
            row = db.execute("SELECT state,summary FROM sessions").fetchone()
            self.assertEqual(row["state"], "completed")
            self.assertIn("尚未通关", row["summary"])
        self.assertEqual(bot.send_group_message.await_count, 1)

    async def test_restart_keeps_notes_but_never_replays_game_input(self):
        from main_chat import MainChat
        bot, key = self.fixture.bot(stored_cid="fixture")
        console = bot._get_main_coordinator()
        exe = Path("game/Game.exe").resolve()
        exe.parent.mkdir()
        exe.write_bytes(b"offline")
        metadata = {"namespace": console.namespace, "scope": "group:12345678", "sender_id": "456",
            "is_master": True, "source_text": str(exe), "sandbox_config": {
                "enabled": True, "directory": str(Path(bot.app_config.sandbox.directory).resolve())}}
        context_file = console.broker.issue_context(metadata, Path("contexts"))
        broker, token = ConsoleBroker.from_context(context_file)
        runtime = SandboxRuntime(bot.app_config.sandbox.directory, backend=FakeGame())
        game = runtime.register(metadata, str(exe), token)
        receipt = broker.sandbox(token, "play", target_id=game["id"])
        sid, _ = runtime.begin(metadata, game["id"], receipt["id"])
        observed = runtime.observe(metadata, sid)
        runtime.note(metadata, sid, "保留已经看到的虚构剧情。", [observed["id"]])
        broker.update(receipt["id"], "running", {"target_id": game["id"], "sandbox_session_id": sid})
        MainChat(bot)
        self.assertEqual(runtime.session(metadata, sid)["state"], "interrupted")
        self.assertEqual(broker.record(receipt["id"])["state"], "failed")
        self.assertEqual(len(runtime.notes(metadata)), 1)
        self.assertFalse(runtime.backend.events)
        bot.ai.chat.assert_not_awaited()
        self.assertTrue(runtime.begin(metadata, game["id"], "new-request")[0])

    async def test_work_context_cannot_send_files_or_start_another_game(self):
        bot, _ = self.fixture.bot(stored_cid="fixture")
        console = bot._get_main_coordinator()
        context = {"namespace": console.namespace, "scope": "g", "sender_id": "456", "is_master": True,
                   "sandbox_session_id": "work", "sandbox_config": {"enabled": True, "directory": "sandbox"}}
        path = console.broker.issue_context(context, "contexts")
        with self.assertRaises(PermissionError):
            execute("send", path, files=["something"])
        with self.assertRaises(PermissionError):
            execute("sandbox", path, operation="play", params={"executable": "another.exe"})

    async def test_scheduled_play_requires_global_optin_and_target_authorization(self):
        bot, key = self.fixture.bot(stored_cid="fixture")
        console = bot._get_main_coordinator()
        exe = Path("game/Game.exe").resolve()
        exe.parent.mkdir()
        exe.write_bytes(b"offline")
        context = {"namespace": console.namespace, "scope": bot._image_scope("12345678"), "sender_id": "456", "is_master": True,
                   "session_key": key, "generation": 0, "source_text": str(exe), "sandbox_config": bot.app_config.sandbox.model_dump()}
        path = console.broker.issue_context(context, "contexts")
        broker, token = ConsoleBroker.from_context(path)
        runtime = SandboxRuntime(bot.app_config.sandbox.directory)
        game = runtime.register(context, str(exe), token)
        broker.sandbox(token, "autoplay", target_id=game["id"], enabled=True)
        await console.schedule_learning()
        self.assertFalse(console.broker.pending(console.namespace))
        bot.app_config.sandbox.autonomous_enabled = True
        await console.schedule_learning()
        pending = console.broker.pending(console.namespace)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "sandbox_play")
        self.assertTrue(json.loads(pending[0]["result"])["quiet"])
