"""Idle invitations stop after three real CLI waits and resume on new input."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
from collections import deque
import unittest
from unittest.mock import AsyncMock, patch

from antigravity_client import AntigravityClient
from console_tools.cli import execute
from main_chat import MainChat
import test_startup_participation as fixtures
from test_ai_fixture import ScriptedConsoleAgentMock, prompt_object


class IdleActivityWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()
        self.actions = deque()
        self.exploration = self.enterContext(patch("exploration.ensure_exploration", return_value=None))

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def model(self, key, prompt, **options):
        action = self.actions.popleft() if self.actions else "wait"
        if action == "error":
            raise RuntimeError("fixture model failed before making a decision")
        context = prompt_object(prompt, "console_context_file")["console_context_file"]
        execute(action, context)
        return ""

    async def prepare(self, official=False):
        bot, key = self.fixture.bot(official, stored_cid="idle-wait-fixture")
        coordinator = bot._get_main_coordinator()
        self.enterContext(patch.object(coordinator, "start"))
        self.enterContext(patch.object(coordinator, "poll", new_callable=AsyncMock))
        bot.ai.chat = ScriptedConsoleAgentMock(return_value="")
        await self.fixture.receive(bot, official, "initial", "先聊到这里")
        bot.app_config.bot.self_activity_enabled = True
        bot.app_config.bot.self_activity_interval_minutes = 1
        bot.ai.chat = AsyncMock(side_effect=self.model)
        return bot, key, coordinator

    async def tick(self, coordinator, elapsed):
        with patch("time.time", return_value=self.fixture.baseline + elapsed):
            await coordinator.schedule_self_activity()
            await asyncio.gather(*coordinator.self_activity_tasks.values())

    async def test_third_wait_stops_future_invitations_and_exploration_fallback_on_both_transports(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, key, coordinator = await self.prepare(official)
                before = self.exploration.call_count
                for number in range(1, 4):
                    await self.tick(coordinator, number * 61)
                    self.assertEqual(bot.ai.get_or_create_session(key).chat_state["self_activity_wait_count"], number)
                self.assertEqual(bot.ai.chat.await_count, 3)
                self.assertEqual(self.exploration.call_count - before, 2)
                for elapsed in (244, 1000, 10000):
                    await self.tick(coordinator, elapsed)
                self.assertEqual(bot.ai.chat.await_count, 3)
                self.assertEqual(self.exploration.call_count - before, 2)
                self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "idle-wait-fixture")
                if not official:
                    bot.send_group_message.assert_not_awaited()
                    bot.send_action.assert_not_awaited()

    async def test_new_chat_resets_only_its_session_even_when_the_chat_itself_chooses_wait(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, key, coordinator = await self.prepare(official)
                for number in range(1, 4):
                    await self.tick(coordinator, number * 61)
                other = bot.ai.get_or_create_session(key + "-other")
                other.chat_state["self_activity_wait_count"] = 3
                await self.fixture.receive(bot, official, "new", "现在继续聊聊", elapsed=200)
                self.assertEqual(bot.ai.get_or_create_session(key).chat_state["self_activity_wait_count"], 0)
                self.assertEqual(other.chat_state["self_activity_wait_count"], 3)
                await self.tick(coordinator, 259)
                self.assertEqual(bot.ai.chat.await_count, 4)
                await self.tick(coordinator, 261)
                self.assertEqual(bot.ai.chat.await_count, 5)
                self.assertEqual(bot.ai.get_or_create_session(key).chat_state["self_activity_wait_count"], 1)

    async def test_finished_activity_breaks_the_wait_streak(self):
        bot, key, coordinator = await self.prepare()
        self.actions.extend(("wait", "wait", "finish", "wait", "wait", "wait"))
        for number, expected in enumerate((1, 2, 0, 1, 2, 3), 1):
            await self.tick(coordinator, number * 61)
            self.assertEqual(bot.ai.get_or_create_session(key).chat_state["self_activity_wait_count"], expected)
        await self.tick(coordinator, 427)
        self.assertEqual(bot.ai.chat.await_count, 6)

    async def test_restart_preserves_suppression_and_new_input_can_resume(self):
        bot, key, coordinator = await self.prepare()
        for number in range(1, 4):
            await self.tick(coordinator, number * 61)
        source = coordinator.contexts[key]
        restored = AntigravityClient(sessions_file=bot.ai._base_sessions_file, account_key=bot.account_key)
        self.assertEqual(restored.get_or_create_session(key).chat_state["self_activity_wait_count"], 3)
        restored.chat = AsyncMock(side_effect=self.model)
        bot.ai = restored
        restarted = MainChat(bot)
        bot._main_coordinator = restarted
        restarted.contexts[key] = source
        self.enterContext(patch.object(restarted, "start"))
        self.enterContext(patch.object(restarted, "poll", new_callable=AsyncMock))
        await self.tick(restarted, 1000)
        restored.chat.assert_not_awaited()
        await self.fixture.receive(bot, False, "after-restart", "回来继续聊", elapsed=1001)
        await self.tick(restarted, 1062)
        self.assertEqual(restored.chat.await_count, 2)
        self.assertEqual(restored.get_or_create_session(key).chat_state["self_activity_wait_count"], 1)

    async def test_waits_in_other_internal_events_do_not_change_the_idle_streak(self):
        bot, key, coordinator = await self.prepare()
        await self.tick(coordinator, 61)
        await self.tick(coordinator, 122)
        for flags in ({}, {"activity_review": True}, {"memory_maintenance": True},
                      {"agent_event": {"namespace": coordinator.namespace, "session_key": key,
                                       "scope": coordinator.contexts[key].scope}}):
            with self.subTest(flags=flags):
                await coordinator.chat(key, "内部事件", idle_event=True,
                    activity_scope=coordinator.contexts[key].scope, **flags)
                self.assertEqual(bot.ai.get_or_create_session(key).chat_state["self_activity_wait_count"], 2)
        await self.tick(coordinator, 183)
        self.assertEqual(bot.ai.get_or_create_session(key).chat_state["self_activity_wait_count"], 3)

    async def test_model_failure_is_not_a_wait_decision_and_does_not_resume_suppressed_activity(self):
        bot, key, coordinator = await self.prepare()
        self.actions.extend(("wait", "wait", "error", "wait"))
        for number, expected in enumerate((1, 2, 2, 3), 1):
            await self.tick(coordinator, number * 61)
            self.assertEqual(bot.ai.get_or_create_session(key).chat_state["self_activity_wait_count"], expected)
        await self.tick(coordinator, 305)
        self.assertEqual(bot.ai.chat.await_count, 4)
