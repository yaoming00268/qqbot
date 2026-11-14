"""Speaker-aware archives and changes-only inputs, without live model calls."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from conversation_records import dialogue_records
from idle_memory import IdleMemory, SCHEMA_VERSION
from memory_digest import record_chunks, summarize_records
from test_ai_fixture import prompt_object
import test_startup_participation as fixtures


class DialogueRecordTests(unittest.TestCase):
    def test_legacy_messages_keep_speakers_and_unicode_but_drop_protocol_tails(self):
        event = {"type": "message", "id": "q", "sender": "小甲", "user_id": "123",
                 "text": "想看音乐讨论\n也想看看游戏 🎵"}
        rows = [{"time": 100, "event": json.dumps(event, ensure_ascii=False) +
                 "\n\n" + json.dumps({"rules": "旧预设" * 6000})},
                {"event": '{"type":"task_result","answer":"内部报告"}'},
                {"event": '{"type":"self_activity"}'},
                {"event": '@rules: local/rules.md'}, {"event": '{"type":'}]
        result = dialogue_records(rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["author_id"], "123")
        self.assertEqual(result[0]["author_name"], "小甲")
        self.assertEqual(result[0]["content"], event["text"])
        self.assertEqual(result[0]["timestamp"], 100)
        self.assertNotIn("旧预设", json.dumps(result, ensure_ascii=False))

    def test_buffer_and_batch_aliases_keep_identity_and_richer_metadata(self):
        rows = [{"id": "q", "sender_id": "a", "text": "看这个？"},
                {"id": "q", "user_id": "a", "sender_name": "小甲", "text": "看这个？", "reply_to": "p"},
                {"event_id": "buffer-id", "author_openid": "b", "author_name": "小乙", "content": "好呀"}]
        result = dialogue_records(rows)
        self.assertEqual(len(result), 2)
        self.assertEqual((result[0]["author_name"], result[0]["reply_to"]), ("小甲", "p"))
        self.assertEqual((result[1]["message_id"], result[1]["author_id"]), ("buffer-id", "b"))

    def test_uncertain_bot_output_and_internal_events_are_not_spoken_facts(self):
        result = dialogue_records([
            *({"author_id": "bot", "is_bot": True, "content": state, "delivery_state": state}
              for state in ("queued", "unknown", "failed", "cancelled")),
            {"author_id": "bot", "is_bot": True, "content": "已实际发送", "delivery_state": "accepted",
             "reply_to": "q", "source_message_id": "q"},
            {"type": "task_result", "text": "后台日志"}, {"event": "遗留的无署名消息"}])
        self.assertEqual([row["content"] for row in result], ["已实际发送", "遗留的无署名消息"])
        self.assertEqual(result[0]["role"], "assistant")
        self.assertEqual(result[1]["role"], "unknown")

    def test_short_exchanges_share_batches_and_long_fragments_keep_their_speaker(self):
        records = dialogue_records([{"id": str(i), "sender_id": "a" if i % 2 else "b",
            "sender_name": "小甲" if i % 2 else "小乙", "text": "讨论同一个话题"} for i in range(30)])
        chunks = list(record_chunks(records))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 30)
        content = "多行内容\n🎵" * 3500
        fragments = [row for chunk in record_chunks([
            {**records[0], "content": content, "reply_to": "parent"}], budget=1600) for row in chunk]
        self.assertEqual("".join(row["content"] for row in fragments), content)
        self.assertTrue(all(row["author_id"] == "b" and row["reply_to"] == "parent" for row in fragments))


class SemanticIdleMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = IdleMemory(self.temp.name, 2)

    def test_v1_digest_is_rebuilt_once_from_original_without_reusing_bad_summary(self):
        event = json.dumps({"type": "message", "id": "q", "user_id": "a", "sender": "甲", "text": "新结论"})
        source = json.dumps({"generation": 2, "event": event + "\n技术预设"}) + "\n"
        self.memory.journal.write_text(source, encoding="utf-8")
        self.memory.path.write_text(json.dumps({"generation": 2, "summary_complete": True,
            "summary": "旧的逐句无署名摘要", "cursor_bytes": len(source.encode())}), encoding="utf-8")
        old_signature = {key: value for key, value in self.memory.signature().items() if key != "schema_version"}
        self.memory.progress.write_text(json.dumps({"signature": old_signature}), encoding="utf-8")
        self.assertIsNone(self.memory.reference())
        snapshot, previous, records = self.memory.begin()
        self.assertEqual(previous, {})
        self.assertEqual(records[0]["author_name"], "甲")
        self.memory.finish(snapshot, {"summary": "甲形成了新结论"}, "same-main-cid")
        restored = IdleMemory(self.temp.name, 2)
        self.assertFalse(restored.pending())
        self.assertIsNotNone(restored.reference())
        self.assertEqual(restored.journal.read_text(encoding="utf-8"), source)

    def test_new_records_resume_cursor_and_cannot_mix_other_generations(self):
        IdleMemory(self.temp.name, 1).append([{"user_id": "x", "text": "其他世代"}])
        self.memory.append([{"id": "q", "user_id": "a", "sender": "甲", "text": "问题"}])
        snapshot, _, records = self.memory.begin()
        self.assertEqual([row["content"] for row in records], ["问题"])
        self.memory.finish(snapshot, {"summary": "甲提问", "participants": {"a": {"nickname": "甲"}}}, "cid")
        self.memory.append([{"id": "r", "is_bot": True, "content": "回答", "reply_to": "q"}])
        _, previous, records = self.memory.begin()
        self.assertEqual([row["content"] for row in records], ["回答"])
        self.assertEqual(previous["participants"]["a"]["nickname"], "甲")
        self.assertEqual(records[0]["reply_to"], "q")
        self.assertEqual(previous["schema_version"], SCHEMA_VERSION)


class DialogueSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_batches_preserve_context_and_accumulate_real_participants(self):
        prompts = []
        session = SimpleNamespace(conversation_id="restored-summary-cid", chat_state={})
        async def generate(key, prompt, **options):
            payload = json.loads(prompt.split("\n", 1)[1])
            prompts.append(payload)
            self.assertIn("不能逐句改写", options["system_prompt"])
            self.assertNotIn("400", prompt)
            return '{"summary":"甲提出配乐问题，乙补充，机器人答应继续找资料。","participants":{"fake":{}}}'
        ai = SimpleNamespace(chat=AsyncMock(side_effect=generate),
            get_or_create_session=lambda _: session, _save_sessions=MagicMock())
        records = dialogue_records([
            {"id": "q", "user_id": "a", "sender": "甲", "text": "配乐怎么样？"},
            {"id": "b", "user_id": "b", "sender": "乙", "text": "我喜欢开场曲", "reply_to": "q"},
            {"id": "r", "is_bot": True, "author_name": "小白", "content": "我再找找访谈", "reply_to": "q"}])
        result = await summarize_records(ai, "summary", records, source="main_chat")
        self.assertEqual(ai.chat.await_count, 1)
        self.assertEqual(len(prompts[0]["records"]), 3)
        self.assertEqual(prompts[0]["records"][2]["reply_to"], "q")
        self.assertEqual(set(result["digest"]["participants"]), {"a", "b", "bot"})
        result = await summarize_records(ai, "summary", [{"author_id": "c", "author_name": "丙", "content": "后来加入"}],
            previous=result["digest"], source="main_chat")
        self.assertEqual(set(result["digest"]["participants"]), {"a", "b", "bot", "c"})
        self.assertEqual(result["digest"]["participants"]["bot"]["role"], "assistant")
        first, second = [call.args[1].split("\n", 1)[0] for call in ai.chat.await_args_list]
        self.assertIn("不能逐句改写", first)
        self.assertEqual(second, "空闲整理事件")
        self.assertIn("summary_rules_hash", session.chat_state)


class MainDialogueInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        # These tests inspect incremental input, not the shutdown summary actor.
        for bot in self.fixture.bots:
            bot.app_config.bot.memory_enabled = False
        await self.fixture.asyncTearDown()

    async def test_questions_and_accepted_bot_replies_reach_one_speaker_aware_archive(self):
        for official in (False, True):
            bot, key = self.fixture.bot(official, stored_cid="existing")
            bot.app_config.bot.memory_enabled = True
            bot.ai.chat.return_value = '{"should_reply":true,"reply":"我喜欢钢琴开场，稍后看看访谈。"}'
            await self.fixture.receive(bot, official, "q", "你觉得这首配乐怎么样？")
            coordinator = bot._get_main_coordinator()
            memory = IdleMemory(coordinator.directory(key), 0)
            _, _, records = memory.begin()
            self.assertEqual([row["role"] for row in records], ["user", "assistant"])
            self.assertEqual(records[0]["author_id"], "456")
            self.assertEqual(records[0]["author_name"], "Fixture user")
            self.assertEqual(records[1]["reply_to"], "q")
            self.assertEqual(records[1]["delivery_state"], "accepted")
            self.assertNotIn("console_context_file", memory.journal.read_text(encoding="utf-8"))

    async def test_private_reply_is_archived_and_uncertain_sends_are_excluded(self):
        bot, _ = self.fixture.bot(stored_cid="existing")
        bot.app_config.bot.memory_enabled = True
        bot.send_private_message = AsyncMock(return_value={"status": "ok"})
        bot.ai.chat.return_value = '{"should_reply":true,"reply":"我去看看这个话题。"}'
        await bot._handle_private_message({"user_id": 456, "message_id": "private", "raw_message": "聊聊配乐",
            "message": [{"type": "text", "data": {"text": "聊聊配乐"}}], "sender": {"nickname": "小甲"}})
        coordinator = bot._get_main_coordinator()
        key = bot.ai.chat.await_args.args[0]
        records = IdleMemory(coordinator.directory(key), 0).begin()[2]
        self.assertEqual([row["role"] for row in records], ["user", "assistant"])
        for state in ("queued", "unknown", "failed", "cancelled"):
            bot.send_group_message = AsyncMock(return_value={"status": state})
            await self.fixture.receive(bot, False, state, "这一次发送回执是什么？", elapsed=1)
        group_key = bot.ai.chat.await_args.args[0]
        group_records = IdleMemory(coordinator.directory(group_key), 0).begin()[2]
        self.assertFalse(any(row["is_bot"] for row in group_records))

    async def test_failed_later_bubble_keeps_only_the_earlier_accepted_reply(self):
        from messaging import SendUnknown
        bot, key = self.fixture.bot(stored_cid="existing")
        bot.app_config.bot.memory_enabled = True
        bot.ai.chat.return_value = json.dumps({"should_reply": True,
            "messages": [{"text": "第一条成功"}, {"text": "第二条不确定"}]}, ensure_ascii=False)
        bot.send_group_message.side_effect = [{"status": "ok"}, SendUnknown("lost receipt")]
        await self.fixture.receive(bot, False, "q", "分开说说")
        coordinator = bot._get_main_coordinator()
        records = IdleMemory(coordinator.directory(key), 0).begin()[2]
        self.assertEqual([row["content"] for row in records if row["is_bot"]], ["第一条成功"])
        with coordinator.broker.connect() as db:
            states = [row[0] for row in db.execute("SELECT state FROM outgoing ORDER BY created")]
        self.assertEqual(states, ["accepted", "unknown"])

    async def test_local_archive_error_does_not_relabel_a_confirmed_send(self):
        bot, _ = self.fixture.bot(stored_cid="existing")
        bot.app_config.bot.memory_enabled = True
        bot.ai.chat.return_value = '{"should_reply":true,"reply":"实际发出"}'
        original = IdleMemory.append
        def append(memory, messages):
            if messages and messages[0].get("is_bot"):
                raise OSError("disk full")
            return original(memory, messages)
        with patch.object(IdleMemory, "append", new=append):
            await self.fixture.receive(bot, False, "q", "回一句")
        bot.send_group_message.assert_awaited_once()
        coordinator = bot._get_main_coordinator()
        with coordinator.broker.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM outgoing").fetchone()[0], "accepted")

    async def test_repeated_inputs_omit_empty_templates_and_unchanged_failure_feedback(self):
        bot, key = self.fixture.bot(stored_cid="existing")
        bot.ai.chat.return_value = '{"should_reply":false}'
        coordinator = bot._get_main_coordinator()
        feedback = [{"request_id": "failed-browser", "state": "failed", "goal": "看配乐讨论",
            "notes": [], "checkpoint": "确实无法打开页面", "launch": {"trace": "technical" * 2000},
            "startup": {"pid": 123}, "recovery": {"revision": 1, "state": "blocked", "code": "NO_WINDOW"}}]
        controllers = []
        def snapshots(session_key, scope, with_controllers=False, only_ids=None):
            result = deepcopy(feedback)
            if with_controllers:
                controllers.append(only_ids)
                for row in result:
                    row["controller_context_file"] = "existing-controller"
            return result
        with patch.object(coordinator, "activity_feedback", side_effect=snapshots):
            await self.fixture.receive(bot, False, "one", "你好")
            first = bot.ai.chat.await_args.args[1]
            self.assertNotIn('"launch":', first)
            self.assertNotIn('"startup":', first)
            self.assertIn("existing-controller", first)
            await self.fixture.receive(bot, False, "two", "接着聊", elapsed=1)
            second = bot.ai.chat.await_args.args[1]
            for redundant in ("reply_call", '"companion_state"', '"activity_feedback"', '"turn"', "@rules:"):
                self.assertNotIn(redundant, second)
            self.assertLess(len(second), 1000)
            self.assertEqual(controllers, [{"failed-browser"}])
            feedback[0]["checkpoint"] = "页面现已恢复"
            await self.fixture.receive(bot, False, "three", "再看看", elapsed=2)
            self.assertIn("页面现已恢复", bot.ai.chat.await_args.args[1])
            feedback.clear()
            await self.fixture.receive(bot, False, "four", "继续", elapsed=3)
            self.assertEqual(prompt_object(bot.ai.chat.await_args.args[1], "activity_feedback_removed")
                ["activity_feedback_removed"], ["failed-browser"])

    async def test_changed_companion_state_is_sent_once_and_retried_after_failed_turn(self):
        bot, _ = self.fixture.bot(stored_cid="existing")
        bot.ai.chat.return_value = '{"should_reply":false}'
        personal = {"reflection": "", "interests": [], "wishes": []}
        with patch("companion_life.state_for", side_effect=lambda *_: deepcopy(personal)):
            await self.fixture.receive(bot, False, "one", "你好")
            self.assertNotIn('"companion_state"', bot.ai.chat.await_args.args[1])
            personal["interests"] = ["想了解配乐"]
            bot.ai.chat.side_effect = RuntimeError("offline")
            await self.fixture.receive(bot, False, "failed", "听着呢", elapsed=1)
            self.assertIn("想了解配乐", bot.ai.chat.await_args.args[1])
            bot.ai.chat.side_effect = None
            await self.fixture.receive(bot, False, "retry", "再说说", elapsed=2)
            self.assertIn("想了解配乐", bot.ai.chat.await_args.args[1])
            await self.fixture.receive(bot, False, "same", "好呀", elapsed=3)
            self.assertNotIn('"companion_state"', bot.ai.chat.await_args.args[1])
            personal["interests"] = []
            await self.fixture.receive(bot, False, "clear", "聊下一个", elapsed=4)
            self.assertEqual(prompt_object(bot.ai.chat.await_args.args[1], "companion_state")["companion_state"], {})

    async def test_changed_feedback_is_acknowledged_only_after_a_successful_model_turn(self):
        bot, key = self.fixture.bot(stored_cid="existing")
        bot.ai.chat.return_value = '{"should_reply":false}'
        await self.fixture.receive(bot, False, "one", "你好")
        coordinator = bot._get_main_coordinator()
        feedback = [{"request_id": "r", "state": "failed", "checkpoint": "新的阻碍"}]
        with patch.object(coordinator, "activity_feedback", side_effect=lambda *_: deepcopy(feedback)):
            bot.ai.chat.side_effect = RuntimeError("offline")
            await self.fixture.receive(bot, False, "failed", "继续", elapsed=1)
            self.assertEqual(bot.ai.get_or_create_session(key).chat_state["activity_feedback_seen"], {})
            bot.ai.chat.side_effect = None
            await self.fixture.receive(bot, False, "retry", "再试试", elapsed=2)
            self.assertIn("新的阻碍", bot.ai.chat.await_args.args[1])
            await self.fixture.receive(bot, False, "same", "好", elapsed=3)
            self.assertNotIn('"activity_feedback"', bot.ai.chat.await_args.args[1])

    async def test_internal_turns_do_not_add_fake_dialogue_or_repeat_empty_personal_state(self):
        bot, key = self.fixture.bot(stored_cid="existing")
        bot.ai.chat.return_value = '{"should_reply":false}'
        await self.fixture.receive(bot, False, "q", "先聊到这里")
        coordinator = bot._get_main_coordinator()
        memory = IdleMemory(coordinator.directory(key), 0)
        original = memory.journal.read_bytes()
        await coordinator.chat(key, '{"type":"self_activity"}', idle_event=True, self_activity=True,
            activity_scope=coordinator.contexts[key].scope)
        prompt = bot.ai.chat.await_args.args[1]
        self.assertEqual(prompt_object(prompt, "internal_event")["internal_event"], "self_activity")
        self.assertNotIn('"companion_state"', prompt)
        self.assertNotIn('"reply_call"', prompt)
        self.assertEqual(memory.journal.read_bytes(), original)
