"""File operations use scoped queues and real workers with offline transports."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import asyncio
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from console_tools.broker import ConsoleBroker
from console_tools.cli import execute, main
from console_tools.files import ConsoleFileError, incoming_files
from console_tools.server import handle
from messaging import SendError, SendUnknown
from onebot_client import OneBotClient
from test_ai_fixture import drain_runtime


class ConsoleFileInterfaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def context(self, bot, key, mid="initial"):
        await self.fixture.receive(bot, False, mid, "先准备文件")
        return self.open_context(bot)

    def open_context(self, bot):
        # Direct CLI tests run inside a fresh main turn capability.
        coordinator = bot._get_main_coordinator()
        ctx = list(coordinator.requests.values())[-1]
        return coordinator.issue_chat_context(ctx, ctx.ai_session_key)[0]

    def upload(self, *, group=True, name="资料 文档.txt", size=7, native="qq-file-id"):
        if group:
            return {"post_type": "notice", "notice_type": "group_upload", "group_id": 12345678,
                "user_id": 456, "file": {"id": native, "name": name, "size": size, "busid": 102}}
        return {"post_type": "message", "message_type": "private", "user_id": 456, "message_id": "private-file",
                "message": [{"type": "file", "data": {"file_id": native, "name": name, "file_size": size}}]}

    async def test_send_existing_files_uses_no_agent_and_returns_per_file_receipts(self):
        bot, key = self.fixture.bot(stored_cid="existing-main")
        bot._is_ws_connected = lambda: True
        context = await self.context(bot, key)
        output = Path(execute("context", context)["output_dir"])
        path = output / "报告 中文.txt"
        path.write_text("completed report", encoding="utf-8")
        bot.ai.chat.reset_mock()
        first = execute("send", context, files=[path.name])
        self.assertEqual(first["id"], execute("send", context, files=[str(path)])["id"])
        coordinator = bot._get_main_coordinator()
        await coordinator.poll()
        await drain_runtime(bot)
        status = execute("status", context, request_id=first["id"])
        self.assertEqual(status["state"], "delivered", status)
        self.assertEqual(status["files"][0]["state"], "accepted")
        self.assertEqual(Path(status["files"][0]["local_path"]).read_text(encoding="utf-8"), "completed report")
        bot.ai.chat.assert_not_awaited()
        bot.send_action.assert_awaited_once()
        self.assertEqual(bot.send_action.await_args.args[0], "upload_group_file")

    async def test_missing_or_outside_file_has_actionable_json_error_without_queueing(self):
        bot, key = self.fixture.bot(stored_cid="existing-main")
        context = await self.context(bot, key)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            main(["send", "--context-file", context, "--file", "does-not-exist.txt"])
        error = json.loads(output.getvalue())
        self.assertEqual(error["code"], "FILE_NOT_FOUND")
        self.assertTrue(error["hint"])
        outside = Path("private-file.txt").resolve()
        outside.write_text("must not send", encoding="utf-8")
        with self.assertRaises(ConsoleFileError) as raised:
            execute("send", context, files=[str(outside)])
        self.assertEqual(raised.exception.code, "FILE_OUTSIDE_OUTPUT")
        self.assertFalse(bot._get_main_coordinator().broker.pending(bot._get_main_coordinator().namespace))

    async def test_source_changed_after_queueing_is_not_sent(self):
        bot, key = self.fixture.bot(stored_cid="existing-main")
        bot._is_ws_connected = lambda: True
        context = await self.context(bot, key)
        path = Path(execute("context", context)["output_dir"]) / "report.txt"
        path.write_text("original", encoding="utf-8")
        receipt = execute("send", context, files=[str(path)])
        path.write_text("changed after submission", encoding="utf-8")
        await bot._get_main_coordinator().poll()
        await drain_runtime(bot)
        status = execute("status", context, request_id=receipt["id"])
        self.assertEqual(status["state"], "failed", status)
        self.assertEqual(status["result"]["code"], "FILE_CHANGED")
        bot.send_action.assert_not_awaited()

    async def test_group_upload_and_private_file_appear_in_context_and_inbox(self):
        for group in (True, False):
            bot, _ = self.fixture.bot(stored_cid="existing-main")
            bot.ai.chat.return_value = '{"should_reply":false}'
            await bot._dispatch_event(self.upload(group=group))
            coordinator = bot._get_main_coordinator()
            context = list(coordinator.requests)[-1]
            files = execute("inbox", context)["files"]
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["name"], "资料 文档.txt")
            self.assertIn(files[0]["file_id"], bot.ai.chat.await_args.args[1])
            self.assertNotIn("qq-file-id", bot.ai.chat.await_args.args[1])
            self.assertEqual(len(execute("inbox", context)["files"]), 1)

    async def test_receive_downloads_once_and_exposes_local_path_without_agent_or_qq_send(self):
        bot, key = self.fixture.bot(stored_cid="existing-main")
        await bot._dispatch_event(self.upload())
        coordinator = bot._get_main_coordinator()
        context = self.open_context(bot)
        file_id = execute("inbox", context)["files"][0]["file_id"]
        bot.ai.chat.reset_mock()
        bot.send_action.return_value = {"status": "ok", "data": {"url": "https://example.com/upload.txt"}}
        async def download(url, target, **kwargs):
            self.assertEqual(url, "https://example.com/upload.txt")
            target.write_bytes(b"fixture")
            return "text/plain", url
        with patch("console_files.download_url", new=AsyncMock(side_effect=download)) as fetch:
            request = execute("receive", context, file_id=file_id)
            await coordinator.poll()
            await drain_runtime(bot)
            status = execute("status", context, request_id=request["id"])
            self.assertEqual(status["state"], "completed", status)
            self.assertEqual(Path(status["files"][0]["local_path"]).read_bytes(), b"fixture")
            self.assertTrue(status["files"][0]["sha256"])
            self.assertNotIn("url", status["files"][0])
            repeated = execute("receive", context, file_id=file_id, request_key="same-file-again")
            await coordinator.poll()
            await drain_runtime(bot)
            self.assertEqual(execute("status", context, request_id=repeated["id"])["state"], "completed")
            fetch.assert_awaited_once()
        bot.ai.chat.assert_not_awaited()
        bot.send_group_message.assert_not_awaited()
        bot.send_action.assert_awaited_once()
        self.assertEqual(bot.send_action.await_args.args[0], "get_group_file_url")

    async def test_inbox_and_receive_are_isolated_by_group_and_account(self):
        bot, key = self.fixture.bot(stored_cid="existing-main")
        await bot._dispatch_event(self.upload())
        context = list(bot._get_main_coordinator().requests)[-1]
        broker, token = ConsoleBroker.from_context(context)
        file_id = execute("inbox", context)["files"][0]["file_id"]
        metadata = broker.context(token)
        for field, value in (("scope", "onebot:account_1:group:other"), ("namespace", "onebot:other-account")):
            other = broker.issue_context({**metadata, field: value}, Path("other-contexts"))
            self.assertFalse(execute("inbox", other)["files"])
            with self.assertRaises(ConsoleFileError):
                execute("receive", other, file_id=file_id)

    async def test_failed_or_unsafe_download_returns_file_error_without_chat_notification(self):
        bot, key = self.fixture.bot(stored_cid="existing-main")
        await bot._dispatch_event(self.upload(name="../../unsafe.txt"))
        context = self.open_context(bot)
        file_id = execute("inbox", context)["files"][0]["file_id"]
        bot.send_action.return_value = {"status": "ok", "data": {"url": "http://127.0.0.1/private"}}
        bot.ai.chat.reset_mock()
        with patch("console_files.download_url", new=AsyncMock()) as fetch:
            request = execute("receive", context, file_id=file_id)
            await bot._get_main_coordinator().poll()
            await drain_runtime(bot)
        status = execute("status", context, request_id=request["id"])
        self.assertEqual(status["state"], "failed")
        self.assertTrue(status["result"]["error"])
        fetch.assert_not_awaited()
        bot.ai.chat.assert_not_awaited()
        bot.send_group_message.assert_not_awaited()

    async def test_unknown_file_send_is_not_repeated_when_queue_is_polled(self):
        bot, key = self.fixture.bot(stored_cid="existing-main")
        bot._is_ws_connected = lambda: True
        context = await self.context(bot, key)
        path = Path(execute("context", context)["output_dir"]) / "report.txt"
        path.write_text("report", encoding="utf-8")
        bot.send_action.return_value = {"status": "failed", "retcode": 1200,
            "message": "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg EventRet: {result:0}"}
        receipt = execute("send", context, files=[str(path)])
        coordinator = bot._get_main_coordinator()
        await coordinator.poll()
        await drain_runtime(bot)
        status = execute("status", context, request_id=receipt["id"])
        self.assertEqual(status["state"], "unknown", status)
        self.assertFalse(status["retryable"])
        self.assertEqual(status["files"][0]["state"], "unknown")
        await coordinator.poll()
        await drain_runtime(bot)
        bot.send_action.assert_awaited_once()
        bot.send_group_message.assert_not_awaited()

    def test_schema_and_invalid_arguments_are_json_and_mcp_errors_are_structured(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["schema"])
        self.assertIn("receive", json.loads(output.getvalue())["actions"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            main(["unsupported"])
        self.assertEqual(json.loads(output.getvalue())["code"], "INVALID_ARGUMENT")
        response = handle({"id": 1, "method": "tools/call", "params": {"name": "qqbot_send_files",
            "arguments": {"context_file": "missing-context.json", "files": ["x.txt"]}}})
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(json.loads(response["result"]["content"][0]["text"])["code"], "CLI_REQUIRED")

    async def test_napcat_listener_budget_and_rpc_budget_are_distinct_and_caller_params_unchanged(self):
        bot, _ = self.fixture.bot()
        bot.send_action = OneBotClient.send_action.__get__(bot)
        packets = []
        class WS:
            closed = False
            async def send(_, raw):
                packet = json.loads(raw)
                packets.append(packet)
                await bot._dispatch_event({"echo": packet["echo"], "status": "ok", "retcode": 0,
                                           "data": {"message_id": "accepted"}})
        bot.ws = WS()
        params = {"group_id": 12345678, "message": "fixture"}
        original_timeout = asyncio.timeout
        with patch("onebot_client.asyncio.timeout", wraps=original_timeout) as timer:
            await bot.send_action("send_group_msg", params)
        self.assertEqual(packets[0]["params"]["timeout"], 30000)
        self.assertEqual(timer.call_args.args[0], 35)
        self.assertNotIn("timeout", params)
        self.assertFalse(bot._pending_echoes)

    async def test_napcat_timeout_rejection_marks_text_outbox_unknown_not_failed(self):
        bot, _ = self.fixture.bot()
        bot.send_action = OneBotClient.send_action.__get__(bot)
        class WS:
            closed = False
            async def send(_, raw):
                echo = json.loads(raw)["echo"]
                await bot._dispatch_event({"echo": echo, "status": "failed", "retcode": 1200,
                    "message": "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg"})
        bot.ws = WS()
        bot._get_runtime()
        with self.assertRaises(SendUnknown):
            await OneBotClient.send_group_message(bot, 12345678, "fixture")
        row = bot._runtime.store.db.execute("SELECT state FROM outbox").fetchone()
        self.assertEqual(row[0], "unknown")
        self.assertFalse(bot._pending_echoes)

    async def test_persona_switch_selects_independent_main_without_erasing_original_cid(self):
        for official in (False, True):
            bot, key = self.fixture.bot(official, stored_cid="existing-main")
            await self.fixture.receive(bot, official, "before", "普通聊天")
            generation = bot.ai.get_or_create_session(key).generation
            bot.ai.chat.reset_mock()
            with patch.object(bot, "_cancel_all_media") as cancel:
                bot.personas.set_active("治愈系温柔学姐")
                bot.sync_system_prompt()
                bot.ai.chat.assert_not_awaited()
                cancel.assert_not_called()
            await self.fixture.receive(bot, official, "after", "继续刚才的话题", elapsed=1)
            prompt = bot.ai.chat.await_args.args[1]
            new_key = bot.ai.chat.await_args.args[0]
            self.assertNotEqual(new_key, key)
            self.assertIn("@rules:", prompt)
            self.assertIn("温柔体贴", bot.ai.chat.await_args.kwargs["system_prompt"])
            self.assertIsNone(bot.ai.get_or_create_session(new_key).conversation_id)
            self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "existing-main")
            self.assertEqual(bot.ai.get_or_create_session(key).generation, generation)
            await self.fixture.receive(bot, official, "later", "再继续", elapsed=2)
            self.assertNotIn('"type": "persona_update"', bot.ai.chat.await_args.args[1])

    async def test_private_cq_files_decode_attributes_and_keep_text_without_native_ids(self):
        cq = "[CQ:file,file=qq&#44;id&amp;token,name=资料&#44;A&#91;1&#93;&amp;.txt,file_size=7]"
        for raw_only in (False, True):
            bot, _ = self.fixture.bot(stored_cid="existing-main")
            event = {"post_type": "message", "message_type": "private", "user_id": 456,
                     "message_id": "cq-file", "raw_message": "请处理这个文件 " + cq}
            if not raw_only:
                event["message"] = event["raw_message"]
            parsed = incoming_files(event)
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0]["native_id"], "qq,id&token")
            self.assertEqual(parsed[0]["name"], "资料,A[1]&.txt")
            await bot._dispatch_event(event)
            context = list(bot._get_main_coordinator().requests)[-1]
            self.assertEqual(execute("inbox", context)["files"][0]["name"], "资料,A[1]&.txt")
            prompt = bot.ai.chat.await_args.args[1]
            self.assertIn("请处理这个文件", prompt)
            self.assertNotIn("qq,id&token", prompt)
            self.assertNotIn("qq&#44;id", prompt)
        self.assertFalse(incoming_files({"message": "&#91;CQ:file,file=plain-text&#93;"}))

    async def test_later_message_can_send_prior_output_but_other_group_cannot(self):
        bot, key = self.fixture.bot(stored_cid="existing-main")
        context = await self.context(bot, key)
        path = Path(execute("context", context)["output_dir"]) / "previous-report.txt"
        path.write_text("previous result", encoding="utf-8")
        await self.fixture.receive(bot, False, "next-message", "发一下刚才的文件", elapsed=1)
        coordinator = bot._get_main_coordinator()
        later = coordinator.issue_chat_context(coordinator.contexts[key], key)[0]
        request = execute("send", later, files=[str(path)])
        self.assertEqual(request["state"], "submitted")
        execute("cancel", later, request_id=request["id"])
        await bot._handle_group_message({"post_type": "message", "message_type": "group", "group_id": 87654321,
            "user_id": 456, "message_id": "other-group", "raw_message": "另一个群的消息"})
        other_ctx = list(coordinator.requests.values())[-1]
        other = coordinator.issue_chat_context(other_ctx, other_ctx.ai_session_key)[0]
        with self.assertRaises(ConsoleFileError) as error:
            execute("send", other, files=[str(path)])
        self.assertEqual(error.exception.code, "FILE_OUTSIDE_OUTPUT")

    async def test_receive_can_be_cancelled_while_download_is_running(self):
        bot, _ = self.fixture.bot(stored_cid="existing-main")
        await bot._dispatch_event(self.upload())
        coordinator = bot._get_main_coordinator()
        context = self.open_context(bot)
        bot.ai.chat.reset_mock()
        bot.send_action.return_value = {"status": "ok", "data": {"url": "https://example.com/file"}}
        started = asyncio.Event()
        async def download(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        with patch("console_files.download_url", new=AsyncMock(side_effect=download)):
            request = execute("receive", context)
            await coordinator.poll()
            await asyncio.wait_for(started.wait(), 2)
            execute("cancel", context, request_id=request["id"])
            await coordinator.poll()
            await drain_runtime(bot)
        self.assertEqual(execute("status", context, request_id=request["id"])["state"], "cancelled")
        self.assertEqual(execute("inbox", context)["files"][0]["state"], "available")
        bot.ai.chat.assert_not_awaited()
        bot.send_group_message.assert_not_awaited()

    async def test_incomplete_download_is_not_accepted_as_received(self):
        bot, _ = self.fixture.bot(stored_cid="existing-main")
        await bot._dispatch_event(self.upload(size=7))
        coordinator = bot._get_main_coordinator()
        context = self.open_context(bot)
        bot.send_action.return_value = {"status": "ok", "data": {"url": "https://example.com/file"}}
        async def download(url, target, **kwargs):
            target.write_bytes(b"bad")
        with patch("console_files.download_url", new=AsyncMock(side_effect=download)):
            request = execute("receive", context)
            await coordinator.poll()
            await drain_runtime(bot)
        status = execute("status", context, request_id=request["id"])
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["result"]["code"], "FILE_SIZE_MISMATCH")
        self.assertEqual(execute("inbox", context)["files"][0]["state"], "available")
        self.assertFalse(status["files"])

    async def test_send_requires_cli_while_mcp_queries_and_receive_share_the_queue(self):
        bot, key = self.fixture.bot(stored_cid="existing-main")
        context = await self.context(bot, key)
        path = Path(execute("context", context)["output_dir"]) / "result.txt"
        path.write_text("result", encoding="utf-8")
        def call(name, **args):
            response = handle({"id": 1, "method": "tools/call", "params": {"name": name,
                "arguments": {"context_file": context, **args}}})["result"]
            self.assertFalse(response["isError"], response)
            return json.loads(response["content"][0]["text"])
        rejected = handle({"id": 1, "method": "tools/call", "params": {"name": "qqbot_send_files",
            "arguments": {"context_file": context, "files": [str(path)]}}})["result"]
        self.assertTrue(rejected["isError"])
        self.assertEqual(json.loads(rejected["content"][0]["text"])["code"], "CLI_REQUIRED")
        request = execute("send", context, files=[str(path)])
        self.assertEqual(request["id"], execute("send", context, files=[str(path)])["id"])
        self.assertEqual(call("qqbot_status_task", request_id=request["id"])["state"], "submitted")
        call("qqbot_cancel_task", request_id=request["id"])
        await bot._get_main_coordinator().poll()
        await bot._dispatch_event(self.upload())
        context = self.open_context(bot)
        file_id = call("qqbot_inbox")["files"][0]["file_id"]
        received = call("qqbot_receive_file", file_id=file_id)
        self.assertEqual(received["id"], execute("receive", context, file_id=file_id)["id"])
        self.assertEqual(call("qqbot_status_task", request_id=received["id"])["state"], "submitted")


if __name__ == "__main__":
    unittest.main()
