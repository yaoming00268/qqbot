"""Offline checks for persistent wishes, scoped browsing and interruptible personal time."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from companion_life import state_for
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.contracts import capabilities
from test_ai_fixture import prompt_object


class CompanionLifeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot_companion_life_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.metadata = {"namespace": "account-a", "scope": "onebot:a:group:123", "session_key": "persona-a",
            "main_agent": True, "self_activity": True, "idle_event": True, "is_master": False,
            "sender_id": "", "source_message_id": "", "source_text": "", "generation": 0,
            "transport": "onebot", "is_group": True, "conversation_id": "123", "received_at": 1,
            "self_activity_config": {"enabled": True},
            "sandbox_config": {"enabled": True, "mode": "background", "directory": str(self.root / "sandbox"),
                               "profiles": {"private": "private-files"}, "browser_profiles": {"account": "login"}}}
        self.context = self.broker.issue_context(self.metadata, self.root / "contexts")

    def test_interests_wishes_survive_restart_and_stay_in_their_persona_and_account(self):
        execute("companion", self.context, operation="remember", params={"text": "喜欢研究游戏配乐", "kind": "preference"})
        execute("companion", self.context, operation="remember", params={"text": "想了解弦乐编曲"})
        first = execute("companion", self.context, operation="wish", params={"text": "想试试这款解谜游戏"})
        execute("companion", self.context, operation="wish", params={"text": "想试试这款解谜游戏"})
        execute("companion", self.context, operation="resolve",
                params={"wish_id": first["wishes"][0]["id"], "wish_status": "declined"})
        restored = ConsoleBroker(self.broker.path)
        state = state_for(restored, "account-a", "persona-a")
        self.assertEqual(state["interests"], ["喜欢研究游戏配乐"])
        self.assertEqual(state["reflection"], "想了解弦乐编曲")
        self.assertEqual(len(state["wishes"]), 1)
        self.assertEqual(state["wishes"][0]["status"], "declined")
        for account, persona in (("account-b", "persona-a"), ("account-a", "persona-b")):
            self.assertFalse(any(state_for(restored, account, persona).values()))

    def test_personal_time_cannot_send_chat_run_commands_or_grant_access(self):
        calls = [("reply", {"text": ["误发消息"]}), ("submit", {"task": "执行命令"}),
                 ("sandbox", {"operation": "grant", "params": {"path": str(self.root)}}),
                 ("desktop", {"operation": "screenshot"})]
        for action, values in calls:
            with self.subTest(action=action), self.assertRaises(PermissionError):
                execute(action, self.context, **values)
        for additions in ({"sandbox_session_id": "worker"}, {"main_agent": False}, {"activity_controller": True}):
            other = self.broker.issue_context({**self.metadata, **additions}, self.root / "contexts")
            with self.assertRaises(PermissionError):
                execute("companion", other, operation="wish", params={"text": "不属于执行分支的愿望"})

    def test_browse_creates_one_quiet_real_browser_task_without_human_identity_or_login(self):
        executable = self.root / "apps" / "chrome.exe"
        executable.parent.mkdir()
        executable.write_bytes(b"fixture")
        with patch("browser_control.installed_browser", return_value=executable):
            receipt = execute("companion", self.context, operation="browse",
                              params={"url": "https://example.com/music", "text": "想了解游戏配乐的作曲方式"})
            again = execute("companion", self.context, operation="browse",
                            params={"url": "https://example.com/games", "text": "也想看看解谜游戏"})
        self.assertEqual(receipt["id"], again["id"])
        request = self.broker.record(receipt["id"])
        metadata = self.broker.context(request["token"])
        self.assertEqual(request["kind"], "sandbox_play")
        self.assertFalse(metadata["is_master"])
        self.assertTrue(metadata["self_directed"])
        self.assertTrue(metadata["sender_id"].startswith("self:"))
        self.assertEqual(metadata["sandbox_config"]["browser_profiles"], {})
        self.assertEqual(metadata["sandbox_config"]["profiles"], {})
        self.assertTrue(json.loads(request["result"])["quiet"])
        self.assertNotIn("reply", capabilities({**metadata, "sandbox_session_id": "worker"}))
        from sandbox_runtime import SandboxRuntime
        runtime = SandboxRuntime(self.root / "sandbox")
        target = runtime.game(metadata, runtime.games(metadata)[0]["id"])
        self.assertEqual(target["launch_url"], "https://example.com/music")
        self.assertEqual(target["autoplay"], 0)

    def test_browse_requires_enabled_main_context_and_an_open_turn(self):
        for additions in ({"self_activity_config": {"enabled": False}}, {"self_activity": False, "proactive_event": True}):
            other = self.broker.issue_context({**self.metadata, **additions}, self.root / "contexts")
            with self.assertRaises(PermissionError):
                execute("companion", other, operation="browse", params={"url": "https://example.com", "text": "想阅读"})
        execute("wait", self.context)
        with self.assertRaises(PermissionError):
            execute("companion", self.context, operation="wish", params={"text": "这轮已经结束"})

    def test_ordinary_chat_can_start_personal_reading_before_its_reply(self):
        executable = self.root / "chrome.exe"
        executable.write_bytes(b"fixture")
        ordinary = self.broker.issue_context({**self.metadata, "self_activity": False, "idle_event": False}, self.root / "contexts")
        with patch("browser_control.installed_browser", return_value=executable):
            receipt = execute("companion", ordinary, operation="browse",
                params={"url": "https://example.com/interview", "text": "刚才的话题让我想看看这篇访谈"})
        self.assertEqual(self.broker.record(receipt["id"])["state"], "submitted")

    def test_agent_result_can_start_a_browser_without_leaking_its_mailbox_role_or_parent_token(self):
        executable = self.root / "chrome.exe"
        executable.write_bytes(b"fixture")
        notice = self.broker.issue_context({**self.metadata, "self_activity": False, "agent_event": True,
            "autonomous_agents": True, "_agent_source_token": "private-parent-token", "agent_notice_ids": [11]}, self.root / "contexts")
        with patch("browser_control.installed_browser", return_value=executable):
            receipt = execute("companion", notice, operation="browse",
                params={"url": "https://example.com/interview", "text": "想继续阅读子代理找到的访谈"})
        metadata = self.broker.context(self.broker.record(receipt["id"])["token"])
        self.assertNotIn("_agent_source_token", metadata)
        self.assertNotIn("agent_event", metadata)
        worker = {**metadata, "sandbox_session_id": "browser", "parent_request_id": receipt["id"]}
        self.assertIn("desktop", capabilities(worker))
        self.assertNotIn("agents", capabilities(worker))


class PersonalTimeSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()
        self.patches = []

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()
        for item in self.patches:
            item.stop()

    async def prepare(self, official=False):
        bot, key = self.fixture.bot(official, stored_cid="personal-time-cid")
        bot.app_config.bot.self_activity_enabled = True
        bot.app_config.bot.self_activity_interval_minutes = 1
        bot.app_config.bot.proactive_chat_enabled = False
        coordinator = bot._get_main_coordinator()
        prevent_watch = patch.object(coordinator, "start")
        prevent_watch.start()
        self.patches.append(prevent_watch)
        from test_ai_fixture import ScriptedConsoleAgentMock
        bot.ai.chat = ScriptedConsoleAgentMock(return_value="")
        message = await self.fixture.receive(bot, official, "first", "先去忙了")
        bot.ai.chat.reset_mock()
        return bot, key, coordinator, message

    async def tick(self, coordinator, elapsed):
        with patch("time.time", return_value=self.fixture.baseline + elapsed):
            await coordinator.schedule_self_activity()
            await asyncio.gather(*coordinator.self_activity_tasks.values())

    async def test_thinking_runs_without_proactive_chat_on_both_transports_and_never_sends(self):
        for official in (False, True):
            bot, key, coordinator, message = await self.prepare(official)
            async def think(session_key, prompt, **options):
                self.assertEqual(session_key, key)
                context = prompt_object(prompt, "console_context_file")["console_context_file"]
                execute("companion", context, operation="remember", params={"text": "对配乐有点好奇", "kind": "preference"})
                execute("companion", context, operation="wish", params={"text": "想玩一款音乐解谜游戏"})
                return "内部想法不应该成为群聊台词"
            bot.ai.chat.side_effect = think
            with patch("exploration.ensure_exploration"):
                await self.tick(coordinator, 59)
                bot.ai.chat.assert_not_awaited()
                await self.tick(coordinator, 60)
                bot.ai.chat.assert_awaited_once()
                await self.tick(coordinator, 61)
                bot.ai.chat.assert_awaited_once()
            self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "personal-time-cid")
            self.assertEqual(state_for(coordinator.broker, coordinator.namespace, key)["interests"], ["对配乐有点好奇"])
            finished_context = prompt_object(bot.ai.chat.await_args.args[1], "console_context_file")["console_context_file"]
            with self.assertRaises(PermissionError):
                execute("companion", finished_context, operation="wish", params={"text": "不能重用旧的独处机会"})
            if official:
                message.reply.assert_not_awaited()
            else:
                bot.send_action.assert_not_awaited()
                bot.send_group_message.assert_not_awaited()

    async def test_fresh_message_interrupts_thinking_and_is_processed_in_same_session(self):
        bot, key, coordinator, _ = await self.prepare()
        entered = asyncio.Event()
        async def generate(session_key, prompt, **options):
            if '"type": "self_activity"' in prompt:
                entered.set()
                await asyncio.Event().wait()
            return ""
        bot.ai.chat.side_effect = generate
        with patch("time.time", return_value=self.fixture.baseline + 60):
            await coordinator.schedule_self_activity()
            await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(self.fixture.receive(bot, False, "new", "回来啦", elapsed=61), 3)
        self.assertTrue(coordinator.self_activity_tasks[key].cancelled())
        self.assertIn("回来啦", bot.ai.chat.await_args.args[1])
        self.assertEqual(bot.ai.chat.await_args.args[0], key)
        bot.send_action.assert_not_awaited()

    async def test_idle_browse_runs_worker_and_completed_observations_return_without_chat(self):
        from test_ai_fixture import drain_runtime
        from test_browser_requests import BrowserFrameBackend
        for official in (False, True):
            bot, key, coordinator, message = await self.prepare(official)
            executable = Path(f"self-browser-{official}/chrome.exe").resolve()
            executable.parent.mkdir()
            executable.touch()
            bot.ai.archive_session = AsyncMock(return_value=True)
            backend = BrowserFrameBackend()
            backend.launch = lambda target: {"pid": 77}
            request_id = ""
            async def agent(session_key, prompt, **options):
                nonlocal request_id
                if session_key.startswith("__work__:task:sandbox:"):
                    self.assertIn("自己感兴趣的公开内容", prompt)
                    self.assertNotIn("持续目标一直执行到主人停止", prompt)
                    context = re.search(r"当前上下文文件：([^\r\n]+)", prompt)[1]
                    frame = execute("desktop", context, operation="screenshot")
                    execute("sandbox", context, operation="note", params={"kind": "observation",
                        "text": "实际测试页面展示了配乐讨论。", "evidence": [frame["observation_id"]]})
                    execute("sandbox", context, operation="finish", params={"text": "已阅读并记录这页配乐讨论。"})
                    return "已完成这次具体阅读。"
                if '"type": "self_activity"' in prompt:
                    context = prompt_object(prompt, "console_context_file")["console_context_file"]
                    receipt = execute("companion", context, operation="browse",
                        params={"url": "https://example.com/music", "text": "想读配乐讨论"})
                    request_id = receipt["id"]
                    execute("wait", context)
                return ""
            bot.ai.chat.side_effect = agent
            with patch("browser_control.installed_browser", return_value=executable), \
                    patch("sandbox_runtime.SandboxRuntime.device", return_value=backend):
                await self.tick(coordinator, 60)
                await asyncio.wait_for(drain_runtime(bot), 5)
                self.assertEqual(coordinator.broker.record(request_id)["state"], "completed")
                feedback = coordinator.activity_feedback(key, coordinator.contexts[key].scope, True)
                self.assertEqual(feedback[0]["state"], "completed")
                self.assertIn("配乐讨论", feedback[0]["notes"][0]["text"])
            if official:
                message.reply.assert_not_awaited()
            else:
                bot.send_action.assert_not_awaited()
                bot.send_group_message.assert_not_awaited()

    async def test_failed_thought_is_not_retried_until_interval_and_disable_stops_thought(self):
        bot, key, coordinator, _ = await self.prepare()
        bot.ai.chat.side_effect = RuntimeError("offline model failure")
        await self.tick(coordinator, 60)
        await self.tick(coordinator, 61)
        bot.ai.chat.assert_awaited_once()
        entered = asyncio.Event()
        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        bot.ai.chat.side_effect = wait
        with patch("time.time", return_value=self.fixture.baseline + 120):
            await coordinator.schedule_self_activity()
            await asyncio.wait_for(entered.wait(), 3)
            bot.app_config.bot.self_activity_enabled = False
            await coordinator.schedule_self_activity()
            await asyncio.gather(*coordinator.self_activity_tasks.values(), return_exceptions=True)
        self.assertTrue(coordinator.self_activity_tasks[key].cancelled())

    async def test_disable_cancels_pending_browser_before_it_can_start(self):
        bot, key, coordinator, _ = await self.prepare()
        source = coordinator.contexts[key]
        metadata = {"namespace": coordinator.namespace, "scope": source.scope, "session_key": key,
            "main_agent": True, "self_activity": True, "idle_event": True, "is_master": False,
            "sender_id": "", "source_message_id": "", "source_text": "", "generation": 0,
            "transport": source.transport, "is_group": True, "conversation_id": source.conversation_id,
            "received_at": self.fixture.baseline, "self_activity_config": {"enabled": True},
            "sandbox_config": bot.app_config.sandbox.model_dump()}
        context = coordinator.broker.issue_context(metadata, Path("self-contexts"))
        executable = Path("pending-browser/chrome.exe").resolve()
        executable.parent.mkdir()
        executable.touch()
        with patch("browser_control.installed_browser", return_value=executable):
            receipt = execute("companion", context, operation="browse",
                params={"url": "https://example.com/music", "text": "想读配乐讨论"})
        coordinator.self_activity_was_enabled = True
        bot.app_config.bot.self_activity_enabled = False
        await coordinator.schedule_self_activity()
        self.assertEqual(coordinator.broker.record(receipt["id"])["state"], "cancel_requested")
        with patch("companion_tasks.play_game", new_callable=AsyncMock) as launch:
            await coordinator.poll()
            launch.assert_not_awaited()
        self.assertEqual(coordinator.broker.record(receipt["id"])["state"], "cancelled")
        bot.ai.chat.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
