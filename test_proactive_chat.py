"""Offline scheduling, stale-topic cancellation and durable chat configuration."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import os
from test_ai_fixture import ConsoleAgentMock

from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from config import AppConfig
import test_startup_participation as fixtures


class ProactiveChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()
        self.now = self.fixture.baseline

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def bot(self, official=False):
        bot, key = self.fixture.bot(official, stored_cid="preserved-main-conversation")
        bot.app_config.bot.proactive_chat_enabled = True
        bot.app_config.bot.proactive_interval_minutes = 1
        bot.app_config.bot.never_sleep = True
        if not official:
            bot._is_running = True
            bot.ws = SimpleNamespace(closed=False)
        bot.ai.chat = ConsoleAgentMock(return_value='{"should_reply":true,"action":"none","reply":"最近有什么好玩的游戏？"}')
        return bot, key

    async def tick(self, bot, elapsed):
        with patch("time.time", return_value=self.now + elapsed):
            bot._proactive_chat.tick()
            if bot._runtime:
                await asyncio.gather(*list(bot._runtime.chat_workers.values()))

    async def test_idle_topic_uses_main_session_and_sends_once_per_interval(self):
        bot, key = self.bot()
        bot._proactive_chat.observe("12345678", "source", received_at=self.now)
        await self.tick(bot, 59)
        bot.ai.chat.assert_not_awaited()
        await self.tick(bot, 60)
        bot.ai.chat.assert_awaited_once()
        self.assertEqual(bot.ai.chat.call_args.args[0], key)
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "preserved-main-conversation")
        bot.send_action.assert_awaited_once()
        payload = bot.send_action.call_args.args[1]
        self.assertEqual(payload["group_id"], 12345678)
        self.assertEqual([seg["type"] for seg in payload["message"]], ["text"])
        self.assertIn("最近有什么好玩的游戏", bot.context_buffer.format_for_prompt("12345678"))
        await self.tick(bot, 119)
        bot.send_action.assert_awaited_once()
        await self.tick(bot, 120)
        self.assertEqual(bot.send_action.await_count, 2)

    async def test_sleep_pauses_idle_topics_and_new_message_wakes_without_mention(self):
        bot, _ = self.bot()
        bot.app_config.bot.never_sleep = False
        bot.app_config.bot.idle_sleep_minutes = 1
        bot._proactive_chat.observe("12345678", "first", received_at=self.now)
        await self.tick(bot, 61)
        bot.ai.chat.assert_not_awaited()
        message = await self.fixture.receive(bot, False, "wakeup", "大家继续聊吧", elapsed=62)
        bot.ai.chat.assert_awaited_once()
        self.assertFalse(bot._proactive_chat.groups["12345678"].quiet)

    async def test_no_observed_group_disabled_and_disconnected_do_not_send(self):
        bot, _ = self.bot()
        await self.tick(bot, 1000)
        bot.ai.chat.assert_not_awaited()
        bot._proactive_chat.observe("12345678", "source", received_at=self.now)
        for field, value in (("autonomous_enabled", False), ("proactive_chat_enabled", False)):
            cfg = bot.app_config.bot
            previous = getattr(cfg, field)
            setattr(cfg, field, value)
            await self.tick(bot, 1000)
            setattr(cfg, field, previous)
        bot.ws.closed = True
        await self.tick(bot, 1000)
        bot.ai.chat.assert_not_awaited()
        bot.send_action.assert_not_awaited()

    async def test_idle_topics_use_their_own_interval_despite_legacy_speech_limits(self):
        bot, _ = self.bot()
        bot.app_config.bot.reply_probability = 0
        bot.app_config.bot.cooldown_seconds = 3600
        bot._proactive_chat.observe("12345678", "source", received_at=self.now)
        await self.tick(bot, 59)
        bot.send_action.assert_not_awaited()
        await self.tick(bot, 60)
        bot.send_action.assert_awaited_once()
        await self.tick(bot, 61)
        bot.send_action.assert_awaited_once()

    async def test_new_inbound_cancels_generation_without_killing_chat_worker(self):
        bot, _ = self.bot()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def generate(key, prompt, **_kwargs):
            if "群聊空闲定时事件" not in prompt:
                return '{"should_reply":false,"reason":"本轮先听群友说"}'
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        bot.ai.chat.side_effect = generate
        bot._proactive_chat.observe("12345678", "old", received_at=self.now)
        with patch("time.time", return_value=self.now + 60):
            bot._proactive_chat.tick()
            await asyncio.wait_for(started.wait(), 2)
            bot._queue_event({"post_type": "message", "message_type": "group", "group_id": 12345678,
                "message_id": "new", "user_id": 456, "raw_message": "现在讨论一下这个问题", "sender": {"nickname": "群友"}})
            await asyncio.wait_for(asyncio.gather(*list(bot._runtime.chat_workers.values())), 2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(bot.ai.chat.await_count, 2)
        bot.send_action.assert_not_awaited()
        self.assertEqual(bot._proactive_chat.groups["12345678"].message_id, "new")

    async def test_queued_idle_topic_does_not_send_after_new_message_or_clear(self):
        for invalidate in ("message", "clear"):
            bot, _ = self.bot()
            bot._proactive_chat.observe("12345678", "old", received_at=self.now)
            with patch("time.time", return_value=self.now + 60):
                bot._proactive_chat.tick()
                if invalidate == "message":
                    bot._proactive_chat.observe("12345678", "new")
                else:
                    bot._cancel_all_media()
                await asyncio.gather(*list(bot._runtime.chat_workers.values()))
            bot.ai.chat.assert_not_awaited()
            bot.send_action.assert_not_awaited()

    async def test_quiet_request_is_directed_and_expires_on_next_group_message(self):
        bot, _ = self.bot()
        bot._proactive_chat.observe("12345678", "one", received_at=self.now)
        bot._proactive_chat.note_quiet("12345678", "闭嘴", directed=False)
        self.assertFalse(bot._proactive_chat.groups["12345678"].quiet)
        bot._proactive_chat.note_quiet("12345678", "我在思考", directed=True)
        self.assertFalse(bot._proactive_chat.groups["12345678"].quiet)
        bot._proactive_chat.note_quiet("12345678", "先闭嘴", directed=True)
        await self.tick(bot, 60)
        bot.ai.chat.assert_not_awaited()
        bot._proactive_chat.observe("12345678", "two", received_at=self.now + 61)
        await self.tick(bot, 121)
        bot.send_action.assert_awaited_once()

    async def test_model_decline_and_transport_failure_retry_only_next_interval(self):
        bot, _ = self.bot()
        bot._proactive_chat.observe("12345678", "source", received_at=self.now)
        bot.ai.chat.return_value = '{"should_reply":false,"reason":"稍后再聊"}'
        await self.tick(bot, 60)
        await self.tick(bot, 61)
        bot.ai.chat.assert_awaited_once()
        bot.ai.chat.return_value = '{"should_reply":true,"reply":"聊个新话题"}'
        bot.send_action.side_effect = OSError("fixture disconnected")
        await self.tick(bot, 120)
        await self.tick(bot, 121)
        self.assertEqual(bot.ai.chat.await_count, 2)
        self.assertNotIn("12345678", bot._last_autonomous_reply_time)
        self.assertIsNone(bot._text_outbox.worker)

    async def test_shutdown_or_disable_cancels_running_topic(self):
        for shutdown in (False, True):
            bot, _ = self.bot()
            entered = asyncio.Event()

            async def generate(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()

            bot.ai.chat.side_effect = generate
            bot._proactive_chat.observe("12345678", "source", received_at=self.now)
            with patch("time.time", return_value=self.now + 60):
                bot._proactive_chat.tick()
                await asyncio.wait_for(entered.wait(), 2)
                if shutdown:
                    await bot._close_runtime()
                    self.fixture.bots.remove(bot)
                else:
                    bot.app_config.bot.proactive_chat_enabled = False
                    bot._proactive_chat.tick()
                    await asyncio.gather(*list(bot._runtime.chat_workers.values()))
            bot.send_action.assert_not_awaited()

    async def test_official_topic_needs_live_reply_window_and_shares_sequence(self):
        bot, _ = self.bot(True)
        bot.ai.chat.return_value = '{"should_reply":false,"reason":"先听"}'
        message = await self.fixture.receive(bot, True, "source", "大家最近在玩什么", elapsed=0)
        bot.ai.chat.return_value = '{"should_reply":true,"reply":"最近有什么好玩的游戏？"}'
        await self.tick(bot, 60)
        message.reply.assert_awaited_once_with(content="最近有什么好玩的游戏？", msg_seq=1)
        await self.tick(bot, 301)
        message.reply.assert_awaited_once()

    async def test_groups_are_isolated_and_self_echo_does_not_wake(self):
        bot, _ = self.bot()
        bot._proactive_chat.observe("12345678", "one", received_at=self.now)
        bot._proactive_chat.observe("87654321", "two", received_at=self.now + 30)
        await self.tick(bot, 60)
        self.assertEqual(bot.send_action.call_args.args[1]["group_id"], 12345678)
        bot._queue_event({"post_type": "message", "message_type": "group", "group_id": 34567890,
                          "message_id": "echo", "user_id": 999, "raw_message": "机器人自身的回声"})
        self.assertNotIn("34567890", bot._proactive_chat.groups)


class WakeConfigTests(unittest.TestCase):
    def test_config_gui_roundtrip_and_zero_auxiliary_timeout(self):
        from PySide6.QtWidgets import QApplication
        from ui.main_window import BotStudioGUI
        import tempfile
        app = QApplication.instance() or QApplication([])
        previous = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="qqbot_wake_gui_") as directory:
            os.chdir(directory)
            window = None
            try:
                config = AppConfig()
                config.bot.reply_probability = 0
                config.bot.cooldown_seconds = 3600
                config.save("config.yaml")
                window = BotStudioGUI(config_path="config.yaml")
                tab = window.config_tab_widget
                self.assertFalse(hasattr(tab, "spin_reply_prob"))
                self.assertFalse(hasattr(tab, "spin_cooldown"))
                tab.chk_never_sleep.setChecked(False)
                tab.spin_idle_sleep.setValue(45)
                tab.chk_proactive_chat.setChecked(True)
                tab.spin_proactive_interval.setValue(7)
                tab.chk_self_activity.setChecked(True)
                tab.spin_self_activity_interval.setValue(23)
                tab.spin_session_timeout.setValue(0)
                window._on_save_config(show_msg=False)
                loaded = AppConfig.load("config.yaml")
                self.assertEqual(loaded.bot.reply_probability, 0)
                self.assertEqual(loaded.bot.cooldown_seconds, 3600)
                self.assertFalse(loaded.bot.never_sleep)
                self.assertEqual(loaded.bot.idle_sleep_minutes, 45)
                self.assertTrue(loaded.bot.proactive_chat_enabled)
                self.assertEqual(loaded.bot.proactive_interval_minutes, 7)
                self.assertTrue(loaded.bot.self_activity_enabled)
                self.assertEqual(loaded.bot.self_activity_interval_minutes, 23)
                self.assertEqual(loaded.antigravity.session_timeout_minutes, 0)
                tab.load_config(loaded)
                self.assertFalse(tab.chk_never_sleep.isChecked())
                self.assertTrue(tab.spin_idle_sleep.isEnabled())
                self.assertTrue(tab.chk_self_activity.isChecked())
                self.assertEqual(tab.spin_self_activity_interval.value(), 23)
            finally:
                if window:
                    window.close()
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
