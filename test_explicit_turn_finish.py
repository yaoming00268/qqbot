"""An explicit main-agent finish stops generation without truncating prior work."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from antigravity_client import AntigravityClient
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.contracts import capabilities
from console_tools.files import ConsoleFileError
from console_tools.server import handle, tools_schema
from test_agent_reply_latency import inbound, final
from test_ai_fixture import prompt_object
import test_startup_participation as fixtures


class FinishTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qq finish 中文 ' ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.payload = {"namespace": "account", "scope": "group:1", "session_key": "persona:1",
            "main_agent": True, "sender_id": "456", "is_master": True, "generation": 0,
            "is_group": True, "reply_sources": [{"id": "q", "sender_id": "456", "text": "问题"}],
            "self_activity_config": {"enabled": True}, "work_permissions": {"access": "all"}}

    def context(self, **changes):
        path = self.broker.issue_context({**self.payload, **changes}, self.root / "contexts")
        return path, ConsoleBroker.from_context(path)[1]

    def test_reply_and_finish_are_one_idempotent_transaction_without_speech_ready(self):
        context, token = self.context(speech_check_required=True, source_message_id="q", source_text="完整问题")
        task = execute("submit", context, task="独立工作")
        result = execute("reply", context, text=["直接回答", "补充一句"], finish=True)
        self.assertEqual(result["state"], "finished")
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(self.broker.reply_control(token)["mode"], "finished")
        self.assertEqual(execute("reply", context, text=["直接回答", "补充一句"], finish=True), result)
        self.assertEqual(execute("finish", context), result)
        self.broker.seal_messages(token)
        self.assertEqual(execute("reply", context, text=["直接回答", "补充一句"], finish=True), result)
        self.assertEqual(len(self.broker.staged_messages(token)), 2)
        self.assertEqual(self.broker.record(task["id"])["state"], "submitted")
        with self.assertRaises(ConsoleFileError):
            execute("reply", context, text="不同内容", finish=True)
        with self.assertRaises(ConsoleFileError):
            execute("submit", context, task="迟到工作")

    def test_combined_reply_failure_leaves_speech_and_turn_open_for_hold(self):
        context, token = self.context(speech_check_required=True, reply_char_limit=2,
                                      source_message_id="q", source_text="因为")
        with self.assertRaises(ConsoleFileError) as error:
            execute("reply", context, text="超出字数限制", finish=True)
        self.assertEqual(error.exception.code, "REPLY_LENGTH_EXCEEDED")
        self.assertEqual(self.broker.speech_status(token)["state"], "unchecked")
        self.assertEqual(self.broker.reply_control(token), {})
        self.assertEqual(self.broker.staged_messages(token), [])
        self.assertEqual(execute("speech", context, operation="hold")["state"], "waiting")

    def test_commit_finish_and_upgrade_of_the_same_reply_do_not_duplicate_output(self):
        context, token = self.context(speech_check_required=True)
        first = execute("message", context, text="第一条")
        second = execute("message", context, text="第二条")
        result = execute("commit", context, finish=True)
        self.assertEqual(result["state"], "finished")
        self.assertEqual(result["message_ids"], [first["id"], second["id"]])
        self.assertEqual(execute("commit", context, finish=True), result)
        other, other_token = self.context(speech_check_required=True)
        committed = execute("reply", other, text="先提交")
        self.assertEqual(execute("reply", other, text="先提交", finish=True)["message_ids"], committed["message_ids"])
        self.assertEqual(len(self.broker.staged_messages(other_token)), 1)
        self.assertEqual(self.broker.reply_control(other_token)["mode"], "finished")
        self.broker.seal_messages(token, cancel=True)
        with self.assertRaises(ConsoleFileError):
            execute("commit", context, finish=True)

    def test_cli_and_mcp_support_combined_reply_without_preflight_calls(self):
        context, token = self.context(speech_check_required=True)
        cli = Path(__file__).with_name("console_tools") / "cli.py"
        result = subprocess.run([sys.executable, str(cli), "reply", "--context-file", context,
                                 "--text", "一次提交的中文答复", "--finish"],
                                capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["state"], "finished")
        response = handle({"id": 1, "method": "tools/call", "params": {"name": "qqbot_cli",
            "arguments": {"action": "reply", "context_file": context,
                          "params": {"text": ["一次提交的中文答复"], "finish": True}}}})["result"]
        self.assertFalse(response["isError"])
        self.assertEqual(json.loads(response["content"][0]["text"]), receipt)
        self.assertEqual(len(self.broker.staged_messages(token)), 1)

    def test_reply_then_work_then_finish_preserves_output_and_background_requests(self):
        context, token = self.context()
        reply = execute("reply", context, text=["我去看看", "稍后再聊"])
        task = execute("submit", context, task="独立整理资料")
        agent = execute("agents", context, operation="create", params={"name": "资料员", "text": "查访谈"})
        execute("agents", context, operation="send", params={"agent_id": agent["id"], "text": "保留来源"})
        finished = execute("finish", context)
        self.assertEqual(finished, {"ok": True, "state": "finished", "message_ids": reply["message_ids"], "next_action": "none"})
        self.assertEqual(self.broker.reply_control(token)["mode"], "finished")
        self.assertEqual([row["text"] for row in self.broker.staged_messages(token)], ["我去看看", "稍后再聊"])
        self.assertEqual(self.broker.record(task["id"])["state"], "submitted")
        with self.broker.connect() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT state FROM agent_mail ORDER BY seq")], ["queued", "queued"])
        for action, kwargs in (("submit", {"task": "迟到任务"}), ("message", {"text": "迟到文字"}),
                ("agents", {"operation": "send", "params": {"agent_id": agent["id"], "text": "迟到信"}})):
            with self.subTest(action=action), self.assertRaises(ConsoleFileError):
                execute(action, context, **kwargs)
        with self.assertRaises(PermissionError):
            execute("companion", context, operation="wish", params={"text": "迟到愿望"})

    def test_finish_retries_are_idempotent_until_explicit_cancellation(self):
        context, token = self.context()
        reply = execute("reply", context, text="已提交")
        first = execute("finish", context)
        self.assertEqual(execute("finish", context), first)
        self.assertEqual(execute("reply", context, text="已提交"), reply)
        self.assertIsNone(self.broker.seal_messages(token, cancel=True, preserve_committed=True))
        self.broker.seal_messages(token)
        self.assertEqual(execute("finish", context), first)
        self.assertEqual(execute("commit", context), reply)
        with self.assertRaises(ConsoleFileError):
            execute("wait", context)
        self.broker.seal_messages(token, cancel=True)
        with self.assertRaises(ConsoleFileError):
            execute("finish", context)
        self.assertEqual(self.broker.staged_messages(token), [])

    def test_finish_never_sends_uncommitted_drafts(self):
        context, token = self.context()
        execute("message", context, text="草稿")
        with self.assertRaises(ConsoleFileError) as raised:
            execute("finish", context)
        self.assertEqual(raised.exception.code, "REPLY_NOT_COMMITTED")
        self.assertEqual(self.broker.reply_control(token), {})
        self.assertEqual(len(self.broker.staged_messages(token)), 1)
        execute("commit", context)
        self.assertEqual(execute("finish", context)["state"], "finished")

    def test_silent_finish_and_wait_keep_distinct_meanings(self):
        context, token = self.context()
        task = execute("submit", context, task="后台工作")
        self.assertEqual(execute("finish", context)["message_ids"], [])
        self.assertEqual(self.broker.record(task["id"])["state"], "submitted")
        self.assertEqual(self.broker.reply_control(token)["mode"], "finished")
        waiting, wait_token = self.context()
        execute("message", waiting, text="不该发出的草稿")
        self.assertEqual(execute("wait", waiting)["state"], "waiting")
        self.assertEqual(self.broker.staged_messages(wait_token), [])
        with self.assertRaises(ConsoleFileError):
            execute("finish", waiting)

    def test_finish_is_bound_to_the_current_token_and_cannot_close_the_next_turn(self):
        old, old_token = self.context()
        receipt = execute("finish", old)
        self.broker.seal_messages(old_token)
        new, new_token = self.context()
        other, other_token = self.context(scope="another-group", session_key="persona:2")
        self.assertEqual(execute("finish", old), receipt)
        self.assertEqual(self.broker.reply_control(new_token), {})
        self.assertEqual(self.broker.reply_control(other_token), {})
        execute("reply", new, text="新一轮仍能回复")
        execute("reply", other, text="另一个群仍能回复")

    def test_workers_and_controllers_cannot_finish_main_turns(self):
        for changes in ({"parent_request_id": "task"}, {"sandbox_session_id": "session"},
                {"agent_worker_id": "agent"}, {"activity_controller": True}):
            context, token = self.context(**changes)
            with self.subTest(changes=changes):
                self.assertNotIn("finish", capabilities({**self.payload, **changes}))
                with self.assertRaises(PermissionError):
                    execute("finish", context)
                with self.assertRaises(PermissionError):
                    self.broker.finish_turn(token)
                self.assertEqual(self.broker.reply_control(token), {})

    def test_main_internal_and_proactive_events_can_finish_without_chat_output(self):
        for changes in ({"idle_event": True}, {"idle_event": True, "self_activity": True},
                {"idle_event": True, "agent_event": True}, {"proactive_event": True}):
            context, token = self.context(**changes)
            with self.subTest(changes=changes):
                self.assertIn("finish", capabilities({**self.payload, **changes}))
                self.assertEqual(execute("finish", context)["state"], "finished")
                self.assertEqual(self.broker.staged_messages(token), [])

    def test_finish_serializes_with_a_parallel_task_submission(self):
        for _ in range(6):
            context, token = self.context()
            execute("reply", context, text="好")
            start = threading.Barrier(2)
            def submit():
                start.wait()
                try:
                    return execute("submit", context, task="并发提交")
                except ConsoleFileError:
                    return None
            def finish():
                start.wait()
                return execute("finish", context)
            with ThreadPoolExecutor(max_workers=2) as pool:
                task, end = pool.submit(submit), pool.submit(finish)
                accepted = task.result()
                self.assertEqual(end.result()["state"], "finished")
            with self.broker.connect() as db:
                rows = [dict(row) for row in db.execute("SELECT id,state FROM requests WHERE token=?", (token,))]
            self.assertEqual(rows, [{"id": accepted["id"], "state": "submitted"}] if accepted else [])

    def test_real_cli_and_mcp_expose_the_same_small_terminal_receipt(self):
        context, token = self.context()
        execute("reply", context, text="中文 ' 与引号")
        cli = Path(__file__).with_name("console_tools") / "cli.py"
        result = subprocess.run([sys.executable, str(cli), "finish", "--context-file", context],
            capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["state"], "finished")
        response = handle({"id": 1, "method": "tools/call", "params": {"name": "qqbot_cli",
            "arguments": {"action": "finish", "context_file": context}}})["result"]
        self.assertFalse(response["isError"])
        self.assertEqual(json.loads(response["content"][0]["text"]), receipt)
        tool = next(row for row in tools_schema() if row["name"] == "qqbot_cli")
        self.assertIn("finish", tool["inputSchema"]["properties"]["action"]["enum"])
        self.assertIn("action=finish", tool["description"])
        self.assertEqual(self.broker.reply_control(token)["mode"], "finished")

    def test_task_delivery_commit_is_preserved_and_does_not_itself_end_the_turn(self):
        context, token = self.context()
        task = execute("submit", context, task="资料整理")
        self.broker.update(task["id"], "ready", {"success": True, "result": {"files": []}})
        delivery = execute("deliver", context, request_id=task["id"], text="资料在这里", finish=True)
        self.assertEqual(self.broker.reply_control(token)["mode"], "committed")
        execute("finish", context)
        self.assertEqual(execute("deliver", context, request_id=task["id"], text="资料在这里", finish=True), delivery)
        plan = json.loads(self.broker.record(task["id"])["result"])["delivery"]
        self.assertEqual(plan["approved_by"], token)
        self.assertEqual(plan["text"], "资料在这里")


class FinishNativeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-finish-native-")
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.context = self.broker.issue_context({"namespace": "a", "scope": "g", "main_agent": True}, self.root / "contexts")
        self.token = ConsoleBroker.from_context(self.context)[1]
        self.ai = AntigravityClient(sessions_file=str(self.root / "sessions.json"))
        self.ai._is_ready = True
        self.ai._brain_dir = str(self.root / "brain")
        self.ai._cancel_native_conversation = AsyncMock()
        self.ai._ensure_conversation_present = AsyncMock()
        self.key = "onebot_group_finish"
        self.ai.get_or_create_session(self.key).conversation_id = "finish-cid"
        self.path = self.root / "brain/finish-cid/.system_generated/logs/transcript.jsonl"
        self.path.parent.mkdir(parents=True)

    async def asyncTearDown(self):
        await asyncio.get_running_loop().shutdown_default_executor()
        self.temp.cleanup()

    def stop(self):
        return self.broker.reply_control(self.token).get("mode", False)

    async def test_finish_stops_a_model_with_no_final_but_reply_alone_leaves_tools_open(self):
        submitted = asyncio.Event()
        async def command(args, **options):
            self.path.write_text(json.dumps(inbound(1, args[-1].splitlines()[0])) + "\n", encoding="utf-8")
            execute("reply", self.context, text="已准备回复")
            submitted.set()
            await asyncio.Event().wait()  # The screenshot's repeated continuation never ends.
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        turn = asyncio.create_task(self.ai.chat(self.key, "问题", bind_response=True, stop_requested=self.stop))
        try:
            await asyncio.wait_for(submitted.wait(), 2)
            await asyncio.sleep(.15)
            self.assertFalse(turn.done())
            self.ai._cancel_native_conversation.assert_not_awaited()
            execute("finish", self.context)
            self.assertEqual(await asyncio.wait_for(turn, 2), "")
            self.ai._cancel_native_conversation.assert_awaited_once_with("finish-cid", kill_background_tasks=False)
            self.assertEqual(len(self.broker.staged_messages(self.token)), 1)
            self.assertEqual(self.ai.get_or_create_session(self.key).conversation_id, "finish-cid")
        finally:
            if not turn.done():
                turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)

    async def test_next_input_waits_until_the_previous_finish_cancellation_returns(self):
        requested, cancel_started, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0
        async def command(args, **options):
            nonlocal calls
            calls += 1
            rows = [inbound(calls * 10, args[-1].splitlines()[0])]
            if calls == 2:
                rows.append(final(21, "第二轮完成"))
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(json.dumps(row) for row in rows) + "\n")
            requested.set()
            return {}
        async def cancel(cid, **options):
            self.assertEqual(options, {"kill_background_tasks": False})
            cancel_started.set()
            await cancelled.wait()
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        self.ai._cancel_native_conversation.side_effect = cancel
        first = asyncio.create_task(self.ai.chat(self.key, "第一条", bind_response=True, stop_requested=self.stop))
        second = None
        try:
            await asyncio.wait_for(requested.wait(), 2)
            execute("finish", self.context)
            await asyncio.wait_for(cancel_started.wait(), 2)
            second = asyncio.create_task(self.ai.chat(self.key, "第二条", bind_response=True))
            await asyncio.sleep(.15)
            self.assertEqual(calls, 1)
            self.assertFalse(first.done())
            cancelled.set()
            self.assertEqual(await asyncio.wait_for(first, 2), "")
            self.assertEqual(await asyncio.wait_for(second, 2), "第二轮完成")
            self.assertEqual(calls, 2)
        finally:
            cancelled.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)

    async def test_finish_racing_with_a_final_row_still_stops_the_invocation(self):
        async def command(args, **options):
            return {}
        async def response(**options):
            execute("finish", self.context)
            return "在线待命，不应再续写"
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        self.ai._wait_for_model_response = AsyncMock(side_effect=response)
        result = await self.ai.chat(self.key, "问题", bind_response=True, stop_requested=self.stop)
        self.assertEqual(result, "")
        self.ai._cancel_native_conversation.assert_awaited_once_with("finish-cid", kill_background_tasks=False)

    async def test_a_new_main_conversation_can_finish_without_a_final_response(self):
        self.ai.get_or_create_session(self.key).conversation_id = None
        async def command(args, **options):
            self.assertEqual(args[0], "new-conversation")
            self.path.write_text(json.dumps(inbound(1, args[-1].splitlines()[0])) + "\n", encoding="utf-8")
            execute("reply", self.context, text="第一条回复")
            execute("finish", self.context)
            return {"response": {"newConversation": {"conversationId": "finish-cid"}}}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        result = await asyncio.wait_for(self.ai.chat(self.key, "首次消息", bind_response=True, stop_requested=self.stop), 2)
        self.assertEqual(result, "")
        self.ai._cancel_native_conversation.assert_awaited_once_with("finish-cid", kill_background_tasks=False)
        self.assertEqual(self.ai.get_or_create_session(self.key).conversation_id, "finish-cid")


class FinishMainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await asyncio.get_running_loop().shutdown_default_executor()
        await self.fixture.asyncTearDown()

    async def test_hold_then_direct_reply_each_need_only_one_tool_call_on_both_transports(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, key = self.fixture.bot(official, stored_cid="one-call-cid")
                if not official:
                    bot._is_ws_connected = lambda: True
                coordinator = bot._get_main_coordinator()
                bot.ai.chat = AntigravityClient.chat.__get__(bot.ai)
                bot.ai._is_ready = True
                bot.ai._brain_dir = str(Path(self.fixture.temp.name) / ("one-call-brain" + str(official)))
                bot.ai._cancel_native_conversation = AsyncMock()
                bot.ai._ensure_conversation_present = AsyncMock()
                log = Path(bot.ai._brain_dir) / "one-call-cid/.system_generated/logs/transcript.jsonl"
                log.parent.mkdir(parents=True)
                calls, contexts, reaped = [], [], []
                async def model(args, **options):
                    context = list(coordinator.requests)[-1]
                    contexts.append(context)
                    with log.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(inbound(len(contexts) * 10, args[-1].splitlines()[0])) + "\n")
                    try:
                        if len(contexts) == 1:
                            calls.append("hold")
                            execute("speech", context, operation="hold")
                        else:
                            speech = prompt_object(args[-1], "speech")["speech"]
                            self.assertEqual([item["text"] for item in speech["cached_messages"]], ["因为"])
                            calls.append("reply")
                            execute("reply", context, text="那先了解养猫需要哪些准备。", finish=True)
                        # No separate ready/finish, and no final response. The
                        # controller must stop this invocation automatically.
                        await asyncio.Event().wait()
                    finally:
                        reaped.append(len(contexts))
                bot.ai._run_agentapi_cmd = AsyncMock(side_effect=model)
                with patch.object(coordinator, "start"), patch.object(coordinator, "poll", new=AsyncMock()):
                    first = await asyncio.wait_for(self.fixture.receive(bot, official, "q", "因为"), 3)
                    (first.reply if official else bot.send_group_message).assert_not_awaited()
                    second = await asyncio.wait_for(self.fixture.receive(bot, official, "next", "我想养只猫，怎么准备？", elapsed=1), 3)
                (second.reply if official else bot.send_group_message).assert_awaited_once()
                self.assertEqual(calls, ["hold", "reply"])
                self.assertEqual(reaped, [1, 2])
                self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "one-call-cid")
                self.assertEqual(bot.ai._cancel_native_conversation.await_count, 2)
                self.assertTrue(all(call.kwargs == {"kill_background_tasks": False}
                                    for call in bot.ai._cancel_native_conversation.await_args_list))
                broker, token = ConsoleBroker.from_context(contexts[-1])
                payload = broker.context(token)
                self.assertFalse(broker.has_pending_speech(payload["namespace"], payload["scope"],
                                                         key, payload["generation"]))

    async def test_main_sends_once_after_finish_and_the_next_message_uses_the_same_cid(self):
        for official in (False, True):
            bot, key = self.fixture.bot(official, stored_cid="main-finish")
            coordinator = bot._get_main_coordinator()
            bot.ai.chat = AntigravityClient.chat.__get__(bot.ai)
            bot.ai._is_ready = True
            bot.ai._brain_dir = str(Path(f"brain-{official}").resolve())
            bot.ai._cancel_native_conversation = AsyncMock()
            bot.ai._ensure_conversation_present = AsyncMock()
            log = Path(bot.ai._brain_dir) / "main-finish/.system_generated/logs/transcript.jsonl"
            log.parent.mkdir(parents=True)
            contexts, tasks = [], []
            async def command(args, **options):
                context = prompt_object(args[-1], "console_context_file")["console_context_file"]
                contexts.append(context)
                execute("speech", context, operation="ready")
                execute("reply", context, text="我继续找找资料。")
                tasks.append(execute("submit", context, task="整理访谈"))
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(inbound(len(contexts) * 10, args[-1].splitlines()[0])) + "\n")
                execute("finish", context)
                await asyncio.Event().wait()  # Explicit finish must release it.
            bot.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
            self.enterContext(patch.object(coordinator, "poll", new_callable=AsyncMock))
            first = await asyncio.wait_for(self.fixture.receive(bot, official, "q", "聊聊配乐"), 3)
            second = await asyncio.wait_for(self.fixture.receive(bot, official, "next", "接着说", elapsed=1), 3)
            if official:
                first.reply.assert_awaited_once()
                second.reply.assert_awaited_once()
                self.assertEqual(first.reply.await_args.kwargs["content"], "我继续找找资料。")
            else:
                self.assertEqual(bot.send_group_message.await_count, 2)
                self.assertTrue(all(call.args[1].body == "我继续找找资料。" for call in bot.send_group_message.await_args_list))
            self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "main-finish")
            self.assertEqual(bot.ai._cancel_native_conversation.await_count, 2)
            self.assertTrue(all(call.kwargs == {"kill_background_tasks": False} for call in bot.ai._cancel_native_conversation.await_args_list))
            self.assertTrue(all(coordinator.broker.record(task["id"])["state"] == "submitted" for task in tasks))
            for context in contexts:
                token = ConsoleBroker.from_context(context)[1]
                self.assertEqual(coordinator.broker.reply_control(token)["mode"], "sealed")
                self.assertEqual(execute("finish", context)["state"], "finished")

    async def test_a_late_model_error_after_finish_cannot_discard_the_committed_reply(self):
        bot, _ = self.fixture.bot(stored_cid="main-finish")
        context = []
        async def model(key, prompt, **options):
            context.append(prompt_object(prompt, "console_context_file")["console_context_file"])
            execute("speech", context[-1], operation="ready")
            execute("reply", context[-1], text="已经提交的正文")
            execute("finish", context[-1])
            raise RuntimeError("late native exit")
        bot.ai.chat.side_effect = model
        await self.fixture.receive(bot, False, "q", "回一句")
        bot.send_group_message.assert_awaited_once()
        self.assertEqual(bot.send_group_message.await_args.args[1].body, "已经提交的正文")
        token = ConsoleBroker.from_context(context[0])[1]
        with bot._get_main_coordinator().broker.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM outgoing WHERE token=?", (token,)).fetchone()[0], "accepted")

    async def test_internal_finish_closes_the_turn_without_any_qq_message(self):
        bot, key = self.fixture.bot(stored_cid="main-finish")
        await self.fixture.receive(bot, False, "q", "先聊到这里")
        coordinator = bot._get_main_coordinator()
        context = []
        async def model(session_key, prompt, **options):
            context.append(prompt_object(prompt, "console_context_file")["console_context_file"])
            execute("finish", context[-1])
            self.assertEqual(options["stop_requested"](), "finished")
            return "不能发送的内部文字"
        bot.ai.chat.side_effect = model
        await coordinator.chat(key, '{"type":"self_activity"}', idle_event=True, self_activity=True,
            activity_scope=coordinator.contexts[key].scope)
        token = ConsoleBroker.from_context(context[0])[1]
        self.assertEqual(coordinator.broker.reply_control(token)["mode"], "sealed")
        bot.send_group_message.assert_not_awaited()
        bot.send_action.assert_not_awaited()
