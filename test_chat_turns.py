"""Offline checks for plain replies, burst speech and actual send-time quoting."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import asyncio
import contextlib
import io
import json
from test_ai_fixture import prompt_object
from pathlib import Path
import unittest

from chat_turns import plain_reply
from console_tools.cli import execute
from console_tools.server import handle
from conversation_runtime import current_event
from onebot_client import OneBotClient
from test_ai_fixture import drain_runtime

QT_APP = None


class PlainReplyTests(unittest.TestCase):
    def test_plain_paragraphs_need_no_json_and_keep_all_content(self):
        text = "第一段。\n\n第二段也直接说话。\n\n" + "长" * 120
        decision = plain_reply(text, 40, sender_id="a", message_id="m")
        self.assertTrue(decision.should_reply)
        self.assertEqual("".join(item["text"] for item in decision.messages), text.replace("\n", ""))
        self.assertTrue(all(len(item["text"]) <= 40 for item in decision.messages))
        self.assertTrue(all(item["action"] == "none" for item in decision.messages))

    def test_screenshot_truncated_protocol_is_never_a_chat_message(self):
        for text in ('{"should_reply":true,"messages":[{"text":"好嘛！既然是你亲口决定的',
                     '```json\n{"should_reply":true,"messages":[',
                     '下面是回复：\n{"should_reply":true,"messages":[',
                     '{"should_reply":true}', '{"should_reply":false,"reply":"不应发送"}'):
            with self.subTest(text=text):
                self.assertFalse(plain_reply(text, 1200).should_reply)

    def test_complete_old_messages_are_unwrapped_without_following_model_quote_actions(self):
        decision = plain_reply('{"should_reply":true,"messages":[{"text":"第一段","action":"quote"},'
                              '{"text":"第二段","action":"at"}]}', 1200)
        self.assertEqual([item["text"] for item in decision.messages], ["第一段", "第二段"])
        self.assertEqual([item["action"] for item in decision.messages], ["none", "none"])

    def test_fenced_code_and_ordinary_words_are_not_silence_controls(self):
        text = '不要保持沉默。\n\n```python\nx = 1\n\nprint(x)\n```'
        decision = plain_reply(text, 1200)
        self.assertEqual(len(decision.messages), 2)
        self.assertIn('x = 1\n\nprint(x)', decision.messages[1]["text"])


class ChatTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def bot(self):
        bot, key = self.fixture.bot(stored_cid="existing-main")
        bot.app_config.bot.chat_settle_seconds = .025
        bot.app_config.bot.chat_max_wait_seconds = .1
        bot._is_ws_connected = lambda: True
        bot.send_group_message = OneBotClient.send_group_message.__get__(bot)
        return bot, key

    def event(self, mid, text, user=456, group=12345678):
        return {"post_type": "message", "message_type": "group", "group_id": group, "user_id": user,
                "message_id": mid, "raw_message": text, "sender": {"nickname": "群友" + str(user)}}

    def packets(self, bot):
        return [call.args[1] for call in bot.send_action.await_args_list if call.args[0] == "send_group_msg"]

    async def test_fragment_burst_is_one_model_turn_with_all_messages_and_latest_source(self):
        bot, _ = self.bot()
        bot.ai.chat.return_value = "原来你是在说吃醋呀。\n\n我听明白了。"
        for mid, text in (("one", "女儿"), ("two", "居然"), ("three", "对我找男性吃醋")):
            bot._queue_event(self.event(mid, text))
        await drain_runtime(bot)
        bot.ai.chat.assert_awaited_once()
        prompt = bot.ai.chat.await_args.args[1]
        self.assertEqual(prompt_object(prompt, "turn")["turn"]["source_message_id"], "three")
        for text in ("女儿", "居然", "对我找男性吃醋"):
            self.assertIn(text, prompt)
        self.assertEqual(len(self.packets(bot)), 2)
        self.assertTrue(all(not any(s["type"] == "reply" for s in p["message"]) for p in self.packets(bot)))
        transcript = bot.context_buffer.get_recent_messages("12345678", 30)
        self.assertTrue(all(any(row["content"] == text for row in transcript) for text in ("女儿", "居然", "对我找男性吃醋")))

    async def test_model_wait_command_suppresses_text_and_next_event_resumes(self):
        for official in (False, True):
            bot, _ = self.fixture.bot(official, stored_cid="existing-main")
            async def chat(key, prompt, **options):
                context = list(bot._get_main_coordinator().requests)[-1]
                response = handle({"id": 1, "method": "tools/call", "params": {"name": "qqbot_wait_for_message",
                    "arguments": {"context_file": context}}})["result"]
                self.assertFalse(response["isError"])
                return "这段文字也不应发送"
            bot.ai.chat.side_effect = chat
            first = await self.fixture.receive(bot, official, "fragment", "因为我觉得")
            if official:
                first.reply.assert_not_awaited()
            else:
                bot.send_group_message.assert_not_awaited()
            bot.ai.chat.side_effect = None
            bot.ai.chat.return_value = "现在听完整了。"
            second = await self.fixture.receive(bot, official, "finish", "还是第一种方案更合适", elapsed=1)
            if official:
                second.reply.assert_awaited_once()
            else:
                bot.send_group_message.assert_awaited_once()

    async def test_first_fragment_mention_is_preserved_when_followups_have_no_mention(self):
        bot, _ = self.bot()
        bot.app_config.bot.autonomous_enabled = False
        bot.ai.chat.return_value = "听完整句再回复"
        bot._queue_event(self.event("mention", "[CQ:at,qq=999] 我想问"))
        bot._queue_event(self.event("finish", "这个设置怎么改？"))
        await drain_runtime(bot)
        bot.ai.chat.assert_awaited_once()
        self.assertEqual(len(self.packets(bot)), 1)
        self.assertIn("我想问", bot.ai.chat.await_args.args[1])

    async def test_same_speaker_continues_during_analysis_queues_both_replies(self):
        bot, _ = self.bot()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def chat(key, prompt, **options):
            calls.append(prompt)
            if len(calls) == 1:
                entered.set()
                await release.wait()
                return "第一轮完整回复"
            return "根据完整意思回复"
        bot.ai.chat.side_effect = chat
        bot._queue_event(self.event("first", "居然"))
        await asyncio.wait_for(entered.wait(), 2)
        bot._queue_event(self.event("continued", "对我找男性吃醋"))
        release.set()
        await drain_runtime(bot)
        self.assertEqual(len(self.packets(bot)), 2)
        self.assertEqual([packet["message"][-1]["data"]["text"] for packet in self.packets(bot)],
                         ["第一轮完整回复", "根据完整意思回复"])
        self.assertNotIn('"previous_delivery"', calls[1])

    async def test_new_message_from_another_user_keeps_actual_source_without_implicit_quote(self):
        bot, _ = self.bot()
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def chat(key, prompt, **options):
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                await release.wait()
                return "这是对第一条的回复"
            return ""
        bot.ai.chat.side_effect = chat
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bot._queue_event(self.event("source", "原始问题"))
            await asyncio.wait_for(entered.wait(), 2)
            bot._queue_event(self.event("newer", "另一个人的发言", user=789))
            release.set()
            await drain_runtime(bot)
        packet = self.packets(bot)[0]
        self.assertEqual([part["type"] for part in packet["message"]], ["text"])
        actions = [json.loads(line[len("[FEED_ACTION] "):]) for line in output.getvalue().splitlines() if line.startswith("[FEED_ACTION] ")]
        replies = [row for row in actions if row.get("reply")]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["source_message_id"], "source")
        self.assertEqual(replies[0]["source_text"], "原始问题")
        self.assertFalse(replies[0]["quoted"])

    async def test_slow_model_without_new_messages_does_not_quote_or_mention(self):
        bot, _ = self.bot()
        bot.ai.chat.return_value = "直接回复"
        bot._queue_event(self.event("source", "[CQ:at,qq=999] 原始问题"))
        await drain_runtime(bot)
        self.assertEqual([s["type"] for s in self.packets(bot)[0]["message"]], ["text"])

    async def test_official_transport_does_not_quote_when_newer_input_exists(self):
        bot, key = self.fixture.bot(True, stored_cid="existing-main")
        async def chat(key, prompt, **options):
            scope = f"official:{bot.app_config.qq.appid}:group:12345678"
            bot._chat_turns.observe(scope, "newer", "789", "另一人发言")
            return "回答原始问题"
        bot.ai.chat.side_effect = chat
        event = await self.fixture.receive(bot, True, "original", "原始问题")
        self.assertNotIn("message_reference", event.reply.await_args.kwargs)
        bot.ai.chat.side_effect = None
        bot.ai.chat.return_value = "直接说话"
        last = await self.fixture.receive(bot, True, "last", "最后的问题", elapsed=1)
        self.assertNotIn("message_reference", last.reply.await_args.kwargs)

    async def test_offline_reply_preserves_no_quote_when_actually_flushed(self):
        from messaging import OutboundText
        bot, key = self.bot()
        connected = False
        bot._is_ws_connected = lambda: connected
        bot._get_runtime()
        scope = bot._image_scope("12345678")
        bot._chat_turns.observe(scope, "source", "456", "问题")
        await bot.send_group_message("12345678", OutboundText("缓存回复", reply_to="source", reply_sender="456"))
        self.assertFalse(self.packets(bot))
        bot._chat_turns.observe(scope, "later", "789", "其他群友发言")
        connected = True
        bot._text_outbox.wake()
        await bot._text_outbox.worker
        self.assertEqual([part["type"] for part in self.packets(bot)[0]["message"]], ["text"])

    async def test_offline_obsolete_reply_is_cancelled_after_same_speaker_supplements(self):
        from messaging import OutboundText
        bot, _ = self.bot()
        connected = False
        bot._is_ws_connected = lambda: connected
        bot._get_runtime()
        scope = bot._image_scope("12345678")
        bot._chat_turns.observe(scope, "source", "456", "前半句")
        await bot.send_group_message("12345678", OutboundText("过时答复", reply_to="source", reply_sender="456"))
        bot._chat_turns.observe(scope, "continued", "456", "后半句")
        connected = True
        bot._text_outbox.wake()
        await bot._text_outbox.worker
        self.assertFalse(self.packets(bot))
        self.assertEqual(bot._runtime.store.db.execute("SELECT state FROM outbox").fetchone()[0], "cancelled")

    async def test_completed_empty_chat_response_is_silence_without_retry(self):
        from antigravity_client import AntigravityClient
        ai = AntigravityClient(sessions_file="empty-test.json")
        ai._brain_dir = str(Path("brain").resolve())
        path = Path(ai._brain_dir) / "conversation" / ".system_generated" / "logs" / "transcript.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                                   "step_index": 1, "content": ""}) + "\n", encoding="utf-8")
        self.assertEqual(await asyncio.wait_for(ai._wait_for_model_response("conversation", timeout=2,
                                               allow_empty_response=True), .5), "")

    async def test_burst_wait_is_bounded_and_priority_control_bypasses_it(self):
        bot, _ = self.bot()
        runtime = bot._get_runtime()
        runtime.settle_seconds, runtime.max_wait_seconds = 2, .1
        entered = asyncio.Event()
        async def handle(data):
            entered.set()
        bot._handle_group_message = handle
        async def produce():
            for index in range(12):
                bot._queue_event(self.event(str(index), "还在继续"))
                await asyncio.sleep(.05)
        task = asyncio.create_task(produce())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertFalse(task.done())
        finally:
            await task
            await drain_runtime(bot)
        entered.clear()
        runtime.settle_seconds = 2
        bot._queue_event(self.event("priority", "[CQ:at,qq=999] /status"))
        await asyncio.wait_for(entered.wait(), .5)

    async def test_console_sidebar_retains_reply_source(self):
        global QT_APP
        from PySide6.QtWidgets import QApplication
        from ui.components.monitor_sidebar import GroupChatMonitorWidget
        QT_APP = QApplication.instance() or QApplication([])
        widget = GroupChatMonitorWidget()
        try:
            widget.handle_feed_event("action", {"group_id": "g", "action": "quote", "reply": "答复",
                "source_message_id": "source-id", "source_text": "原消息内容", "quoted": True})
            self.assertEqual(widget._all_messages[-1]["source_message_id"], "source-id")
            self.assertIn("原消息内容", widget.feed_browser.toPlainText())
            self.assertIn("source-id", widget.feed_browser.toPlainText())
        finally:
            widget.close()
            from shiboken6 import delete
            delete(widget)  # Dispose the QObject tree on the GUI thread before async worker GC.
            QT_APP.processEvents()
