"""The command's actual result must survive a short main-agent introduction."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.files import ConsoleFileError
from messaging import SendUnknown
from onebot_client import OneBotClient
from test_ai_fixture import drain_runtime


class CommandResultApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.context = self.broker.issue_context({"namespace": "fixture", "scope": "group",
            "sender_id": "456", "session_key": "group_123", "is_group": True}, self.root / "contexts")
        _, self.token = ConsoleBroker.from_context(self.context)

    def ready(self, kind="bot_command", *, success=True, answer="支持命令：\n/help\n/status\n/next", files=None):
        request = self.broker._enqueue(self.token, self.broker.context(self.token), kind, "/help", {})
        self.broker.update(request["id"], "ready", {"success": success,
            "result": {"answer": answer, "files": files or []}, "error": "" if success else answer})
        return request["id"], answer

    def plan(self, request_id):
        return json.loads(self.broker.record(request_id)["result"])["delivery"]

    def test_short_intro_cannot_replace_full_command_result(self):
        request, answer = self.ready()
        execute("deliver", self.context, request_id=request, text="前辈，这是完整指令列表~", finish=True)
        self.assertEqual(self.plan(request)["text"], "前辈，这是完整指令列表~\n\n" + answer)

    def test_no_intro_uses_command_body_without_generic_completion(self):
        request, answer = self.ready()
        execute("deliver", self.context, request_id=request, finish=True)
        self.assertEqual(self.plan(request)["text"], answer)

    def test_real_cli_approval_without_text_keeps_the_complete_result(self):
        request, answer = self.ready()
        cli = Path(__file__).with_name("console_tools") / "cli.py"
        process = subprocess.run([sys.executable, str(cli), "deliver", "--context-file", self.context,
            "--request-id", request, "--finish"], capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["state"], "committed")
        self.assertEqual(self.plan(request)["text"], answer)

    def test_already_included_body_is_not_duplicated_and_approval_stays_immutable(self):
        request, answer = self.ready()
        text = "前辈，结果如下：\n\n" + answer
        first = execute("deliver", self.context, request_id=request, text=text)
        self.assertEqual(execute("deliver", self.context, request_id=request, text=text), first)
        self.assertEqual(self.plan(request)["text"], text)
        with self.assertRaises(ConsoleFileError):
            execute("deliver", self.context, request_id=request, text="另一个开场白")

    def test_agent_heading_does_not_duplicate_heading_from_body(self):
        request, answer = self.ready()
        execute("deliver", self.context, request_id=request, text="支持命令：")
        self.assertEqual(self.plan(request)["text"], answer)

    def test_file_delivery_uses_approved_notice_without_broadcasting_report(self):
        request, _ = self.ready(kind="media_command", answer="已准备文件的内部说明", files=[{"path": "report.txt"}])
        execute("deliver", self.context, request_id=request, text="文件在这里")
        self.assertEqual(self.plan(request)["text"], "文件在这里")


class CommandResultDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def bot(self, official=False):
        bot, _ = self.fixture.bot(official, stored_cid="command-main")
        bot.app_config.bot.master_id = "456"
        if not official:
            bot._is_ws_connected = lambda: True
            bot.send_group_message = OneBotClient.send_group_message.__get__(bot)
        bot.app_config.bot.max_reply_length = 70
        return bot

    def agent(self, bot, *, intro="前辈，这是控制台当前支持的完整指令列表~", legacy=False):
        events = []
        async def respond(key, prompt, **options):
            event = next(json.loads(line) for line in prompt.splitlines() if line.startswith('{"type": "task_result"'))
            binding = next(json.loads(line) for line in prompt.splitlines() if line.startswith('{"console_context_file":'))
            events.append(event)
            self.assertTrue(event["command_result"])
            self.assertNotIn("text", binding["deliver_call"]["params"])
            receipt = execute("deliver", binding["console_context_file"], request_id=event["request_id"], text=intro, finish=True)
            self.assertEqual(receipt["state"], "committed")
            if legacy:
                # Simulate a pre-fix approved plan before any platform send.
                broker, _ = ConsoleBroker.from_context(binding["console_context_file"])
                payload = json.loads(broker.record(event["request_id"])["result"])
                payload["delivery"]["text"] = intro
                broker.update(event["request_id"], "ready", payload)
            return ""
        bot.ai.chat = AsyncMock(side_effect=respond)
        return events

    def sent(self, bot, message=None, official=False):
        if official:
            return "".join(call.args[0] if call.args else call.kwargs["content"] for call in message.reply.await_args_list)
        return "".join(segment["data"]["text"] for call in bot.send_action.await_args_list
            if call.args[0] == "send_group_msg" for segment in call.args[1]["message"] if segment["type"] == "text")

    async def test_help_contains_whole_list_with_intro_on_both_transports(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot = self.bot(official)
                events = self.agent(bot)
                message = await self.fixture.receive(bot, official, "help-source", "/help")
                await drain_runtime(bot)
                body = events[0]["answer"]
                sent = self.sent(bot, message, official)
                self.assertEqual(sent, "前辈，这是控制台当前支持的完整指令列表~\n\n" + body)
                for command in ("/help", "/status", "/generate-image", "/next"):
                    self.assertIn(command, sent)
                if official:
                    self.assertLessEqual(message.reply.await_count, 5)
                    self.assertTrue(all("message_reference" not in call.kwargs for call in message.reply.await_args_list))
                else:
                    self.assertTrue(all(segment["type"] == "text" for call in bot.send_action.await_args_list for segment in call.args[1]["message"]))
                count = message.reply.await_count if official else bot.send_action.await_count
                await bot._get_main_coordinator().poll()
                await drain_runtime(bot)
                self.assertEqual(message.reply.await_count if official else bot.send_action.await_count, count)

    async def test_status_needs_only_bound_approval_command_and_no_intro(self):
        bot = self.bot()
        events = self.agent(bot, intro="")
        await self.fixture.receive(bot, False, "status-source", "/status")
        await drain_runtime(bot)
        self.assertEqual(self.sent(bot), events[0]["answer"])
        self.assertIn("当前模型", self.sent(bot))

    async def test_command_error_includes_actual_reason(self):
        bot = self.bot()
        events = self.agent(bot, intro="命令处理结果：")
        await self.fixture.receive(bot, False, "invalid-source", "/status extra")
        await drain_runtime(bot)
        self.assertFalse(events[0]["success"])
        self.assertIn(events[0]["answer"], self.sent(bot))
        self.assertIn("不接受附加参数", self.sent(bot))

    async def test_media_paging_keeps_candidate_ids_and_last_item(self):
        bot = self.bot()
        library = Path("library").resolve()
        library.mkdir()
        for index in range(7):
            (library / f"report-{index}.txt").write_text("fixture", encoding="utf-8")
        bot.app_config.media.local_library_roots = [str(library)]
        events = self.agent(bot, intro="查到这些文件：")
        for index, command in enumerate(("/library report", "/next")):
            bot.send_action.reset_mock()
            await self.fixture.receive(bot, False, f"page-{index}", command, elapsed=index)
            await drain_runtime(bot)
            self.assertIn(events[index]["answer"], self.sent(bot))
            self.assertIn(f"第 {index + 1} 页", self.sent(bot))
            self.assertIn("/pick", self.sent(bot))

    async def test_legacy_ready_approval_gets_body_before_first_send(self):
        bot = self.bot()
        events = self.agent(bot, legacy=True)
        await self.fixture.receive(bot, False, "help-source", "/help")
        await drain_runtime(bot)
        self.assertIn(events[0]["answer"], self.sent(bot))

    async def test_member_markup_inside_command_body_stays_literal_on_both_transports(self):
        for official in (False, True):
            bot = self.bot(official)
            bot.memory_mgr.get_memory_prompt = lambda *args, **kwargs: '引用文本：<@456> [CQ:at,qq=456]'
            self.agent(bot, intro="")
            message = await self.fixture.receive(bot, official, "memory-source", "/memory")
            await drain_runtime(bot)
            text = self.sent(bot, message, official)
            self.assertIn("引用文本", text)
            self.assertNotIn("<@456>", text)
            self.assertIn("[CQ:at,qq=456]", text)
            if not official:
                self.assertTrue(all(segment["type"] == "text" for call in bot.send_action.await_args_list for segment in call.args[1]["message"]))

    async def test_offline_reply_preserves_entire_body_and_resumes_without_model(self):
        bot = self.bot()
        connected = False
        bot._is_ws_connected = lambda: connected
        events = self.agent(bot)
        await self.fixture.receive(bot, False, "help-source", "/help")
        await drain_runtime(bot)
        bot.send_action.assert_not_awaited()
        model_calls = bot.ai.chat.await_count
        connected = True
        bot._text_outbox.wake()
        await bot._text_outbox.worker
        console = bot._get_main_coordinator()
        console.retry_after.clear()
        await console.poll()
        await drain_runtime(bot)
        self.assertIn(events[0]["answer"], self.sent(bot))
        self.assertEqual(bot.ai.chat.await_count, model_calls)
        self.assertEqual(console.broker.record(events[0]["request_id"])["state"], "delivered")

    async def test_unknown_chunk_is_not_replayed(self):
        bot = self.bot()
        events = self.agent(bot)
        bot.send_action.side_effect = SendUnknown("platform receipt missing")
        await self.fixture.receive(bot, False, "help-source", "/help")
        await drain_runtime(bot)
        console = bot._get_main_coordinator()
        self.assertEqual(console.broker.record(events[0]["request_id"])["state"], "unknown")
        console.retry_after.clear()
        await console.poll()
        await drain_runtime(bot)
        bot.send_action.assert_awaited_once()
