"""Offline regressions for provider retries retaining their original reply turn."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from antigravity_client import (AgentServiceError, AntigravityClient, ContentBlockedError,
                               ConversationMissingError, SessionInvalidatedError)
from reply_queue import ReplyQueue


CAPACITY = "API error (attempt 1): UNAVAILABLE (code 503): No capacity available for model fixture"


def inbound(step, marker):
    return {"step_index": step, "source": "USER_EXPLICIT", "type": "USER_INPUT",
            "status": "DONE", "content": marker}


def failure(step, detail=CAPACITY, **extra):
    return {"step_index": step, "source": "SYSTEM", "type": "ERROR_MESSAGE",
            "status": "DONE", "error": detail, **extra}


def final(step, text):
    return {"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE",
            "status": "DONE", "content": text}


class ModelRetryQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-retry-")
        self.addCleanup(self.temp.cleanup)
        self.ai = AntigravityClient(sessions_file=str(Path(self.temp.name) / "sessions.json"))
        self.ai._is_ready = True
        self.ai._brain_dir = str(Path(self.temp.name) / "brain")
        self.ai._cancel_native_conversation = AsyncMock()
        self.ai._ensure_conversation_present = AsyncMock()
        self.key = "onebot_group_retry"
        self.session = self.ai.get_or_create_session(self.key)
        self.session.conversation_id = "fixture"
        self.path = Path(self.ai._brain_dir) / "fixture/.system_generated/logs/transcript.jsonl"
        self.path.parent.mkdir(parents=True)

    def write(self, *rows):
        self.path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def append(self, *rows):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))

    async def wait_reply(self, **options):
        return await self.ai._wait_for_model_response("fixture", request_marker="current", timeout=1, **options)

    async def test_capacity_retry_then_success_in_same_snapshot(self):
        self.write(inbound(1, "current"), failure(2), final(3, "恢复后的完整回复"))
        self.assertEqual(await self.wait_reply(), "恢复后的完整回复")

    async def test_network_retry_then_success_is_retained(self):
        self.write(inbound(1, "current"), failure(2, "API error (attempt 2): request failed: EOF"),
                   final(3, "网络恢复后的回复"))
        self.assertEqual(await self.wait_reply(), "网络恢复后的回复")

    async def test_new_input_waits_for_backend_retry_without_resubmission_or_cancellation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        queue, delivered, commands = ReplyQueue(), [], []
        async def command(args, **options):
            commands.append(args[0])
            marker = args[-1].splitlines()[0]
            if len(commands) == 1:
                self.write(inbound(1, marker), failure(2))
                entered.set()
            else:
                self.append(inbound(4, marker), final(5, "第二轮回复"))
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        async def reply(text):
            async with queue.turn(self.key):
                delivered.append(await self.ai.chat(self.key, text, bind_response=True))
        async def recover():
            await release.wait()
            self.append(final(3, "第一轮完整回复"))
        writer = asyncio.create_task(recover())
        first = asyncio.create_task(reply("第一问"))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(reply("还在吗"))
        try:
            await asyncio.sleep(.15)
            self.assertEqual(commands, ["send-message"])
            self.assertFalse(first.done())
            self.assertFalse(second.done())
            self.assertFalse(delivered)
        finally:
            release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second, writer, return_exceptions=True), 2)
        self.assertFalse(any(isinstance(r, BaseException) for r in results), results)
        self.assertEqual(delivered, ["第一轮完整回复", "第二轮回复"])
        self.assertEqual(commands, ["send-message", "send-message"])
        self.ai._cancel_native_conversation.assert_not_awaited()
        self.assertEqual(self.session.conversation_id, "fixture")
        self.assertFalse(queue._slots)

    async def test_progress_before_retry_is_not_mistaken_for_completed_reply(self):
        self.write(inbound(1, "current"), final(2, "此前的进度"), failure(3))
        async def recover():
            await asyncio.sleep(.15)
            self.append(final(4, "真正完成的回复"))
        writer = asyncio.create_task(recover())
        try:
            self.assertEqual(await self.wait_reply(), "真正完成的回复")
        finally:
            await writer

    async def test_external_input_during_retry_does_not_adopt_its_reply(self):
        self.write(inbound(1, "current"), final(2, "此前的进度"), failure(3),
                   inbound(4, "external-input"), final(5, "其他输入的回复"))
        with self.assertRaisesRegex(RuntimeError, "另一条输入打断"):
            await self.wait_reply()

    async def test_empty_final_after_retry_is_not_silent_success(self):
        self.ai._CONVERSATION_CHECK_DELAY = 0
        self.ai._retry_run_status = AsyncMock(return_value={"status": "CASCADE_RUN_STATUS_IDLE"})
        self.write(inbound(1, "current"), failure(2), final(3, ""))
        with self.assertRaises(AgentServiceError) as caught:
            await self.wait_reply(allow_empty_response=True)
        self.assertEqual(getattr(caught.exception, "code", ""), "MODEL_BUSY")

    async def test_backend_stopping_after_retry_releases_queue_and_keeps_conversation(self):
        self.ai._CONVERSATION_CHECK_DELAY = 0
        self.ai._retry_run_status = AsyncMock(return_value={"status": "CASCADE_RUN_STATUS_IDLE"})
        async def command(args, **options):
            self.write(inbound(1, args[-1].splitlines()[0]), failure(2))
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        queue = ReplyQueue()
        trace = []
        with self.assertRaises(AgentServiceError) as caught:
            async with queue.turn(self.key):
                await asyncio.wait_for(self.ai.chat(self.key, "hello", bind_response=True, response_trace=trace), 1)
        self.assertEqual(getattr(caught.exception, "code", ""), "MODEL_BUSY")
        self.assertEqual(self.session.conversation_id, "fixture")
        self.ai._run_agentapi_cmd.assert_awaited_once()
        self.assertFalse(queue._slots)
        self.assertTrue(any(row.get("error") == CAPACITY for row in trace))

    async def test_idle_status_does_not_beat_final_rows_still_being_written(self):
        self.ai._CONVERSATION_CHECK_DELAY = 0
        self.ai._retry_run_status = AsyncMock(return_value={"status": "CASCADE_RUN_STATUS_IDLE", "numTotalSteps": 4})
        self.write(inbound(1, "current"), failure(2))
        async def recover():
            await asyncio.sleep(.15)
            self.append(final(3, "日志随后落盘的回复"))
        writer = asyncio.create_task(recover())
        try:
            self.assertEqual(await self.wait_reply(), "日志随后落盘的回复")
        finally:
            await writer

    async def test_retry_wait_still_honors_explicit_timeout(self):
        self.write(inbound(1, "current"), failure(2))
        with self.assertRaises(TimeoutError):
            await self.ai._wait_for_model_response("fixture", request_marker="current", timeout=.15)

    async def test_retry_wait_still_honors_cancellation(self):
        self.write(inbound(1, "current"), failure(2))
        task = asyncio.create_task(self.wait_reply())
        try:
            await asyncio.sleep(.15)
            self.assertFalse(task.done())
        finally:
            task.cancel()
            results = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)

    async def test_reset_interrupts_retry_and_releases_the_turn(self):
        entered = asyncio.Event()
        async def command(args, **options):
            self.write(inbound(1, args[-1].splitlines()[0]), failure(2))
            entered.set()
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        queue = ReplyQueue()
        async def reply():
            async with queue.turn(self.key):
                await self.ai.chat(self.key, "第一问", bind_response=True)
        task = asyncio.create_task(reply())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.ai.reset_session(self.key)
            with self.assertRaises(SessionInvalidatedError):
                await asyncio.wait_for(task, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(queue._slots)
        self.ai._cancel_native_conversation.assert_awaited_once_with("fixture")

    async def test_terminal_error_and_policy_block_are_not_hidden_by_attempt_annotation(self):
        for detail, expected in [
                ("API error (attempt 1): UNAUTHENTICATED (code 401)", RuntimeError),
                ("API error (attempt 2): retries exhausted: 503 unavailable", RuntimeError),
                ("API error (attempt 1): content blocked by safety policy", ContentBlockedError),
                ("503 model unavailable", RuntimeError)]:
            with self.subTest(detail=detail):
                self.write(inbound(1, "current"), failure(2, detail))
                with self.assertRaises(expected):
                    await self.wait_reply()


class NativeRetryStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-retry-status-")
        self.addCleanup(self.temp.cleanup)
        self.ai = AntigravityClient(sessions_file=str(Path(self.temp.name) / "sessions.json"))
        self.responses, self.requests = [], []
        async def status(request):
            self.requests.append((request.path, await request.json(), request.headers.get("X-Codeium-Csrf-Token")))
            return self.responses.pop(0)
        app = web.Application()
        app.router.add_post("/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory", status)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.addAsyncCleanup(self.runner.cleanup)
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.ai.ls_address = "127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.ai.csrf_token = "fixture-only"

    async def test_status_probe_reads_native_state_without_sending_or_resuming(self):
        from aiohttp import web
        self.responses.append(web.json_response({"trajectory": {"cascadeId": "fixture"},
            "status": "CASCADE_RUN_STATUS_IDLE", "numTotalSteps": 42}))
        self.assertEqual(await self.ai._retry_run_status("fixture"),
                         {"status": "CASCADE_RUN_STATUS_IDLE", "numTotalSteps": 42})
        self.assertEqual(self.requests, [("/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory",
                                         {"cascadeId": "fixture"}, "fixture-only")])

    async def test_unavailable_status_is_unknown_and_preserves_the_retry(self):
        from aiohttp import web
        self.responses.append(web.Response(status=503, text="temporarily unavailable"))
        self.assertIsNone(await self.ai._retry_run_status("fixture"))

    async def test_read_only_status_reconnects_after_local_port_changes(self):
        from aiohttp import web

        address = self.ai.ls_address
        self.responses.append(web.json_response({"status": "CASCADE_RUN_STATUS_IDLE"}))
        self.ai._is_ready = True
        session = self.ai.get_or_create_session("group_test")
        session.conversation_id = session.chat_state["stop_pending"] = "fixture"
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=RuntimeError("send reached"))
        with socket.socket() as refused:
            refused.bind(("127.0.0.1", 0))
            self.ai.ls_address = f"127.0.0.1:{refused.getsockname()[1]}"
            with patch.object(self.ai, "_discover_ls_credentials", return_value=(address, "refreshed")) as discover:
                with self.assertRaisesRegex(RuntimeError, "send reached"):
                    await self.ai.chat("group_test", "新消息", raise_on_error=True)
        self.assertNotIn("stop_pending", session.chat_state)
        self.assertEqual(discover.call_count, 1)
        self.assertTrue(discover.call_args.kwargs["refresh"])
        self.assertEqual(self.requests, [("/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory",
                                         {"cascadeId": "fixture"}, "refreshed")])

    async def test_explicit_missing_conversation_still_allows_recovery(self):
        from aiohttp import web
        self.responses.append(web.json_response({"message": "cascade not found: fixture"}, status=404))
        with self.assertRaises(ConversationMissingError):
            await self.ai._retry_run_status("fixture")


class FailureEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_main_context_remains_open_until_native_retry_delivers_all_bubbles(self):
        from console_tools.cli import execute
        for official in (False, True):
            with self.subTest(official=official):
                bot, key = self.fixture.bot(official, stored_cid="fixture")
                ai = bot.ai
                ai.chat = AntigravityClient.chat.__get__(ai)
                ai._is_ready = True
                ai._brain_dir = str(Path(ai.sessions_file).resolve().parent / "brain")
                ai._cancel_native_conversation = AsyncMock()
                path = Path(ai._brain_dir) / "fixture/.system_generated/logs/transcript.jsonl"
                path.parent.mkdir(parents=True)
                entered = asyncio.Event()
                async def command(args, **options):
                    rows = [inbound(1, args[-1].splitlines()[0]), failure(2)]
                    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
                    entered.set()
                    return {}
                ai._run_agentapi_cmd = AsyncMock(side_effect=command)
                task = asyncio.create_task(self.fixture.receive(bot, official, "source", "活着吗"))
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    console = bot._get_main_coordinator()
                    context_file = list(console.requests)[-1]
                    _, token = console.broker.from_context(context_file)
                    await asyncio.sleep(.15)
                    self.assertFalse(task.done())
                    self.assertNotEqual(console.broker.reply_control(token).get("mode"), "sealed")
                    execute("reply", context_file, text=["第一段完整回复", "第二段完整回复"])
                    execute("finish", context_file)
                    with path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(final(3, "internal completion")) + "\n")
                    event = await asyncio.wait_for(task, 3)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                parts = ([c.kwargs["content"] for c in event.reply.await_args_list] if official
                         else [str(c.args[1]) for c in bot.send_group_message.await_args_list])
                self.assertEqual(parts, ["第一段完整回复", "第二段完整回复"])
                self.assertEqual(ai.get_or_create_session(key).conversation_id, "fixture")
                ai._run_agentapi_cmd.assert_awaited_once()
                ai._cancel_native_conversation.assert_awaited_once_with("fixture", kill_background_tasks=False)

    async def test_next_turn_receives_actual_service_failure_once(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, key = self.fixture.bot(official, stored_cid="same-main")
                bot.ai.chat.side_effect = AgentServiceError()
                await self.fixture.receive(bot, official, "failed", "活着吗")
                bot.ai.chat.side_effect = None
                bot.ai.chat.return_value = "服务刚才不可用，现在能收到啦。"
                await self.fixture.receive(bot, official, "followup", "刚才为什么不回复", elapsed=1)
                from test_ai_fixture import prompt_object
                evidence = prompt_object(bot.ai.chat.await_args.args[1], "previous_turn_failure")["previous_turn_failure"]
                self.assertEqual(evidence["source_message_id"], "failed")
                self.assertEqual(evidence["code"], "AI_SERVICE_UNAVAILABLE")
                self.assertEqual(evidence["reason"], AgentServiceError.user_message)
                await self.fixture.receive(bot, official, "next", "继续聊", elapsed=2)
                self.assertNotIn('"previous_turn_failure"', bot.ai.chat.await_args.args[1])
                self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "same-main")
