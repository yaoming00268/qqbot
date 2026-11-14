"""Offline reply transactions and natural completion of later tool calls."""
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

from antigravity_client import AgentServiceError, AntigravityClient, ContentBlockedError
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.files import ConsoleFileError
from test_agent_reply_latency import inbound, final
from test_ai_fixture import drain_runtime


class ReplyTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="QQ reply 中文 ' ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.payload = {"namespace": "account", "scope": "group", "session_key": "group_1",
            "sender_id": "456", "is_group": True, "bot_id": "999", "transport": "official",
            "reply_slots": 5, "reply_sources": [{"id": "source+/=", "sender_id": "456", "text": "问题"}]}
        self.context = self.broker.issue_context(self.payload, self.root / "contexts")
        _, self.token = ConsoleBroker.from_context(self.context)

    def test_batch_preserves_order_targets_first_bubble_and_exact_retries(self):
        args = dict(text=["第一句", "第二句", "第二句"], reply_to="source+/=", mention="456")
        first = execute("reply", self.context, **args)
        self.assertEqual(first["state"], "committed")
        self.assertEqual(first["next_action"], "continue_or_finish")
        self.assertEqual(execute("reply", self.context, **args), first)
        rows = self.broker.staged_messages(self.token)
        self.assertEqual([row["text"] for row in rows], args["text"])
        self.assertEqual([(row["reply_to"], row["mention"]) for row in rows], [("source+/=", "456"), ("", ""), ("", "")])
        self.broker.seal_messages(self.token)
        broker, _ = ConsoleBroker.from_context(self.context)
        self.assertEqual(broker.reply(self.token, **{"texts": args["text"], "reply_to": "source+/=", "mention": "456"}),
                         {key: value for key, value in first.items() if key != "ok"})
        self.assertEqual(execute("commit", self.context), first)
        for action, values in (("reply", {"text": "不同内容"}), ("message", {"text": "迟到消息"}), ("wait", {})):
            with self.subTest(action=action), self.assertRaises(ConsoleFileError):
                execute(action, self.context, **values)

    def test_invalid_batch_or_insufficient_platform_slots_leaves_no_partial_reply(self):
        for values in ({"text": ["第一句", " "]}, {"text": ["第一句", "第二句"], "mention": "unknown"}):
            with self.assertRaises(ValueError):
                execute("reply", self.context, **values)
            self.assertEqual(self.broker.staged_messages(self.token), [])
            self.assertEqual(self.broker.reply_control(self.token), {})
        context = self.broker.issue_context({**self.payload, "reply_slots": 1}, self.root / "limited")
        _, token = ConsoleBroker.from_context(context)
        with self.assertRaises(ConsoleFileError):
            execute("reply", context, text=["第一句", "第二句"], reply_to="source+/=")
        self.assertEqual(self.broker.staged_messages(token), [])
        self.assertEqual(self.broker.reply_control(token), {})
        self.assertEqual(execute("reply", context, text=["第一句", "第二句"])["state"], "committed")

    def test_legacy_multi_target_staging_can_be_committed_once(self):
        first = execute("message", self.context, text="带引用", reply_to="source+/=")
        second = execute("message", self.context, text="普通正文")
        receipt = execute("commit", self.context)
        self.assertEqual(receipt["message_ids"], [first["id"], second["id"]])
        self.assertEqual(execute("commit", self.context), receipt)
        with self.assertRaises(ConsoleFileError):
            execute("wait", self.context)
        self.assertEqual(len(self.broker.staged_messages(self.token)), 2)

    def test_wait_and_reply_are_serialized_without_cancelling_a_committed_reply(self):
        for index in range(8):
            context = self.broker.issue_context(self.payload, self.root / str(index))
            _, token = ConsoleBroker.from_context(context)
            start = threading.Barrier(2)
            def run(action):
                start.wait()
                try:
                    return execute(action, context, **({"text": "已提交"} if action == "reply" else {}))
                except ConsoleFileError as error:
                    return error.code
            with ThreadPoolExecutor(max_workers=2) as workers:
                replies = list(workers.map(run, ["reply", "wait"]))
            self.assertEqual(sum(isinstance(row, dict) for row in replies), 1, replies)
            mode = self.broker.reply_control(token)["mode"]
            self.assertEqual(len(self.broker.staged_messages(token)), 1 if mode == "committed" else 0)

    def test_stage_cannot_write_after_seal_between_validation_and_transaction(self):
        original = self.broker._message_targets
        def seal_after_validation(*args):
            original(*args)
            self.broker.seal_messages(self.token, cancel=True)
        with patch.object(self.broker, "_message_targets", side_effect=seal_after_validation):
            with self.assertRaises(ConsoleFileError):
                self.broker.stage_message(self.token, "迟到正文")
        self.assertEqual(self.broker.staged_messages(self.token), [])

    def test_seal_snapshot_contains_every_successful_concurrent_stage(self):
        for index in range(8):
            context = self.broker.issue_context(self.payload, self.root / ("seal" + str(index)))
            _, token = ConsoleBroker.from_context(context)
            start = threading.Barrier(2)
            def stage():
                start.wait()
                try:
                    return self.broker.stage_message(token, "并发回复")
                except ConsoleFileError:
                    return None
            def seal():
                start.wait()
                return self.broker.seal_messages(token)
            with ThreadPoolExecutor(max_workers=2) as workers:
                sender, closer = workers.submit(stage), workers.submit(seal)
                sent, snapshot = sender.result(), closer.result()
            self.assertEqual([row["command_id"] for row in snapshot], [sent["id"]] if sent else [])
            self.assertEqual(self.broker.staged_messages(token), snapshot)

    def test_explicit_cancellation_does_not_return_an_old_success_on_retry(self):
        execute("reply", self.context, text="旧回复")
        self.broker.seal_messages(self.token, cancel=True)
        with self.assertRaises(ConsoleFileError):
            execute("reply", self.context, text="旧回复")
        self.assertEqual(self.broker.staged_messages(self.token), [])

    def test_worker_cannot_use_fast_reply_or_commit(self):
        worker = self.broker.issue_context({**self.payload, "parent_request_id": "task"}, self.root / "worker")
        for action in ("reply", "commit"):
            with self.subTest(action=action), self.assertRaises(PermissionError):
                execute(action, worker, text="执行分支不得发送")

    def test_approval_and_finish_commit_together_and_replay_is_idempotent(self):
        task = execute("submit", self.context, task="资料整理")
        self.broker.update(task["id"], "ready", {"success": True, "result": {"files": []}})
        receipt = execute("deliver", self.context, request_id=task["id"], text="资料已整理", finish=True)
        self.assertEqual(receipt["state"], "committed")
        self.assertEqual(execute("deliver", self.context, request_id=task["id"], text="资料已整理", finish=True), receipt)
        self.assertEqual(json.loads(self.broker.record(task["id"])["result"])["delivery"]["approved_by"], self.token)
        with self.assertRaises(ConsoleFileError):
            execute("deliver", self.context, request_id=task["id"], text="另一条", finish=True)

    def test_real_cli_repeated_text_and_unicode_file_do_not_suggest_polling(self):
        cli = str(Path(__file__).with_name("console_tools") / "cli.py")
        argv = [sys.executable, cli, "reply", "--context-file", self.context, "--text", "中文 ' 引号", "--text", '第二句 "quote" $(literal)']
        result = subprocess.run(argv, capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertNotIn("next_commands", receipt)
        self.assertEqual([row["text"] for row in self.broker.staged_messages(self.token)], [argv[-3], argv[-1]])
        context = self.broker.issue_context(self.payload, self.root / "file_context")
        source = self.root / "正文.txt"
        source.write_text("多行中文\n原样保留 ' 与 \" 引号", encoding="utf-16")
        result = subprocess.run([sys.executable, cli, "reply", "--context-file", context, "--text-file", str(source)],
                                capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        _, token = ConsoleBroker.from_context(context)
        self.assertEqual(self.broker.staged_messages(token)[0]["text"], source.read_text(encoding="utf-16"))


class CommittedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def bot(self, official=False):
        bot, key = self.fixture.bot(official, stored_cid="fast-cid")
        if not official:
            bot._is_ws_connected = lambda: True
        return bot, key

    def context(self, bot):
        return list(bot._get_main_coordinator().requests)[-1]

    async def test_reply_waits_for_later_tools_and_natural_final_without_cancelling_either_transport(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, key = self.bot(official)
                ai = bot.ai
                ai.chat = AntigravityClient.chat.__get__(ai)
                ai._is_ready = True
                ai._brain_dir = str(Path(self.fixture.temp.name) / ("brain" + str(official)))
                ai._cancel_native_conversation = AsyncMock()
                ai._ensure_conversation_present = AsyncMock()
                path = Path(ai._brain_dir) / "fast-cid/.system_generated/logs/transcript.jsonl"
                path.parent.mkdir(parents=True)
                count = 0
                reaped = []
                submitted, finish = asyncio.Event(), asyncio.Event()
                coordinator = bot._get_main_coordinator()
                agents = []
                async def tool(args, **options):
                    nonlocal count
                    count += 1
                    with path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(inbound(count * 10, args[-1].splitlines()[0])) + "\n")
                    context = self.context(bot)
                    execute("speech", context, operation="ready")
                    execute("reply", context, text=f"第{count}轮完整回复")
                    submitted.set()
                    try:
                        await finish.wait()
                        agent = execute("agents", context, operation="create", params={"name": "资料员", "text": f"实际任务{count}"})
                        agents.append(agent)
                        execute("agents", context, operation="send", params={"agent_id": agent["id"], "text": f"后续要求{count}"})
                        execute("companion", context, operation="remember", params={"text": "回复之后继续安排实际行动"})
                        execute("finish", context)
                        with path.open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(final(count * 10 + 1, "")) + "\n")
                        # Slow local RPC cleanup must not trigger backend cancellation.
                        await asyncio.Event().wait()
                    finally:
                        reaped.append(count)
                ai._run_agentapi_cmd = AsyncMock(side_effect=tool)
                with patch.object(coordinator, "start"), patch.object(coordinator, "poll", new=AsyncMock()):
                    for index in (1, 2):
                        submitted.clear()
                        finish.clear()
                        receiving = asyncio.create_task(self.fixture.receive(bot, official, f"source{index}", "普通问题", elapsed=index))
                        try:
                            await asyncio.wait_for(submitted.wait(), 2)
                            await asyncio.sleep(.15)
                            self.assertFalse(receiving.done())
                            self.assertEqual(ai._cancel_native_conversation.await_count, index - 1)
                            finish.set()
                            event = await asyncio.wait_for(receiving, 2)
                        finally:
                            finish.set()
                            if not receiving.done():
                                receiving.cancel()
                            await asyncio.gather(receiving, return_exceptions=True)
                        sends = event.reply if official else bot.send_group_message
                        self.assertEqual(sends.await_count, 1 if official else index)
                self.assertEqual(reaped, [1, 2])
                self.assertEqual(agents[0]["id"], agents[1]["id"])
                with coordinator.broker.connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM agent_mail WHERE direction='to_agent'").fetchone()[0], 4)
                self.assertEqual(ai.get_or_create_session(key).conversation_id, "fast-cid")
                self.assertEqual(ai._cancel_native_conversation.await_count, 2)
                self.assertTrue(all(call.kwargs.get("kill_background_tasks") is False
                    for call in ai._cancel_native_conversation.await_args_list))

    async def test_model_error_after_commit_does_not_lose_successful_reply(self):
        for official, error in ((False, AgentServiceError()), (True, ContentBlockedError("late error"))):
            bot, _ = self.bot(official)
            async def respond(key, prompt, **options):
                execute("speech", self.context(bot), operation="ready")
                execute("reply", self.context(bot), text="已经确认的回复")
                raise error
            bot.ai.chat = AsyncMock(side_effect=respond)
            event = await self.fixture.receive(bot, official, "source", "普通问题")
            (event.reply if official else bot.send_group_message).assert_awaited_once()
            await drain_runtime(bot)

    async def test_explicit_session_reset_still_invalidates_a_committed_reply(self):
        bot, key = self.bot()
        async def respond(key, prompt, **options):
            execute("speech", self.context(bot), operation="ready")
            execute("reply", self.context(bot), text="旧会话的回复")
            bot.ai.reset_session(key)
            return ""
        bot.ai.chat = AsyncMock(side_effect=respond)
        await self.fixture.receive(bot, False, "source", "普通问题")
        bot.send_group_message.assert_not_awaited()
        console = bot._get_main_coordinator()
        _, token = ConsoleBroker.from_context(self.context(bot))
        self.assertEqual(console.broker.staged_messages(token), [])

    async def test_task_result_approval_finish_delivers_even_if_model_has_no_final(self):
        bot, _ = self.bot()
        async def respond(key, prompt, **options):
            if key.startswith("__work__:"):
                return '{"answer":"查到的资料","files":[],"sources":[]}'
            event = next((json.loads(line) for line in prompt.splitlines() if line.startswith('{"type": "task_result"')), None)
            if event:
                self.assertIn("deliver_call", prompt)
                execute("deliver", self.context(bot), request_id=event["request_id"], text="资料在这里", finish=True)
                self.assertFalse(options["stop_requested"]())
                raise AgentServiceError()
            execute("submit", self.context(bot), task="整理资料")
            execute("finish", self.context(bot))
            return ""
        bot.ai.chat = AsyncMock(side_effect=respond)
        await self.fixture.receive(bot, False, "source", "整理资料")
        await drain_runtime(bot)
        bot.send_group_message.assert_awaited_once()
        self.assertEqual(str(bot.send_group_message.await_args.args[1]), "资料在这里")

