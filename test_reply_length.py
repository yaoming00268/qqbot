"""Adaptive answer length and complete, naturally separated QQ replies."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import hashlib
import unittest

from chat_turns import plain_reply
from main_chat import MAIN_RULES
from messaging import split_text
from onebot_client import OneBotClient
from prompt_builder import compose_autonomous_decision_prompt, compose_system_prompt, compose_user_prompt


SCREENSHOT = ("收到收到，信号非常清晰哦前辈！突然用求救信号测试吓了我一跳，还以为出什么事了呢……"
    "不管什么时候，只要前辈需要，小光都会一直陪着前辈的！……唔、等等，总之测试通过啦！")


class ReplyLengthPolicyTests(unittest.TestCase):
    def test_single_bubble_setting_does_not_limit_whole_answer(self):
        for limit in (100, 1200):
            with self.subTest(limit=limit):
                prompt = compose_system_prompt("自然简短口语", limit, include_skills=False)
                self.assertIn(f"单条消息分段阈值为 {limit} 字", prompt)
                self.assertIn("把必要信息讲完整", prompt)
                self.assertIn("确认、测试、接梗和简单问答", prompt)
                self.assertIn("重复保证陪伴", prompt)
                self.assertNotIn("单次回复总字数必须严格控制", prompt)
                self.assertNotIn("严禁输出冗长长文", prompt)
                self.assertNotIn("推荐回复长度在", prompt)

    def test_short_group_rhythm_does_not_override_request_for_detail_in_legacy_prompts(self):
        for include in (True, False):
            for compose in (compose_user_prompt, compose_autonomous_decision_prompt):
                with self.subTest(include=include, compose=compose.__name__):
                    prompt = compose("详细解释原因并给出完整步骤和例子", max_reply_length=100,
                        include_preset=include, chat_rhythm={"avg_length": 3, "suggested_max": 15})
                    self.assertNotIn("总字数绝对不可超过", prompt)
                    self.assertNotIn("严格控制在 100 字以内", prompt)
                    self.assertIn("单条分段阈值", prompt)
                    self.assertIn("详细解释原因并给出完整步骤和例子", prompt)


class ReplyFormattingTests(unittest.TestCase):
    def contents(self, text, limit=100):
        return [item["text"] for item in plain_reply(text, limit).messages]

    def test_screenshot_paragraph_becomes_natural_bubbles_without_deleting_dialogue(self):
        parts = self.contents(SCREENSHOT)
        self.assertGreaterEqual(len(parts), 2)
        self.assertEqual("".join(parts), SCREENSHOT)
        self.assertTrue(all(len(part) <= 100 for part in parts))
        self.assertTrue(all(not part.startswith("，") for part in parts))

    def test_short_answer_is_not_padded_or_forced_into_several_messages(self):
        self.assertEqual(self.contents("收到了，测试通过。"), ["收到了，测试通过。"])

    def test_plain_lines_are_independent_bubbles_even_without_blank_lines(self):
        lines = ["收到了。", "刚才是在测试呀。", "消息没问题。", "可以继续聊。"]
        self.assertEqual(self.contents("\n".join(lines)), lines)

    def test_more_than_three_paragraphs_keep_their_own_bubbles(self):
        paragraphs = [f"这是第{i}个独立意思。" for i in range(1, 9)]
        self.assertEqual(self.contents("\n\n".join(paragraphs)), paragraphs)

    def test_long_answer_preserves_every_paragraph_and_final_conclusion(self):
        paragraphs = [f"原因{i}：" + "这里需要解释条件和具体步骤。" * 25 for i in range(6)] + ["最终结论：所有步骤均需完成。"]
        parts = self.contents("\n\n".join(paragraphs))
        self.assertGreater(len(parts), 5)
        self.assertEqual("".join(parts), "".join(paragraphs))
        self.assertTrue(all(len(part) <= 100 for part in parts))
        self.assertEqual(parts[-1], paragraphs[-1])

    def test_explanatory_paragraph_is_not_reduced_to_chat_sized_sentences(self):
        paragraph = "这个步骤需要保存原数据，并检查恢复后的结果。" * 22
        parts = self.contents(paragraph, 1200)
        self.assertEqual(parts, [paragraph])

    def test_lists_and_tables_stay_together_instead_of_one_bubble_per_line(self):
        for block in ("依次执行：\n1. 保存设置。\n2. 重启服务。\n3. 检查结果。",
                      "| 项目 | 结果 |\n| --- | --- |\n| 测试 | 通过 |",
                      "# 操作步骤\n先保存。\n再重启。"):
            with self.subTest(block=block):
                self.assertEqual(self.contents(block, 1200), [block])

    def test_code_blank_lines_indentation_and_inline_text_are_preserved(self):
        code = '```python\nif ready:\n    print("成功！继续。")\n\n    finish()\n```'
        self.assertEqual(self.contents("按这样执行：\n\n" + code), ["按这样执行：", code])
        text = "版本是 1.25，使用 `a!=b` 判断。" + "说明下一步怎么操作。" * 6
        parts = self.contents(text)
        self.assertTrue(any("1.25" in part and "`a!=b`" in part for part in parts))
        self.assertEqual("".join(parts), text)

    def test_default_transport_split_has_no_five_message_truncation(self):
        text = "保留这一句。" * 100
        parts = split_text(text, 50)
        self.assertGreater(len(parts), 5)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(part.endswith("。") for part in parts))

    def test_urls_are_not_cut_at_sentence_punctuation_or_transport_limit(self):
        url = "https://example.com/" + "verylong/" * 30 + "?v=1.25&ok=true"
        text = "资料地址：" + url + "\n请查看原文。"
        parts = self.contents(text)
        self.assertTrue(any(url in part for part in parts))
        self.assertEqual("".join(parts), text.replace("\n", ""))


class ReplyLengthIngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def onebot(self):
        bot, key = self.fixture.bot(stored_cid="existing-main")
        bot.app_config.bot.max_reply_length = 100
        bot._is_ws_connected = lambda: True
        bot.send_group_message = OneBotClient.send_group_message.__get__(bot)
        return bot, key

    def texts(self, bot):
        return [call.args[1]["message"][-1]["data"]["text"] for call in bot.send_action.await_args_list
                if call.args[0] == "send_group_msg"]

    async def test_multiple_chat_lines_are_sent_separately_in_one_model_turn(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, _ = self.fixture.bot(official, stored_cid="existing-main")
                lines = ["收到了。", "刚才是在测试呀。", "消息正常。", "继续聊吧。"]
                bot.ai.chat.return_value = "\n".join(lines)
                event = await self.fixture.receive(bot, official, "short-lines", "测试一下收到了吗")
                calls = event.reply.await_args_list if official else bot.send_group_message.await_args_list
                self.assertEqual(len(calls), 4)
                texts = [call.kwargs["content"] if official else str(call.args[1]) for call in calls]
                self.assertEqual(texts, lines)
                bot.ai.chat.assert_awaited_once()

    async def test_onebot_delivers_full_long_answer_past_five_bubbles_and_records_tail(self):
        bot, key = self.onebot()
        text = "\n\n".join(f"步骤{i}：" + "先检查条件，然后完成这一项。" * 12 for i in range(8)) + "\n\n完整结论：最后检查数据一致。"
        bot.ai.chat.return_value = text
        await self.fixture.receive(bot, False, "long-answer", "请完整解释这个流程，列出所有步骤")
        parts = self.texts(bot)
        self.assertGreater(len(parts), 5)
        self.assertEqual("".join(parts), text.replace("\n", ""))
        self.assertTrue(all(len(part) <= 100 for part in parts))
        session = bot.ai.get_or_create_session(key)
        self.assertEqual(session.chat_state["last_delivery"]["segments_sent"], len(parts))
        self.assertIn("最后检查数据一致", bot._last_chat_replies[next(iter(bot._last_chat_replies))])
        bot.ai.chat.assert_awaited_once()

    async def test_offline_keeps_all_bubbles_then_delivers_in_order(self):
        bot, key = self.onebot()
        connected = [False]
        bot._is_ws_connected = lambda: connected[0]
        runtime = bot._get_runtime()
        lines = [f"第{i}条需要保留的内容。" for i in range(7)]
        bot.ai.chat.return_value = "\n\n".join(lines)
        await self.fixture.receive(bot, False, "offline-reply", "请把这些要点完整说一下")
        bot.send_action.assert_not_awaited()
        rows = runtime.store.db.execute("SELECT state FROM outbox").fetchall()
        self.assertEqual([row[0] for row in rows], ["pending"] * len(lines))
        delivery = bot.ai.get_or_create_session(key).chat_state["last_delivery"]
        self.assertEqual(delivery["state"], "queued")
        self.assertEqual(delivery["segments_queued"], len(lines))
        connected[0] = True
        bot._text_outbox.wake()
        await asyncio.wait_for(bot._text_outbox.worker, 2)
        self.assertEqual(self.texts(bot), lines)
        bot.ai.chat.assert_awaited_once()

    async def test_new_message_during_multiple_sends_does_not_quote_following_bubbles(self):
        bot, _ = self.onebot()
        lines = ["第一句回答。", "第二句说明。", "最后一个要点。"]
        bot.ai.chat.return_value = "\n".join(lines)
        async def send(action, params):
            bot._chat_turns.observe(bot._image_scope("12345678"), "interruption", "789", "别人插话")
            return {"status": "ok", "data": {"message_id": "receipt"}}
        bot.send_action.side_effect = send
        await self.fixture.receive(bot, False, "source", "请解释一下")
        self.assertEqual(self.texts(bot), lines)
        packets = [call.args[1]["message"] for call in bot.send_action.await_args_list]
        self.assertFalse(any(seg["type"] == "reply" for seg in packets[0]))
        for packet in packets[1:]:
            self.assertFalse(any(seg["type"] == "reply" for seg in packet))

    async def test_official_merges_excess_bubbles_to_fit_reply_slots_without_losing_text(self):
        bot, _ = self.fixture.bot(True, stored_cid="existing-main")
        bot.app_config.bot.max_reply_length = 100
        text = "\n\n".join(f"完整要点{i}：" + "解释条件并给出做法。" * 8 for i in range(9))
        bot.ai.chat.return_value = text
        event = await self.fixture.receive(bot, True, "official-long", "请详细解释每一个要点")
        calls = event.reply.await_args_list
        self.assertEqual(len(calls), 5)
        sent = "".join(call.kwargs["content"] for call in calls)
        self.assertEqual(sent.replace("\n", ""), text.replace("\n", ""))
        self.assertEqual([call.kwargs["msg_seq"] for call in calls], [1, 2, 3, 4, 5])
        bot.ai.chat.assert_awaited_once()

    async def test_existing_chat_updates_old_length_rules_once_without_recreation(self):
        bot, key = self.onebot()
        session = bot.ai.get_or_create_session(key)
        session.chat_state.update(conversation_id=session.conversation_id, rules_hash="old-fingerprint",
                                  protocol_hash="old-protocol")
        bot.ai.chat.return_value = "收到了。"
        await self.fixture.receive(bot, False, "rules-update", "先测试一下")
        first = bot.ai.chat.await_args.args[1]
        self.assertIn("没有固定句数或段数上限", first)
        self.assertNotIn("单次回复总字数必须严格控制", first)
        await self.fixture.receive(bot, False, "next", "然后详细说明", elapsed=1)
        self.assertNotIn("@rules:", bot.ai.chat.await_args.args[1])
        self.assertEqual(session.chat_state["protocol_hash"], hashlib.sha256(MAIN_RULES.encode()).hexdigest())
        self.assertEqual(session.conversation_id, "existing-main")

