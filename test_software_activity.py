"""Software use shares the observed input loop; idle chat remains silent."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import asyncio
import ctypes
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from browser_control import BrowserBridge, BrowserError, checked_url
from chat_turns import plain_reply
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute, main
from console_tools.server import handle, tools_schema
from sandbox_runtime import SandboxRuntime
from test_desktop_control import FrameBackend
from test_background_window import FakeBackgroundApi
import test_startup_participation as fixtures


class QuietPresetTests(unittest.TestCase):
    def test_idle_prose_and_legacy_wrappers_are_suppressed(self):
        for text in ("等待下一条群消息。", "保持在线并等待下一条群消息。", "等待下一条消息。", "继续待机。", "本轮不回复",
                     "【等待下一条群消息。】"):
            with self.subTest(text=text):
                self.assertFalse(plain_reply(text, 1200).should_reply)
                self.assertFalse(plain_reply(json.dumps({"should_reply": True, "reply": text}), 1200).should_reply)
        self.assertEqual(plain_reply("这个配乐不错。\n等待下一条群消息。", 1200).reply_text, "这个配乐不错。")

    def test_literal_examples_and_ordinary_conversation_survive(self):
        for text in ("你可以等待下一条群消息。", "“等待下一条群消息。”这句话不该发出来。", "先等等。",
                     "```text\n等待下一条群消息。\n[保持安静]\n```", "`等待下一条群消息。`"):
            self.assertEqual(plain_reply(text, 1200).reply_text, text)


class SoftwareControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / "browser/chrome.exe"
        self.exe.parent.mkdir()
        self.exe.touch()
        self.backend = FrameBackend()
        self.patcher = patch("sandbox_runtime.SandboxRuntime.device", return_value=self.backend)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.broker = ConsoleBroker(self.root / "console.db")
        self.metadata = {"namespace": "account", "scope": "group", "sender_id": "owner", "is_master": True,
            "source_text": str(self.exe), "sandbox_config": {"enabled": True, "mode": "background",
            "directory": str(self.root / "sandbox"), "max_actions": 10,
            "browser_profiles": {"video": str(self.root / "video-profile")}}}
        self.context = self.broker.issue_context(self.metadata, self.root / "contexts")
        self.request = execute("sandbox", self.context, operation="use", params={"executable": str(self.exe),
            "profile": "video", "url": "https://example.com/", "text": "自己看看游戏相关视频和帖子"})
        self.runtime = SandboxRuntime(self.root / "sandbox")
        self.target_id = json.loads(self.broker.record(self.request["id"])["result"])["target_id"]
        self.sid, self.target = self.runtime.begin(self.metadata, self.target_id, self.request["id"])
        self.broker.update(self.request["id"], "running", {"target_id": self.target_id, "sandbox_session_id": self.sid})
        self.worker = self.broker.issue_context({**self.metadata, "sandbox_session_id": self.sid}, self.root / "worker")

    def mcp(self, operation, **params):
        result = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "qqbot_desktop_" + operation, "arguments": {"context_file": self.worker, **params}}})["result"]
        value = json.loads(result["content"][0]["text"])
        self.assertFalse(result["isError"], value)
        self.assertEqual(result["content"][1]["type"], "image")
        return value

    def test_text_scroll_navigation_return_images_and_do_not_replay(self):
        observed = self.mcp("screenshot")
        params = {"observation_id": observed["observation_id"], "text": "想看剧情与配乐", "wait_ms": 0}
        current = self.mcp("type", **params)
        repeated = self.mcp("type", **params)
        self.assertEqual(current["observation_id"], repeated["observation_id"])
        self.assertEqual(len(self.backend.inputs), 1)
        current = self.mcp("scroll", observation_id=current["observation_id"], delta=600, wait_ms=0)
        self.assertAlmostEqual(self.backend.inputs[-1][1]["x"], .50125)
        current = self.mcp("navigate", observation_id=current["observation_id"], url="https://example.com/posts", wait_ms=0)
        self.mcp("key", observation_id=current["observation_id"], key="ctrl+a", wait_ms=0)
        self.assertEqual([item[0] for item in self.backend.inputs], ["type", "scroll", "navigate", "key"])
        with self.runtime.connect() as db:
            self.assertNotIn("想看剧情与配乐", str([dict(row) for row in db.execute("SELECT * FROM actions")]))

    def test_bad_input_never_consumes_a_frame_or_touches_the_process(self):
        observed = self.mcp("screenshot")
        for operation, params in (("type", {"text": ""}), ("type", {"text": "x" * 4001}),
                                  ("scroll", {"delta": 0}), ("scroll", {"delta": float("nan")}),
                                  ("navigate", {"url": "javascript:alert(1)"}), ("navigate", {"url": "file:///C:/secret"}),
                                  ("navigate", {"url": "https://user:secret@example.com/"})):
            with self.subTest(operation=operation, params=params), self.assertRaises(ValueError):
                execute("desktop", self.worker, operation=operation, params={"observation_id": observed["observation_id"], **params})
        self.assertFalse(self.backend.inputs)
        self.assertEqual(self.runtime.session(self.metadata, self.sid)["actions"], 0)

    def test_observations_are_real_software_records_and_login_can_pause_autonomy(self):
        broker, token = ConsoleBroker.from_context(self.context)
        broker.sandbox(token, "autoplay", target_id=self.target_id, enabled=True)
        observed = self.mcp("screenshot")
        saved = execute("sandbox", self.worker, operation="note", params={"text": "页面提示需要登录。",
            "kind": "observation", "evidence": [observed["observation_id"]]})
        self.assertEqual(saved["source"], "software_observation")
        execute("sandbox", self.worker, operation="pause", params={"text": "等待主人完成登录"})
        self.assertFalse(self.runtime.game(self.metadata, self.target_id)["autoplay"])
        notes = execute("sandbox", self.context, operation="notes")["notes"]
        self.assertEqual(notes[0]["source"], "software_observation")
        stranger = {**self.metadata, "sender_id": "someone-else"}
        self.assertEqual(self.runtime.notes(stranger), [])

    def test_profile_is_bound_and_work_context_cannot_register_other_software(self):
        self.assertEqual(Path(self.target["profile_path"]), (self.root / "video-profile").resolve())
        apps = execute("sandbox", self.context, operation="apps")["apps"]
        self.assertEqual(apps[0]["goal"], "自己看看游戏相关视频和帖子")
        self.assertNotIn("profile_path", apps[0])
        self.assertEqual(execute("sandbox", self.context, operation="games")["games"], [])
        with self.assertRaises(PermissionError):
            execute("sandbox", self.worker, operation="use", params={"executable": str(self.exe)})
        with self.assertRaises(PermissionError):
            self.runtime.register({**self.metadata, "source_text": "使用浏览器"}, str(self.exe), kind="software")
        with self.assertRaises(PermissionError):
            self.runtime.register(self.metadata, str(self.exe), kind="software", profile="not-configured")

    def test_cli_accepts_unicode_text_and_software_tools_have_typed_schemas(self):
        observed = self.mcp("screenshot")
        output = io.StringIO()
        with redirect_stdout(output):
            main(["desktop", "--context-file", self.worker, "--operation", "type", "--observation-id",
                  observed["observation_id"], "--text", "测试中文输入", "--wait-ms", "0"])
        self.assertTrue(json.loads(output.getvalue())["input_posted"])
        self.assertEqual(self.backend.inputs[-1][1]["text"], "测试中文输入")
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools_schema()}
        for operation, required in (("type", "text"), ("scroll", "delta"), ("navigate", "url")):
            self.assertIn(required, schemas["qqbot_desktop_" + operation]["required"])

    def test_legacy_database_migrates_without_losing_game_identity_or_progress(self):
        root = self.root / "old"
        root.mkdir()
        with closing(sqlite3.connect(root / "sandbox.sqlite3")) as db, db:
            db.execute("CREATE TABLE games(id TEXT PRIMARY KEY,namespace TEXT,scope TEXT,owner TEXT,executable TEXT,title TEXT,source_token TEXT,autoplay INTEGER DEFAULT 0,last_run REAL DEFAULT 0)")
            db.execute("INSERT INTO games VALUES('old','account','group','owner',?,'Game','token',1,123)", (str(self.exe),))
        old = SandboxRuntime(root).game(self.metadata, "old")
        self.assertEqual((old["kind"], old["autoplay"], old["last_run"]), ("game", 1, 123))


class BrowserBridgeTests(unittest.TestCase):
    def test_browser_internal_account_dialogs_are_not_selected_as_web_pages(self):
        bridge = BrowserBridge({"profile_path": "profile"})
        pages = [
            {"id": "dialog", "type": "page", "url": "edge://sync-confirmation-dialog/", "webSocketDebuggerUrl": "ws://127.0.0.1:3333/devtools/page/dialog"},
            {"id": "website", "type": "page", "url": "https://example.com/", "webSocketDebuggerUrl": "ws://127.0.0.1:3333/devtools/page/website"},
            {"id": "foreign", "type": "page", "url": "https://example.com/", "webSocketDebuggerUrl": "ws://example.net/devtools/page/foreign"}]
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=json.dumps(pages).encode())))
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(bridge, "endpoint", return_value="http://127.0.0.1:3333"), patch("browser_control.build_opener", return_value=opener):
            self.assertEqual([page["id"] for page in bridge.pages()], ["website"])
            self.assertEqual([page["id"] for page in bridge.pages(include_internal=True)], ["dialog", "website"])

    def test_page_dispatch_uses_viewport_and_releases_inputs(self):
        bridge = BrowserBridge({"profile_path": "profile"})
        page = {"id": "page", "url": "https://example.com/"}
        observed = {"browser_page": "page", "page_url": page["url"], "browser_viewport": [800, 600]}
        with patch.object(bridge, "page", return_value=page), patch.object(bridge, "connection"), \
                patch.object(bridge, "input_state", return_value={"focus": {"editable": True}}), \
                patch.object(bridge, "command") as command:
            bridge.act(observed, "scroll", x=.5, y=.25, delta=600)
            self.assertEqual(command.call_args.args[3], {"x": 400, "y": 150, "type": "mouseWheel", "deltaX": 0, "deltaY": 600})
            command.reset_mock()
            bridge.act(observed, "type", text="中文😀")
            self.assertEqual(command.call_args.args[2:], ("Input.insertText", {"text": "中文😀"}))
            command.reset_mock()
            bridge.act(observed, "key", key="ctrl+a")
            self.assertEqual([call.args[3]["type"] for call in command.call_args_list], ["rawKeyDown", "keyUp"])
            self.assertEqual(command.call_args_list[0].args[3]["modifiers"], 2)

    def test_page_navigation_change_rejects_stale_input(self):
        bridge = BrowserBridge({"profile_path": "profile"})
        with patch.object(bridge, "page", return_value={"id": "page", "url": "https://example.com/new"}), patch.object(bridge, "connection") as connection:
            with self.assertRaises(BrowserError):
                bridge.act({"browser_page": "page", "page_url": "https://example.com/old"}, "type", text="private")
            connection.assert_not_called()


class NativeSoftwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from background_window import BackgroundWindow
        self.backend = BackgroundWindow(self.root / "windows")
        self.target = {"id": "app", "executable": str(self.root / "Editor.exe"), "kind": "software"}
        self.api = FakeBackgroundApi()
        self.desktop = self.backend.desktop_name(self.target)
        self.observed = {**self.api.row, "mode": "background", "desktop": self.desktop,
                         "target_id": "app", "coordinate_space": "client"}

    def act(self, action, **params):
        return self.backend._act(self.api, self.target, self.desktop, self.observed, action,
            key="", x=.5, y=.25, button="left", guard=None, **params)

    def test_native_unicode_including_surrogate_pairs_is_delivered_without_clipboard(self):
        self.act("type", text="中文😀")
        raw = "中文😀".encode("utf-16-le")
        expected = [int.from_bytes(raw[index:index + 2], "little") for index in range(0, len(raw), 2)]
        self.assertEqual([post[1] for post in self.api.posts], [0x102] * 4)
        self.assertEqual([post[2] for post in self.api.posts], expected)

    def test_native_scroll_coordinates_are_mapped_without_moving_cursor(self):
        from ctypes import wintypes as w
        def map_point(_source, _dest, pointer, _count):
            point = ctypes.cast(pointer, ctypes.POINTER(w.POINT)).contents
            point.x -= 1000
            point.y -= 400
            return 1
        self.api.u.MapWindowPoints = Mock(side_effect=map_point)
        self.act("scroll", delta=600)
        post = self.api.posts[-1]
        self.assertEqual(post[1], 0x20A)
        self.assertEqual(post[2], ((-720) & 0xFFFF) << 16)
        self.assertEqual(post[3], ((240 - 1000) & 0xFFFF) | (((80 - 400) & 0xFFFF) << 16))

    def test_browser_profile_flyout_does_not_hide_the_named_page_window(self):
        browser = {**self.target, "executable": str(self.root / "msedge.exe")}
        page = {**self.api.row, "executable": browser["executable"], "title": "Video page - Edge"}
        self.api.rows = [{**page, "title": "", "hwnd": 99, "size": [524, 364]}, page]
        self.assertEqual(self.backend._locate(self.api, browser), page)
        result = self.backend._launch(self.api, browser, self.desktop)
        self.assertTrue(result["attached"])

    def test_closed_browser_can_reopen_immediately_without_replaying_old_inputs(self):
        self.api.rows = []
        self.api.process = Mock(return_value=None)
        self.api.create_process = Mock(return_value=18)
        Path(self.target["executable"]).touch()
        state = self.backend.root / (self.desktop + ".json")
        import time
        state.write_text(json.dumps({"pid": 17, "process": {"pid": 17}, "started": time.time()}))
        result = self.backend._launch(self.api, self.target, self.desktop)
        self.assertEqual(result["pid"], 18)
        self.assertFalse(result["attached"])


class SoftwareChatIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_idle_wait_has_its_own_context_and_never_sends_fallback_text(self):
        bot, _ = self.fixture.bot(stored_cid="existing")
        bot.app_config.bot.proactive_chat_enabled = True
        bot.app_config.bot.proactive_interval_minutes = 1
        bot._is_running = True
        from types import SimpleNamespace
        bot.ws = SimpleNamespace(closed=False)
        async def generate(_key, prompt, **options):
            contexts = [json.loads(line) for line in prompt.split("\n\n") if line.startswith('{"console_context_file"')]
            context = contexts[-1]["console_context_file"]
            with self.assertRaises(PermissionError):
                execute("submit", context, task="not allowed")
            execute("wait", context)
            self.assertTrue(options["stop_requested"]())
            return "我正在后台推进游戏。等待下一条群消息。"
        bot.ai.chat.side_effect = generate
        bot._proactive_chat.observe("12345678", "old", received_at=self.fixture.baseline)
        with patch("time.time", return_value=self.fixture.baseline + 61):
            bot._proactive_chat.tick()
            await asyncio.gather(*list(bot._runtime.chat_workers.values()))
        bot.ai.chat.assert_awaited_once()
        bot.send_action.assert_not_awaited()

    async def test_software_worker_records_results_quietly_and_scheduler_keeps_goal(self):
        from test_ai_fixture import drain_runtime
        bot, key = self.fixture.bot(stored_cid="existing")
        bot.app_config.bot.master_id = "456"
        bot.ai.archive_session = AsyncMock(return_value=True)
        exe = Path("browser/chrome.exe").resolve()
        exe.parent.mkdir()
        exe.touch()
        backend = FrameBackend()
        backend.launch = lambda _target: {"pid": 77}
        async def generate(session, prompt, **_options):
            if session.startswith("__work__:"):
                context = str(list(Path(bot.app_config.sandbox.directory).glob("sessions/*/contexts/*.json"))[-1])
                self.assertIn("刷视频", prompt)
                self.assertIn("kind=observation", prompt)
                first = execute("desktop", context, operation="screenshot")
                after = execute("desktop", context, operation="scroll", params={"observation_id": first["observation_id"], "delta": 500, "wait_ms": 0})
                execute("sandbox", context, operation="note", params={"text": "页面展示游戏配乐讨论。", "kind": "observation", "evidence": [after["observation_id"]]})
                execute("sandbox", context, operation="pause", params={"text": "继续阅读需要主人完成登录，已保存页面观察。"})
                return "看了页面中的配乐讨论，记下标题；没有听取音频。"
            context = list(bot._get_main_coordinator().requests)[-1]
            execute("sandbox", context, operation="use", params={"executable": str(exe), "text": "刷视频和看游戏帖子"})
            execute("wait", context)
            return "等待下一条群消息。"
        bot.ai.chat.side_effect = generate
        with patch("sandbox_runtime.SandboxRuntime.device", return_value=backend):
            await self.fixture.receive(bot, False, "software", "用 " + str(exe) + " 刷视频和看游戏帖子")
            await drain_runtime(bot)
        bot.send_group_message.assert_not_awaited()
        bot.ai.archive_session.assert_not_awaited()
        runtime = SandboxRuntime(bot.app_config.sandbox.directory)
        with runtime.connect() as db:
            row = dict(db.execute("SELECT * FROM games WHERE kind='software'").fetchone())
            self.assertEqual(db.execute("SELECT state FROM sessions").fetchone()[0], "paused")
        console = bot._get_main_coordinator()
        metadata = {"namespace": console.namespace, "scope": row["scope"], "sender_id": "456", "is_master": True,
            "session_key": key, "generation": 0, "source_text": "继续刷视频", "sandbox_config": bot.app_config.sandbox.model_dump()}
        context = console.broker.issue_context(metadata, "schedule-context")
        broker, token = ConsoleBroker.from_context(context)
        broker.sandbox(token, "autoplay", target_id=row["id"], enabled=True)
        bot.app_config.sandbox.autonomous_enabled = True
        with runtime.connect() as db:
            db.execute("UPDATE games SET last_run=0 WHERE id=?", (row["id"],))
        await console.schedule_learning()
        pending = console.broker.pending(console.namespace)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["task"], "刷视频和看游戏帖子")
        console.sandbox_schedule_after = 0
        with runtime.connect() as db:
            db.execute("UPDATE games SET last_run=0 WHERE id=?", (row["id"],))
        await console.schedule_learning()
        self.assertEqual(len(console.broker.pending(console.namespace)), 1)


if __name__ == "__main__":
    unittest.main()
