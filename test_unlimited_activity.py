"""Indefinite desktop work must survive model finals, old limits and age."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from config import SandboxConfig
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.server import tools_schema
from sandbox_runtime import SandboxRuntime
from test_desktop_control import FrameBackend
import test_startup_participation as fixtures


class UnlimitedDesktopCapabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.exe = self.root / "browser/chrome.exe"
        self.exe.parent.mkdir()
        self.exe.touch()
        self.backend = FrameBackend()
        device = patch("sandbox_runtime.SandboxRuntime.device", return_value=self.backend)
        device.start()
        self.addCleanup(device.stop)
        self.broker = ConsoleBroker(self.root / "console.db")
        self.metadata = {"namespace": "account", "scope": "group", "sender_id": "owner", "is_master": True,
            "source_text": str(self.exe), "sandbox_config": {"enabled": True, "directory": str(self.root / "sandbox")}}
        self.owner = self.broker.issue_context(self.metadata, self.root / "contexts")
        self.request = execute("sandbox", self.owner, operation="use", params={"executable": str(self.exe)})
        target = json.loads(self.broker.record(self.request["id"])["result"])["target_id"]
        self.runtime = SandboxRuntime(self.root / "sandbox")
        self.sid, _ = self.runtime.begin(self.metadata, target, self.request["id"])
        self.broker.update(self.request["id"], "running")
        self.worker = self.broker.issue_context({**self.metadata, "sandbox_session_id": self.sid}, self.root / "worker")

    def test_default_control_still_posts_inputs_beyond_both_old_limits(self):
        self.assertEqual(SandboxConfig().max_actions, 0)
        self.assertEqual(SandboxConfig(max_actions=10000).max_actions, 10000)
        with self.runtime.connect() as db:
            db.execute("UPDATE sessions SET actions=600 WHERE id=?", (self.sid,))
        observation = execute("desktop", self.worker, operation="screenshot")
        for _ in range(3):
            observation = execute("desktop", self.worker, operation="key", params={
                "observation_id": observation["observation_id"], "key": "enter", "wait_ms": 0})
            self.assertTrue(observation["ok"])
        self.assertEqual(len(self.backend.inputs), 3)
        self.assertEqual(self.runtime.session(self.metadata, self.sid)["actions"], 603)

    def test_an_explicit_positive_limit_is_still_enforced(self):
        bounded = SandboxRuntime(self.runtime.root, max_actions=2)
        for _ in range(2):
            observed = bounded.observe(self.metadata, self.sid)
            bounded.act(self.metadata, self.sid, observed["id"], "key", key="enter")
        observed = bounded.observe(self.metadata, self.sid)
        with self.assertRaisesRegex(ValueError, "额度"):
            bounded.act(self.metadata, self.sid, observed["id"], "key", key="enter")
        self.assertEqual(len(self.backend.inputs), 2)

    def test_running_worker_keeps_access_after_a_day_and_stop_revokes_input(self):
        later = time.time() + 3 * 86400
        with patch("time.time", return_value=later):
            with self.assertRaisesRegex(ValueError, "过期"):
                ConsoleBroker.from_context(self.owner)
            observed = execute("desktop", self.worker, operation="screenshot")
            result = execute("desktop", self.worker, operation="key", params={
                "observation_id": observed["observation_id"], "key": "enter", "wait_ms": 0})
            self.assertTrue(result["ok"])
            owner = self.broker.issue_context(self.metadata, self.root / "renewed")
            execute("sandbox", owner, operation="stop")
            with self.assertRaises(ValueError):
                execute("desktop", self.worker, operation="key", params={
                    "observation_id": result["observation_id"], "key": "enter", "wait_ms": 0})
        self.assertEqual(len(self.backend.inputs), 1)

    def test_finish_requires_evidence_and_prevents_further_input(self):
        with self.assertRaisesRegex(ValueError, "截图依据"):
            execute("sandbox", self.worker, operation="finish", params={"text": "完成"})
        observed = execute("desktop", self.worker, operation="screenshot")
        execute("sandbox", self.worker, operation="note", params={"text": "页面展示一条游戏讨论。",
            "kind": "observation", "evidence": [observed["observation_id"]]})
        receipt = execute("sandbox", self.worker, operation="finish", params={"text": "已阅读主人指定的一条讨论。"})
        self.assertEqual(receipt["action"], "finish")
        with self.assertRaisesRegex(ValueError, "完成或暂停"):
            self.runtime.act(self.metadata, self.sid, observed["observation_id"], "key", key="enter")
        schema = next(row for row in tools_schema() if row["name"] == "qqbot_sandbox")
        self.assertIn("finish", schema["inputSchema"]["properties"]["operation"]["enum"])


class UnlimitedActivityWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_model_final_keeps_software_and_game_running_until_owner_stops(self):
        from test_ai_fixture import drain_runtime
        for software in (True, False):
            with self.subTest(software=software):
                bot, key = self.fixture.bot(stored_cid="existing")
                bot.app_config.bot.master_id = "456"
                bot.app_config.sandbox.directory = f"sandbox-{software}"
                bot.ai.archive_session = AsyncMock(return_value=True)
                exe = Path(f"app-{software}/" + ("chrome.exe" if software else "Game.exe")).resolve()
                exe.parent.mkdir()
                exe.touch()
                backend = FrameBackend()
                backend.launch = lambda _target: {"pid": 77}
                waiting, release = asyncio.Event(), asyncio.Event()
                worker_calls = 0
                request_id = ""

                async def agent(session, prompt, **options):
                    nonlocal worker_calls, request_id
                    if not session.startswith("__work__:"):
                        if not request_id:
                            context = list(bot._get_main_coordinator().requests)[-1]
                            request_id = execute("sandbox", context, operation="use" if software else "play",
                                params={"executable": str(exe), "text": "持续浏览帖子" if software else "持续阅读游戏剧情"})["id"]
                        return "好。"
                    worker_calls += 1
                    self.assertEqual(options["timeout"], 0)
                    self.assertIn("操作次数不限", prompt)
                    if worker_calls == 2:
                        waiting.set()
                        await release.wait()
                        return "仍在浏览。"
                    context = str(next(Path(bot.app_config.sandbox.directory).glob("sessions/*/contexts/*.json")))
                    observed = execute("desktop", context, operation="screenshot")
                    after = execute("desktop", context, operation="key", params={
                        "observation_id": observed["observation_id"], "key": "enter", "wait_ms": 0})
                    execute("sandbox", context, operation="note", params={"text": "已观察到当前画面。",
                        "kind": "observation" if software else "plot", "evidence": [after["observation_id"]]})
                    return "正在导航回论坛首页，稍候查看最新页面。"

                bot.ai.chat.side_effect = agent
                with patch("sandbox_runtime.SandboxRuntime.device", return_value=backend):
                    try:
                        await self.fixture.receive(bot, False, f"start-{software}", "使用 " + str(exe))
                        await asyncio.wait_for(waiting.wait(), 5)
                        console = bot._get_main_coordinator()
                        self.assertEqual(console.broker.record(request_id)["state"], "running")
                        with SandboxRuntime(bot.app_config.sandbox.directory).connect() as db:
                            self.assertEqual(db.execute("SELECT state FROM sessions").fetchone()[0], "running")
                        bot.ai.archive_session.assert_not_awaited()
                        # A new normal chat turn must not close independent work.
                        await self.fixture.receive(bot, False, f"chat-{software}", "现在聊点别的。", elapsed=1)
                        self.assertEqual(console.broker.record(request_id)["state"], "running")
                    finally:
                        console = bot._get_main_coordinator()
                        owner = console.issue_chat_context(console.contexts[key], key)[0]
                        execute("sandbox", owner, operation="stop")
                        await console.poll()
                        release.set()
                        await asyncio.wait_for(drain_runtime(bot), 5)
                self.assertEqual(console.broker.record(request_id)["state"], "cancelled")
                bot.ai.archive_session.assert_not_awaited()

    async def test_explicit_action_limit_pauses_without_archiving_the_model(self):
        from test_ai_fixture import drain_runtime
        bot, _ = self.fixture.bot(stored_cid="existing")
        bot.app_config.bot.master_id = "456"
        bot.app_config.sandbox.max_actions = 1
        bot.ai.archive_session = AsyncMock(return_value=True)
        exe = Path("browser/chrome.exe").resolve()
        exe.parent.mkdir()
        exe.touch()
        backend = FrameBackend()
        backend.launch = lambda _target: {"pid": 77}
        request_id = ""

        async def agent(session, _prompt, **_options):
            nonlocal request_id
            if not session.startswith("__work__:"):
                context = list(bot._get_main_coordinator().requests)[-1]
                request_id = execute("sandbox", context, operation="use", params={"executable": str(exe)})["id"]
                return "好。"
            context = str(next(Path(bot.app_config.sandbox.directory).glob("sessions/*/contexts/*.json")))
            observed = execute("desktop", context, operation="screenshot")
            execute("desktop", context, operation="key", params={"observation_id": observed["observation_id"], "key": "enter", "wait_ms": 0})
            return "当前操作已投递，进度待继续。"

        bot.ai.chat.side_effect = agent
        with patch("sandbox_runtime.SandboxRuntime.device", return_value=backend):
            await self.fixture.receive(bot, False, "bounded", "使用 " + str(exe))
            await asyncio.wait_for(drain_runtime(bot), 5)
        self.assertEqual(bot._get_main_coordinator().broker.record(request_id)["state"], "paused")
        bot.ai.archive_session.assert_not_awaited()

    async def test_model_return_after_owner_stop_cannot_restore_running_state(self):
        from test_ai_fixture import drain_runtime
        bot, key = self.fixture.bot(stored_cid="existing")
        bot.app_config.bot.master_id = "456"
        bot.ai.archive_session = AsyncMock(return_value=True)
        exe = Path("browser/chrome.exe").resolve()
        exe.parent.mkdir()
        exe.touch()
        backend = FrameBackend()
        backend.launch = lambda _target: {"pid": 77}
        request_id = ""

        async def agent(session, _prompt, **_options):
            nonlocal request_id
            owner = list(bot._get_main_coordinator().requests)[-1]
            if not session.startswith("__work__:"):
                request_id = execute("sandbox", owner, operation="use", params={"executable": str(exe)})["id"]
                return "好。"
            console = bot._get_main_coordinator()
            owner = console.issue_chat_context(console.contexts[key], key)[0]
            execute("sandbox", owner, operation="stop")
            return "正在导航，稍后继续。"

        bot.ai.chat.side_effect = agent
        with patch("sandbox_runtime.SandboxRuntime.device", return_value=backend):
            await self.fixture.receive(bot, False, "stop-race", "使用 " + str(exe))
            await asyncio.wait_for(drain_runtime(bot), 5)
        self.assertEqual(bot._get_main_coordinator().broker.record(request_id)["state"], "cancelled")
        self.assertFalse(backend.inputs)
        bot.ai.archive_session.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
