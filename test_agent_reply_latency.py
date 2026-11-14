"""Offline reproductions of notification mix-ups, slow receipts and lost replies."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from agent_transcript import TranscriptReader
from antigravity_client import AgentServiceError, AntigravityClient
from chat_turns import ChatTurns
from messaging import OutboundText
from onebot_client import OneBotClient
from test_ai_fixture import drain_runtime


def inbound(step, marker, text="question"):
    return {"step_index": step, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE",
            "content": f"<SYSTEM_MESSAGE>\n[Message] sender=system content={marker}\n{text}\n</SYSTEM_MESSAGE>"}


def notification(step):
    row = inbound(step, "", "Task id 59 finished: background search")
    row["content"] = row["content"].replace("sender=system", "sender=fixture/task-59")
    return row


def final(step, text):
    return {"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": text}


class TranscriptReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "transcript.jsonl"

    def test_partial_utf8_json_is_retained_until_complete(self):
        data = (json.dumps(final(1, "完整中文回复"), ensure_ascii=False) + "\n").encode()
        cut = data.index("中".encode()) + 1
        self.path.write_bytes(data[:cut])
        reader = TranscriptReader(self.path)
        self.assertEqual(reader.read(), [])
        with self.path.open("ab") as stream:
            stream.write(data[cut:])
        self.assertEqual(reader.read(), [final(1, "完整中文回复")])
        self.assertEqual(reader.read(), [final(1, "完整中文回复")])

    def test_no_newline_and_subsequent_append_do_not_duplicate_a_reply(self):
        self.path.write_text(json.dumps(final(1, "one")), encoding="utf-8")
        reader = TranscriptReader(self.path)
        self.assertEqual(reader.read(), [final(1, "one")])
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("\ninvalid\n[]\n" + json.dumps(final(2, "two")) + "\n")
        self.assertEqual(reader.read(), [final(1, "one"), final(2, "two")])

    def test_rewritten_and_truncated_log_never_keeps_stale_final(self):
        self.path.write_text(json.dumps(final(1, "old")) + "\n", encoding="utf-8")
        reader = TranscriptReader(self.path)
        self.assertEqual(reader.read()[0]["content"], "old")
        stamp = self.path.stat().st_mtime_ns
        self.path.write_text(json.dumps(final(1, "new")) + "\n", encoding="utf-8")
        os.utime(self.path, ns=(stamp + 1_000_000, stamp + 1_000_000))
        self.assertEqual(reader.read(), [final(1, "new")])
        self.path.write_bytes(b"")
        self.assertEqual(reader.read(), [])

    def test_unchanged_history_is_not_parsed_again(self):
        self.path.write_text("\n".join(json.dumps(final(n, "history")) for n in range(1000)) + "\n", encoding="utf-8")
        reader = TranscriptReader(self.path)
        reader.read()
        with patch("agent_transcript.json.loads", wraps=json.loads) as loads:
            for _ in range(10):
                self.assertEqual(len(reader.read()), 1000)
        self.assertLessEqual(loads.call_count, 10)


class AgentReplyLatencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.ai = AntigravityClient(sessions_file=str(root / "sessions.json"))
        self.ai._is_ready = True
        self.ai._brain_dir = str(root / "brain")
        self.ai._cancel_native_conversation = AsyncMock()
        self.ai._ensure_conversation_present = AsyncMock()
        self.key = "onebot_group_fixture"
        self.session = self.ai.get_or_create_session(self.key)
        self.session.conversation_id = "fixture"
        self.path = root / "brain/fixture/.system_generated/logs/transcript.jsonl"
        self.path.parent.mkdir(parents=True)

    def write(self, *rows):
        self.path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    def append(self, *rows):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")

    async def ask(self, **options):
        return await asyncio.wait_for(self.ai.chat(self.key, "本次图片问题", bind_response=True, **options), 1.5)

    async def test_background_completion_before_our_input_cannot_steal_its_reply(self):
        self.write(final(60, "old"))
        async def command(args, **options):
            marker = args[-1].splitlines()[0]
            self.append(notification(63), final(64, "已获悉后台搜索任务完成。"))
            await asyncio.sleep(.025)
            self.append(inbound(65, marker), final(66, "突然发这么惬意满足的表情。"))
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        trace = []
        self.assertEqual(await self.ask(response_trace=trace), "突然发这么惬意满足的表情。")
        self.assertEqual([row["content"] for row in trace if row["source"] == "MODEL"], ["突然发这么惬意满足的表情。"])
        self.ai._run_agentapi_cmd.assert_awaited_once()

    async def test_notification_after_current_reply_does_not_replace_it(self):
        self.write(inbound(1, "current"), final(2, "第一段"), final(3, "第二段"),
                   notification(4), final(5, "后台任务回执"))
        result = await self.ai._wait_for_model_response("fixture", request_marker="current", timeout=1)
        self.assertEqual(result, "第一段\n\n第二段")

    async def test_late_previous_final_is_not_the_new_turn_even_without_a_notification(self):
        self.write(final(1, "previous"))
        async def command(args, **options):
            self.append(final(2, "上一轮的迟到回复"))
            await asyncio.sleep(.025)
            self.append(inbound(3, args[-1].splitlines()[0]), final(4, "当前问题的回复"))
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        self.assertEqual(await self.ask(), "当前问题的回复")

    async def test_tool_progress_and_repeated_step_are_not_duplicate_final_bubbles(self):
        tool = final(3, "检索中")
        tool["tool_calls"] = [{"name": "search_web"}]
        self.write(inbound(1, "current"), final(2, "之前的进度文字"), tool,
                   final(4, "完整结果"), final(4, "完整结果"), final(5, "补充解释"))
        result = await self.ai._wait_for_model_response("fixture", request_marker="current", timeout=1)
        self.assertEqual(result, "完整结果\n\n补充解释")

    async def test_bound_response_still_waits_when_latest_step_calls_a_tool(self):
        tool = final(3, "")
        tool["tool_calls"] = [{"name": "search_web"}]
        self.write(inbound(1, "current"), final(2, "先前的进度"), tool)
        async def complete():
            await asyncio.sleep(.025)
            self.append(final(4, "实际最终结果"))
        writer = asyncio.create_task(complete())
        try:
            result = await self.ai._wait_for_model_response("fixture", request_marker="current", timeout=1)
            self.assertEqual(result, "实际最终结果")
        finally:
            await writer

    async def test_completed_text_does_not_wait_for_a_slow_send_command_exit(self):
        reaped = asyncio.Event()
        async def command(args, **options):
            self.write(inbound(1, args[-1].splitlines()[0]), final(2, "正文早已完成"))
            try:
                await asyncio.Event().wait()
            finally:
                reaped.set()
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        result = await asyncio.wait_for(self.ask(), .6)
        self.assertEqual(result, "正文早已完成")
        self.assertTrue(reaped.is_set())
        self.ai._run_agentapi_cmd.assert_awaited_once()
        self.ai._cancel_native_conversation.assert_not_awaited()
        self.assertEqual(self.session.conversation_id, "fixture")

    async def test_slow_metadata_query_does_not_hold_up_newly_completed_text(self):
        self.ai._CONVERSATION_CHECK_DELAY = 0
        checking, reaped = asyncio.Event(), asyncio.Event()
        async def metadata(*args, **options):
            checking.set()
            try:
                await asyncio.Event().wait()
            finally:
                reaped.set()
        self.ai._ensure_conversation_present.side_effect = metadata
        self.write(inbound(1, "current"))
        task = asyncio.create_task(self.ai._wait_for_model_response("fixture", request_marker="current", timeout=1))
        try:
            await asyncio.wait_for(checking.wait(), .6)
            self.append(final(2, "查询尚未返回也应读取的正文"))
            self.assertEqual(await asyncio.wait_for(task, .6), "查询尚未返回也应读取的正文")
            self.assertTrue(reaped.is_set())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_disk_read_does_not_block_the_message_event_loop(self):
        self.write(final(1, "正文"))
        released, overlapped = threading.Event(), []
        read = TranscriptReader.read
        def slow_read(reader):
            overlapped.append(released.wait(.5))
            return read(reader)
        async def heartbeat():
            await asyncio.sleep(.025)
            released.set()
        tick = asyncio.create_task(heartbeat())
        try:
            with patch.object(TranscriptReader, "read", slow_read):
                self.assertEqual(await self.ai._wait_for_model_response("fixture", timeout=1), "正文")
        finally:
            released.set()
            await tick
        self.assertEqual(overlapped, [True])

    async def test_actual_backend_error_message_is_not_silent_empty_output(self):
        async def command(args, **options):
            self.write(inbound(1, args[-1].splitlines()[0]),
                       {"step_index": 2, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "DONE",
                        "content": "model output must contain either output text or tool calls"}, final(3, ""))
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        with self.assertRaises(AgentServiceError):
            await self.ask(allow_empty_response=True)

    async def test_unrelated_backend_error_before_input_does_not_kill_current_reply(self):
        async def command(args, **options):
            self.write({"step_index": 1, "source": "SYSTEM", "type": "ERROR_MESSAGE", "content": "old failure"},
                       inbound(2, args[-1].splitlines()[0]), final(3, "正常回复"))
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        self.assertEqual(await self.ask(), "正常回复")

    async def test_wait_flag_finishes_without_any_model_final_and_keeps_conversation(self):
        waiting = False
        async def command(args, **options):
            nonlocal waiting
            self.write(inbound(1, args[-1].splitlines()[0]))
            waiting = True
            await asyncio.Event().wait()
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        self.assertEqual(await self.ask(stop_requested=lambda: waiting), "")
        self.ai._cancel_native_conversation.assert_awaited_once_with("fixture")
        self.assertEqual(self.session.conversation_id, "fixture")

    async def test_missing_bound_conversation_can_still_be_rebuilt_once(self):
        async def command(args, **options):
            if args[0] == "send-message":
                raise RuntimeError("trajectory not found: fixture")
            marker = args[-1].splitlines()[0]
            path = self.path.parent.parent.parent.parent / "replacement/.system_generated/logs/transcript.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(inbound(0, marker)) + "\n" + json.dumps(final(1, "重建后的回复")) + "\n", encoding="utf-8")
            return {"response": {"newConversation": {"conversationId": "replacement"}}}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        self.assertEqual(await self.ask(), "重建后的回复")
        self.assertEqual([call.args[0][0] for call in self.ai._run_agentapi_cmd.await_args_list], ["send-message", "new-conversation"])

    async def test_another_input_cannot_supply_the_missing_current_final(self):
        self.write(inbound(1, "current"), inbound(2, "different"), final(3, "别人的答案"))
        with self.assertRaisesRegex(RuntimeError, "另一条输入打断"):
            await self.ai._wait_for_model_response("fixture", request_marker="current", timeout=1)

    async def test_deleted_before_input_was_logged_is_still_detected(self):
        from antigravity_client import ConversationMissingError
        self.write({"step_index": 1, "type": "SYSTEM_ERROR", "content": "trajectory not found: fixture"})
        with self.assertRaises(ConversationMissingError):
            await self.ai._wait_for_model_response("fixture", request_marker="not_logged", timeout=1)

    async def test_controller_marker_is_never_returned_as_qq_dialogue(self):
        async def command(args, **options):
            marker = args[-1].splitlines()[0]
            self.write(inbound(1, marker), final(2, marker + "\n真实台词"))
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        self.assertEqual(await self.ask(), "真实台词")


class ReplyDeliveryLatencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_later_independent_question_from_same_person_preserves_unquoted_old_reply(self):
        for official in (False, True):
            bot, _ = self.fixture.bot(official, stored_cid="fixture")
            if not official:
                bot._is_ws_connected = lambda: True
                bot.send_group_message = OneBotClient.send_group_message.__get__(bot)
            async def chat(key, prompt, **options):
                bot._chat_turns.observe(bot._get_main_coordinator().contexts[key].scope, "independent", "456", "另一个独立问题",
                                        self.fixture.baseline + 30)
                return "原问题的完整回答"
            bot.ai.chat.side_effect = chat
            event = await self.fixture.receive(bot, official, "original", "原问题")
            if official:
                self.assertNotIn("message_reference", event.reply.await_args.kwargs)
            else:
                packet = bot.send_action.await_args.args[1]
                self.assertFalse(any(segment["type"] == "reply" for segment in packet["message"]))
                self.assertEqual(packet["message"][-1]["data"]["text"], "原问题的完整回答")

    async def test_offline_independent_question_does_not_delete_saved_reply(self):
        bot, _ = self.fixture.bot(stored_cid="fixture")
        connected = False
        bot._is_ws_connected = lambda: connected
        bot.send_group_message = OneBotClient.send_group_message.__get__(bot)
        bot._get_runtime()
        scope = bot._image_scope("12345678")
        bot._chat_turns.observe(scope, "original", "456", "原问题", received_at=100)
        await bot.send_group_message("12345678", OutboundText("原问题回复", reply_to="original", reply_sender="456"))
        bot._chat_turns.observe(scope, "independent", "456", "独立问题", received_at=130)
        connected = True
        bot._text_outbox.wake()
        await bot._text_outbox.worker
        packet = bot.send_action.await_args.args[1]
        self.assertFalse(any(segment["type"] == "reply" for segment in packet["message"]))
        self.assertEqual(bot._runtime.store.db.execute("SELECT state FROM outbox").fetchone()[0], "accepted")

    async def test_supplement_after_first_sent_bubble_does_not_truncate_the_answer(self):
        bot, _ = self.fixture.bot(stored_cid="fixture")
        bot._is_ws_connected = lambda: True
        bot.send_group_message = OneBotClient.send_group_message.__get__(bot)
        bot.ai.chat.return_value = "第一段。\n第二段。\n第三段。"
        calls = []
        async def send(action, params):
            calls.append(params)
            if len(calls) == 1:
                bot._chat_turns.observe(bot._image_scope("12345678"), "continued", "456", "新的补充",
                                        self.fixture.baseline + 1)
            return {"status": "ok", "retcode": 0, "data": {"message_id": "accepted"}}
        bot.send_action.side_effect = send
        await self.fixture.receive(bot, False, "original", "请解释一下")
        self.assertEqual([packet["message"][-1]["data"]["text"] for packet in calls], ["第一段。", "第二段。", "第三段。"])

    async def test_wait_tool_releases_real_adapter_and_next_message_can_reply(self):
        bot, key = self.fixture.bot(stored_cid="fixture")
        # Measure adapter wait/release after the one-time local schema setup.
        bot._get_main_coordinator()
        bot._get_runtime()
        ai = bot.ai
        ai.chat = AntigravityClient.chat.__get__(ai)
        ai._is_ready = True
        ai._brain_dir = str(Path("brain").resolve())
        ai._cancel_native_conversation = AsyncMock()
        path = Path(ai._brain_dir) / "fixture/.system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True)
        count = 0
        async def command(args, **options):
            nonlocal count
            count += 1
            marker = args[-1].splitlines()[0]
            if count == 1:
                path.write_text(json.dumps(inbound(1, marker)) + "\n", encoding="utf-8")
                context_file = list(bot._get_main_coordinator().requests)[-1]
                broker, token = bot._get_main_coordinator().broker.from_context(context_file)
                broker.wait_for_message(token)
                await asyncio.Event().wait()
            from console_tools.cli import execute
            execute("reply", list(bot._get_main_coordinator().requests)[-1], text="现在听完整了。", finish=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(inbound(2, marker)) + "\n" + json.dumps(final(3, "现在听完整了。")) + "\n")
            return {}
        ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        await asyncio.wait_for(self.fixture.receive(bot, False, "fragment", "因为我觉得"), 1.5)
        bot.send_group_message.assert_not_awaited()
        ai._cancel_native_conversation.assert_awaited_once_with("fixture", kill_background_tasks=False)
        await asyncio.wait_for(self.fixture.receive(bot, False, "finish", "第一种方案更合适", elapsed=1), 1.5)
        bot.send_group_message.assert_awaited_once()
        self.assertEqual(str(bot.send_group_message.await_args.args[1]), "现在听完整了。")
        self.assertEqual(ai.get_or_create_session(key).conversation_id, "fixture")

    async def test_busy_queue_does_not_merge_questions_outside_the_speech_window(self):
        bot, _ = self.fixture.bot(stored_cid="fixture")
        from conversation_runtime import EventContext
        from types import SimpleNamespace
        runtime = bot._get_runtime()
        entered, release = asyncio.Event(), asyncio.Event()
        async def busy():
            entered.set()
            await release.wait()
        scope = bot._image_scope("12345678")
        runtime.submit_chat(EventContext(scope, "busy"), busy)
        await asyncio.wait_for(entered.wait(), 1)
        clock = 10
        with patch("conversation_runtime.asyncio.get_running_loop", return_value=SimpleNamespace(time=lambda: clock)):
            runtime.submit_chat(EventContext(scope, "one", owner="456", text="第一个问题"), AsyncMock(), coalesce=True)
            clock = 40
            runtime.submit_chat(EventContext(scope, "two", owner="456", text="另一个问题"), AsyncMock(), coalesce=True)
        self.assertEqual([item.event.message_id for item in runtime.inboxes[scope]], ["one", "two"])
        release.set()
        await drain_runtime(bot)

    def test_evicted_source_is_quoted_conservatively_but_never_silently_cancelled(self):
        turns = ChatTurns()
        for n in range(300):
            turns.observe("group", str(n), "456", "消息", received_at=n)
        self.assertTrue(turns.newer("group", "0"))
        self.assertFalse(turns.superseded("group", "0", "456"))
