"""Offline ingress checks for startup participation and durable conversations."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import json
import os
from test_ai_fixture import ConsoleAgentMock

from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from antigravity_client import AntigravityClient
from bot_client import GeminiQQBot
from config import AppConfig
from group_memory import GroupMemoryManager
from onebot_client import OneBotClient
from persona_manager import PersonaManager
from prompt_builder import compose_autonomous_decision_prompt, parse_ai_decision_and_reply


class StartupParticipationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot_startup_participation_")
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        self.bots = []
        self.baseline = time.time()

    async def asyncTearDown(self):
        for bot in self.bots:
            await bot._close_runtime()
        os.chdir(self.previous)
        self.temp.cleanup()

    def bot(self, official=False, *, stored_cid=None):
        # Every fixture owns its own files, including the account-scoped session
        # file selected by OneBotClient.bind_account during construction.
        root = Path(f"fixture_{len(self.bots)}")
        root.mkdir()
        config = AppConfig()
        config.bot.memory_enabled = False
        config.bot.chat_settle_seconds = 0  # Burst timing has dedicated event-ingress tests.
        config.bot.chat_buffer_seconds = 0
        config.bot.search_enabled = False
        config.bot.autonomous_enabled = True
        config.bot.reply_probability = 0.2
        config.bot.cooldown_seconds = 15
        config.bot.mention_probability = 0
        config.bot.runtime_database_path = str(root / "runtime.sqlite3")
        config.media.database_path = str(root / "media.sqlite3")
        session_key = ("group_" if official else "onebot_group_") + "12345678"
        personas = PersonaManager(str(root / "personas.yaml"))
        from persona_isolation import scoped_session
        stub = SimpleNamespace(app_config=config, personas=personas)
        if not official:
            stub.account_key = config.onebot.active_account
            stub.send_action = None
            stub.self_id = "999"
        session_key = scoped_session(stub, session_key)
        sessions_file = root / "sessions.json"
        account = "" if official else config.onebot.active_account
        scoped_file = root / (f"sessions_{account}.json" if account else "sessions.json")
        if stored_cid:
            scoped_file.write_text(json.dumps({session_key: {
                "conversation_id": stored_cid,
                "model": "flash",
                "updated_at": self.baseline - 86400,
            }}), encoding="utf-8")
        ai = AntigravityClient(sessions_file=str(sessions_file), account_key=account)
        ai.chat = ConsoleAgentMock(return_value='{"should_reply":false,"reason":"暂不插话"}')
        memories = GroupMemoryManager(storage_dir=str(root / "memories"))
        with patch.object(ai, "reset_all_sessions", wraps=ai.reset_all_sessions) as reset:
            bot = (GeminiQQBot(config=config, antigravity_client=ai,
                       persona_manager=personas, memory_manager=memories)
                   if official else OneBotClient(config, ai, personas, memories))
            reset.assert_not_called()
        self.bots.append(bot)
        if official:
            bot._connection = MagicMock()
            bot._connection.state.robot = SimpleNamespace(id="999", member_openid="999", name="FixtureBot")
        else:
            bot.self_id = "999"
            bot.send_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "accepted"}})
            bot.send_group_message = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "accepted"}})
        return bot, session_key

    async def receive(self, bot, official, message_id, text, *, sender="456", elapsed=0):
        now = self.baseline + elapsed
        if official:
            message = SimpleNamespace(id=message_id, group_openid="12345678", content=text,
                author=SimpleNamespace(member_openid=sender, username="Fixture user", bot=False),
                mentions=[], attachments=None, message_reference=None,
                reply=AsyncMock(return_value={"id": f"receipt_{message_id}"}))
        else:
            message = {"post_type": "message", "message_type": "group", "self_id": 999,
                "group_id": 12345678, "user_id": int(sender), "message_id": message_id,
                "raw_message": text, "message": [{"type": "text", "data": {"text": text}}],
                "sender": {"nickname": "Fixture user"}, "time": now}
        with patch("time.time", return_value=now), patch("random.random", return_value=0.99):
            if official:
                await bot._handle_group_message(message, is_at_event=False)
            else:
                await bot._handle_group_message(message)
        return message

    def assert_not_sent(self, bot, official, message):
        if official:
            message.reply.assert_not_awaited()
        else:
            bot.send_group_message.assert_not_awaited()
            bot.send_action.assert_not_awaited()

    async def test_every_nonmention_is_assessed_after_model_declines(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, _ = self.bot(official)
                first = await self.receive(bot, official, "first", "大家觉得这件事怎么处理？")
                bot.ai.chat.assert_awaited_once()
                self.assert_not_sent(bot, official, first)
                second = await self.receive(bot, official, "second", "今天大家聊得挺热闹", elapsed=16)
                self.assertEqual(bot.ai.chat.await_count, 2)
                self.assert_not_sent(bot, official, second)
                self.assertTrue(bot.app_config.bot.autonomous_enabled)
                self.assertEqual(bot.app_config.bot.reply_probability, 0.2)
                self.assertEqual(bot.app_config.bot.cooldown_seconds, 15)

    async def test_legacy_zero_probability_does_not_silence_chat_but_autonomy_switch_does(self):
        for official in (False, True):
            for enabled, probability in ((False, 0.2), (True, 0.0)):
                with self.subTest(official=official, enabled=enabled, probability=probability):
                    bot, _ = self.bot(official)
                    bot.app_config.bot.autonomous_enabled = enabled
                    bot.app_config.bot.reply_probability = probability
                    bot.ai.chat.return_value = '{"should_reply":true,"reply":"可以一起聊聊"}'
                    message = await self.receive(bot, official, "disabled", "大家觉得这件事怎么处理？")
                    self.assertEqual(bot.ai.chat.await_count, int(enabled))
                    if enabled:
                        (message.reply if official else bot.send_group_message).assert_awaited_once()
                    else:
                        self.assert_not_sent(bot, official, message)

    async def test_random_reply_mention_does_not_override_ai_silence(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, _ = self.bot(official)
                bot.app_config.bot.mention_probability = 1
                message = await self.receive(bot, official, "mention_is_not_directed", "大家觉得这件事怎么处理？")
                bot.ai.chat.assert_awaited_once()
                self.assert_not_sent(bot, official, message)

    async def test_same_sender_followup_after_real_reply_is_assessed_without_forcing(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, _ = self.bot(official)
                bot.app_config.bot.reply_probability = 1
                bot.ai.chat.side_effect = ['{"should_reply":true,"reply":"我觉得可以先试一下"}',
                                           '{"should_reply":false,"reason":"无需继续插话"}']
                first = await self.receive(bot, official, "reply_first", "大家觉得这件事怎么处理？")
                if official:
                    first.reply.assert_awaited_once()
                else:
                    bot.send_group_message.assert_awaited_once()
                bot.app_config.bot.reply_probability = .2
                with patch("onebot_client.compose_autonomous_decision_prompt", wraps=compose_autonomous_decision_prompt) as compose, \
                     patch("prompt_builder.parse_ai_decision_and_reply", wraps=parse_ai_decision_and_reply) as parse:
                    followup = await self.receive(bot, official, "followup", "是吗", elapsed=3)
                    self.assertEqual(bot.ai.chat.await_count, 2)
                    if official:
                        self.assertFalse(parse.call_args.kwargs["is_at_bot"])
                        self.assertFalse(parse.call_args.kwargs["is_master"])
                        followup.reply.assert_not_awaited()
                    else:
                        self.assertFalse(compose.call_args.kwargs["must_reply"])
                        self.assertFalse(compose.call_args.kwargs["is_at_bot"])
                        bot.send_group_message.assert_awaited_once()

    async def test_ordinary_speech_and_other_sender_are_always_analyzed(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, _ = self.bot(official)
                bot.ai.chat.return_value = '{"should_reply":true,"reply":"这个可以先试一下"}'
                await self.receive(bot, official, "real_reply", "大家觉得这件事怎么处理？")
                await self.receive(bot, official, "ordinary", "我先去吃晚饭了", elapsed=16)
                await self.receive(bot, official, "different_sender", "是吗", sender="789", elapsed=17)
                self.assertEqual(bot.ai.chat.await_count, 3)

    async def test_legacy_probability_and_cooldown_never_interrupt_continuing_conversation(self):
        for official in (False, True):
            for probability in (0, .01, .2):
                with self.subTest(official=official, probability=probability):
                    bot, key = self.bot(official, stored_cid="continuous-chat")
                    bot.app_config.bot.reply_probability = probability
                    bot.app_config.bot.cooldown_seconds = 3600
                    bot.ai.chat.return_value = '{"should_reply":true,"reply":"聊聊这个吧"}'
                    for index, text in enumerate(("今天聊聊这个吧", "我在思考", "1", "是吗", "继续", "我也这么觉得")):
                        message = await self.receive(bot, official, str(index), text,
                            sender="789" if index % 2 else "456", elapsed=index * .1)
                        if official:
                            message.reply.assert_awaited_once()
                    self.assertEqual(bot.ai.chat.await_count, 6)
                    if not official:
                        self.assertEqual(bot.send_group_message.await_count, 6)
                    for call in bot.ai.chat.await_args_list:
                        self.assertEqual(call.args[0], key)
                        self.assertNotIn("本轮发言额度", call.args[1])
                        self.assertIn("自然承接", call.kwargs["system_prompt"])
                    self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "continuous-chat")

    async def test_declining_one_turn_does_not_block_next_turn_even_with_legacy_limits(self):
        for official in (False, True):
            bot, _ = self.bot(official)
            bot.app_config.bot.reply_probability = 0
            bot.app_config.bot.cooldown_seconds = 3600
            bot.ai.chat.side_effect = ['{"should_reply":false,"reason":"本轮无需回应"}',
                                       '{"should_reply":true,"reply":"可以接着讲"}']
            first = await self.receive(bot, official, "decline", "我先整理一下")
            self.assert_not_sent(bot, official, first)
            second = await self.receive(bot, official, "continue", "你觉得刚才说的怎么样", elapsed=.1)
            self.assertEqual(bot.ai.chat.await_count, 2)
            (second.reply if official else bot.send_group_message).assert_awaited_once()

    async def test_restored_conversation_survives_construction_without_model_warmup(self):
        cid = "1a831595-4664-4dbb-96f0-c75e163b8a01"
        for official in (False, True):
            with self.subTest(official=official):
                bot, key = self.bot(official, stored_cid=cid)
                self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, cid)
                bot.ai.chat.assert_not_awaited()

    async def test_persona_touch_and_content_change_preserve_cid_without_model_warmup(self):
        cid = "1a831595-4664-4dbb-96f0-c75e163b8a01"
        for official in (False, True):
            with self.subTest(official=official):
                bot, key = self.bot(official, stored_cid=cid)
                path = Path(bot.personas.storage_path)
                original = path.read_bytes()
                touched = path.stat().st_mtime + 2
                path.write_bytes(original)
                os.utime(path, (touched, touched))
                with patch.object(bot.ai, "reset_all_sessions", wraps=bot.ai.reset_all_sessions) as reset:
                    bot.sync_system_prompt()
                    reset.assert_not_called()
                    self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, cid)
                    changed = yaml.safe_load(original.decode("utf-8"))
                    active = bot.personas.get_active_persona().name
                    changed["active"] = active
                    changed["presets"][active]["prompt"] += "\n这是测试中新增的实际人设内容。"
                    path.write_text(yaml.safe_dump(changed, allow_unicode=True, sort_keys=False), encoding="utf-8")
                    os.utime(path, (touched + 2, touched + 2))
                    bot.sync_system_prompt()
                    reset.assert_not_called()
                    self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, cid)
                    self.assertIn("新增的实际人设内容", bot.ai.system_prompt)
                    bot.ai.chat.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
