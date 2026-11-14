"""Offline regressions for CLI-only delivery, worker dispatch and real targets."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from console_tools.broker import ConsoleBroker
from console_tools.chat_commands import parse_command
from console_tools.cli import execute
from console_tools.files import ConsoleFileError
from messaging import SendUnknown
from onebot_client import OneBotClient
from test_ai_fixture import drain_runtime


class ConsoleTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="QQ CLI 中文 ' ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.payload = {"namespace": "onebot:a", "scope": "onebot:a:group:123", "session_key": "group_123",
            "sender_id": "456", "is_group": True, "bot_id": "999", "search_enabled": True,
            "reply_sources": [{"id": "100", "sender_id": "456", "text": "第一条", "received_at": 1},
                              {"id": "101", "sender_id": "789", "text": "第二条", "received_at": 2}]}
        self.context = self.broker.issue_context(self.payload, self.root / "contexts")
        _, self.token = ConsoleBroker.from_context(self.context)

    def test_default_message_has_no_quote_or_mention_and_is_idempotent(self):
        first = execute("message", self.context, text="收到")
        second = execute("message", self.context, text="收到")
        self.assertEqual(first, second)
        item, = self.broker.staged_messages(self.token)
        self.assertEqual((item["reply_to"], item["mention"]), ("", ""))
        self.assertEqual(execute("status", self.context, request_id=first["id"])["state"], "staged")

    def test_opaque_official_message_ids_are_preserved_exactly(self):
        identity = "ROBOT1.0_example+/=="
        context = self.broker.issue_context({**self.payload, "reply_sources": [
            {"id": identity, "sender_id": "456", "text": "真实消息", "received_at": 3}]}, self.root / "official")
        execute("message", context, text="精确回应", reply_to=identity)
        _, token = ConsoleBroker.from_context(context)
        self.assertEqual(self.broker.staged_messages(token)[0]["reply_to"], identity)

    def test_message_target_must_be_real_and_belong_to_same_group(self):
        other = self.broker.issue_context({**self.payload, "scope": "other", "reply_sources": [
            {"id": "foreign", "sender_id": "888", "text": "别群", "received_at": 3}]}, self.root / "other")
        for values in ({"reply_to": "foreign"}, {"reply_to": "m2"}, {"mention": "888"},
                       {"mention": "all"}, {"mention": "999"}, {"text": "[CQ:at,qq=789]"}, {"text": "<@789>"}):
            with self.subTest(values=values), self.assertRaises(ConsoleFileError):
                execute("message", self.context, **{"text": "回应", **values})
        receipt = execute("message", self.context, text="回应第二条", reply_to="101", mention="789")
        self.assertEqual(receipt["state"], "staged")
        self.assertNotIn("foreign", [row["id"] for row in execute("messages", self.context)["messages"]])
        self.assertEqual(execute("messages", other)["messages"][0]["id"], "foreign")

    def test_wait_cancels_pending_messages_and_turn_sealing_rejects_late_sends(self):
        message = execute("message", self.context, text="不要发送")
        execute("wait", self.context)
        self.assertEqual(self.broker.staged_messages(self.token), [])
        self.assertEqual(execute("status", self.context, request_id=message["id"])["state"], "cancelled")
        with self.assertRaises(ConsoleFileError):
            execute("message", self.context, text="迟到消息")

    def test_worker_cannot_message_submit_send_or_approve(self):
        worker = self.broker.issue_context({**self.payload, "parent_request_id": "original"}, self.root / "worker")
        for action, args in (("message", {"text": "不准发送"}), ("submit", {"task": "派任务"}),
                             ("deliver", {"request_id": "original"}), ("send", {"files": ["x"]})):
            with self.subTest(action=action), self.assertRaises(PermissionError):
                execute(action, worker, **args)

    def test_search_and_generation_have_distinct_worker_types(self):
        search = execute("search", self.context, query="sufur2 公开战绩")
        generation = execute("generate-image", self.context, prompt="雪中的猫", count=2, size="1024x1024")
        self.assertEqual(self.broker.record(search["id"])["kind"], "search")
        self.assertEqual(self.broker.record(generation["id"])["kind"], "generate_image")
        self.assertEqual(json.loads(self.broker.record(generation["id"])["task"])["count"], 2)

    def test_cli_preserves_unicode_and_multiline_prompt_in_real_child_process(self):
        prompt = self.root / "图片 描述.txt"
        prompt.write_text("雪中的白猫\n招牌写着 '你好'", encoding="utf-16")
        cli = str(Path(__file__).with_name("console_tools") / "cli.py")
        result = subprocess.run([sys.executable, cli, "generate-image", "--context-file", self.context,
            "--prompt-file", str(prompt)], capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        description = json.loads(self.broker.record(receipt["id"])["task"])
        self.assertEqual(description["prompt"], prompt.read_text(encoding="utf-16"))

    def test_regex_commands_are_anchored_and_aliases_canonicalized(self):
        self.assertEqual(parse_command("／STATUS").canonical, "/status")
        self.assertEqual(parse_command("/draw 一只白猫").canonical, "/generate-image 一只白猫")
        self.assertIsNone(parse_command("例子：/clear", natural=True))
        for value in ("/statusx", "/status extra", "/clear\n/exec abc", "/search"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_command(value)

    def test_approval_is_scoped_and_cannot_be_changed_or_replayed(self):
        request = execute("search", self.context, query="查资料")
        self.broker.update(request["id"], "ready", {"success": True, "result": {"files": []}})
        for text in ("[CQ:at,qq=789]", "<@789>", '<qqbot-at-user id="789"/>'):
            with self.subTest(text=text), self.assertRaises(ConsoleFileError):
                execute("deliver", self.context, request_id=request["id"], text=text)
        foreign = self.broker.issue_context({**self.payload, "scope": "other"}, self.root / "other")
        with self.assertRaises(ValueError):
            execute("deliver", foreign, request_id=request["id"])
        execute("deliver", self.context, request_id=request["id"], text="资料在这里")
        with self.assertRaises(ConsoleFileError):
            execute("deliver", self.context, request_id=request["id"], text="改成另一条")
        self.assertEqual(execute("deliver", self.context, request_id=request["id"], text="资料在这里")["state"], "ready")


class CliWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def context(self, bot):
        return list(bot._get_main_coordinator().requests)[-1]

    def result_event(self, prompt):
        return next((json.loads(line) for line in prompt.splitlines() if line.startswith('{"type": "task_result"')), None)

    def bot(self, official=False):
        bot, key = self.fixture.bot(official, stored_cid="same-cid")
        from test_ai_fixture import ScriptedConsoleAgentMock
        bot.ai.chat = ScriptedConsoleAgentMock(return_value="内部最终文本")
        if not official:
            bot._is_ws_connected = lambda: True
        return bot, key

    async def test_final_text_and_legacy_json_are_never_sent(self):
        for response in ("这不应自动发送", '{"should_reply":true,"reply":"不应发送","tasks":[{"type":"search","task":"不应执行"}]}'):
            bot, _ = self.bot()
            bot.ai.chat.return_value = response
            await self.fixture.receive(bot, False, "source", "普通问题")
            await drain_runtime(bot)
            bot.send_group_message.assert_not_awaited()
            self.assertEqual(bot._get_main_coordinator().broker.active(bot._get_main_coordinator().namespace), [])

    async def test_cli_message_and_explicit_target_work_on_both_transports(self):
        for official in (False, True):
            bot, _ = self.bot(official)
            async def respond(key, prompt, **options):
                execute("speech", self.context(bot), operation="ready")
                execute("message", self.context(bot), text="第一句", reply_to="source", mention="456")
                execute("message", self.context(bot), text="第二句")
                return "这段最终文本不应发送"
            bot.ai.chat.side_effect = respond
            message = await self.fixture.receive(bot, official, "source", "说两句")
            if official:
                self.assertEqual(message.reply.await_count, 2)
                self.assertEqual(message.reply.await_args_list[0].kwargs["message_reference"]["message_id"], "source")
                self.assertNotIn("message_reference", message.reply.await_args_list[1].kwargs)
            else:
                first, second = [call.args[1] for call in bot.send_group_message.await_args_list]
                self.assertEqual((first.quote, first.mention), ("source", "456"))
                self.assertEqual((second.quote, second.mention), ("", ""))

    async def test_search_worker_does_not_block_chat_and_result_returns_to_main(self):
        bot, key = self.bot()
        bot.app_config.bot.search_enabled = True
        entered, release = asyncio.Event(), asyncio.Event()
        sequence = []
        async def respond(session_key, prompt, **options):
            if session_key.startswith("__work__:task:"):
                sequence.append("worker")
                entered.set()
                await release.wait()
                options["response_trace"].extend([
                    {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "search_web"}]},
                    {"type": "TOOL_RESULT", "status": "DONE", "content": "https://example.com/stats"}])
                return '{"answer":"公开战绩报告","sources":[{"title":"战绩","url":"https://example.com/stats"}],"files":[]}'
            self.assertEqual(session_key, key)
            event = self.result_event(prompt)
            if event:
                sequence.append("main-result")
                self.assertEqual(event["answer"], "公开战绩报告")
                bot.send_action.assert_not_awaited()
                execute("deliver", self.context(bot), request_id=event["request_id"], text="战绩查好了")
            elif '"text": "查询公开战绩"' in prompt:
                execute("search", self.context(bot), query="查询公开战绩并记录来源")
            else:
                execute("speech", self.context(bot), operation="ready")
                execute("message", self.context(bot), text="我还在，可以继续聊")
            return "内部结束"
        bot.ai.chat.side_effect = respond
        await self.fixture.receive(bot, False, "search-source", "查询公开战绩")
        await asyncio.wait_for(entered.wait(), 2)
        await self.fixture.receive(bot, False, "chat", "还在吗", elapsed=1)
        self.assertEqual(bot.send_group_message.await_count, 1)
        release.set()
        await drain_runtime(bot)
        self.assertEqual(sequence, ["worker", "main-result"])
        self.assertEqual(bot.send_action.await_count, 1)
        notice = bot.send_group_message.await_args.args[1]
        self.assertEqual((notice.body, notice.quote, notice.mention), ("战绩查好了", "", ""))
        rows = execute("tasks", self.context(bot))["tasks"]
        self.assertEqual(rows[0]["state"], "delivered")

    async def test_ready_results_wait_for_main_approval(self):
        bot, _ = self.bot()
        async def respond(key, prompt, **options):
            if key.startswith("__work__:task:"):
                return '{"answer":"详细成果","files":[]}'
            if not self.result_event(prompt):
                execute("submit", self.context(bot), task="整理资料")
            return "内部最终文本"
        bot.ai.chat.side_effect = respond
        await self.fixture.receive(bot, False, "source", "整理资料")
        await drain_runtime(bot)
        bot.send_action.assert_not_awaited()
        bot.send_group_message.assert_not_awaited()
        coordinator = bot._get_main_coordinator()
        ready = coordinator.broker.completed(coordinator.namespace, bot._image_scope("12345678"))
        self.assertEqual(ready[0]["state"], "ready")
        count = bot.ai.chat.await_count
        coordinator.retry_after.clear()
        await coordinator.poll()
        self.assertEqual(bot.ai.chat.await_count, count)

    async def test_control_command_uses_console_worker_and_main_delivery(self):
        bot, _ = self.bot()
        bot.app_config.bot.master_id = "456"
        async def respond(key, prompt, **options):
            event = self.result_event(prompt)
            self.assertIsNotNone(event)
            self.assertIn("当前模型", event["answer"])
            execute("deliver", self.context(bot), request_id=event["request_id"], text=event["answer"])
            return "结束"
        bot.ai.chat.side_effect = respond
        await self.fixture.receive(bot, False, "status-source", "/status")
        await drain_runtime(bot)
        self.assertEqual(bot.ai.chat.await_count, 1)
        self.assertIn("当前模型", bot.send_group_message.await_args.args[1].body)

    async def test_unknown_send_receipt_is_not_replayed(self):
        bot, _ = self.bot()
        ids = []
        async def respond(key, prompt, **options):
            execute("speech", self.context(bot), operation="ready")
            ids.append(execute("message", self.context(bot), text="一次发送")["id"])
            return "结束"
        bot.ai.chat.side_effect = respond
        bot.send_group_message.side_effect = SendUnknown("回执丢失")
        await self.fixture.receive(bot, False, "source", "说一句")
        coordinator = bot._get_main_coordinator()
        self.assertEqual(execute("status", self.context(bot), request_id=ids[0])["state"], "unknown")
        coordinator.retry_after.clear()
        await coordinator.poll()
        self.assertEqual(bot.send_group_message.await_count, 1)

    async def test_split_messages_do_not_add_automatic_quotes_or_repeat_metadata(self):
        bot, _ = self.bot()
        bot.send_group_message = OneBotClient.send_group_message.__get__(bot)
        bot.app_config.bot.max_reply_length = 30
        async def respond(key, prompt, **options):
            execute("speech", self.context(bot), operation="ready")
            execute("message", self.context(bot), text="这是完整的一条较长回复。" * 9, reply_to="source", mention="456")
            return "结束"
        bot.ai.chat.side_effect = respond
        await self.fixture.receive(bot, False, "source", "说长一点")
        packets = [call.args[1]["message"] for call in bot.send_action.await_args_list if call.args[0] == "send_group_msg"]
        self.assertGreater(len(packets), 1)
        self.assertEqual([segment["type"] for segment in packets[0]][:2], ["reply", "at"])
        self.assertTrue(all([segment["type"] for segment in packet] == ["text"] for packet in packets[1:]))

    async def test_official_quota_merges_content_only_for_identical_targets(self):
        bot, _ = self.bot(True)
        bot.app_config.bot.max_reply_length = 10
        identities = []
        async def respond(key, prompt, **options):
            execute("speech", self.context(bot), operation="ready")
            for index in range(8):
                identities.append(execute("message", self.context(bot), text=f"完整段落{index}。" * 4,
                    reply_to="source" if index < 3 else "")["id"])
            return "结束"
        bot.ai.chat.side_effect = respond
        message = await self.fixture.receive(bot, True, "source", "一起回复")
        self.assertLessEqual(message.reply.await_count, 5)
        calls = message.reply.await_args_list
        full_text = "".join(call.kwargs["content"] for call in calls)
        for index in range(8):
            self.assertEqual(full_text.count(f"完整段落{index}。"), 4)
        for identity in identities:
            self.assertEqual(execute("status", self.context(bot), request_id=identity)["state"], "accepted")
        # A merged command never acquires its neighbour's quotation target.
        for call in calls:
            if "完整段落3。" in call.kwargs["content"]:
                self.assertNotIn("message_reference", call.kwargs)

    async def test_all_split_outbox_receipts_are_tracked_and_unknown_is_never_replayed(self):
        bot, _ = self.bot()
        bot.send_group_message = OneBotClient.send_group_message.__get__(bot)
        bot.app_config.bot.max_reply_length = 12
        connected = False
        bot._is_ws_connected = lambda: connected
        bot._get_runtime()
        identities = []
        async def respond(key, prompt, **options):
            execute("speech", self.context(bot), operation="ready")
            identities.append(execute("message", self.context(bot), text="完整回复需要多个分段。" * 5)["id"])
            return "结束"
        bot.ai.chat.side_effect = respond
        await self.fixture.receive(bot, False, "source", "回复")
        first = execute("status", self.context(bot), request_id=identities[0])
        self.assertEqual(first["state"], "queued")
        outbox_ids = first["result"]["data"]["outbox_ids"]
        self.assertGreater(len(outbox_ids), 1)
        sent = 0
        async def send(action, params, **options):
            nonlocal sent
            sent += 1
            if sent == 2:
                raise SendUnknown("第二段回执未知")
            return {"status": "ok", "data": {"message_id": str(sent)}}
        bot.send_action.side_effect = send
        connected = True
        bot._text_outbox.wake()
        await bot._text_outbox.worker
        self.assertEqual(execute("status", self.context(bot), request_id=identities[0])["state"], "unknown")
        count = bot.send_action.await_count
        await bot._get_main_coordinator().poll()
        self.assertEqual(bot.send_action.await_count, count)

    async def test_library_paging_and_pick_prepare_results_for_main_approval(self):
        bot, key = self.bot()
        bot.app_config.bot.master_id = "456"
        root = Path("library").resolve()
        root.mkdir()
        for index in range(7):
            (root / f"report-{index}.txt").write_text(f"original report {index}", encoding="utf-8")
        bot.app_config.media.local_library_roots = [str(root)]
        events = []
        async def respond(session_key, prompt, **options):
            self.assertEqual(session_key, key)
            event = self.result_event(prompt)
            self.assertIsNotNone(event)
            events.append(event)
            execute("deliver", self.context(bot), request_id=event["request_id"], text=event["answer"] or "已准备文件")
            return "结束"
        bot.ai.chat.side_effect = respond
        for index, command in enumerate(("/library report", "/next", "/pick 1")):
            await self.fixture.receive(bot, False, f"command-{index}", command, elapsed=index)
            await drain_runtime(bot)
        self.assertEqual(len(events), 3)
        self.assertIn("第 1 页", events[0]["answer"])
        self.assertIn("第 2 页", events[1]["answer"])
        self.assertEqual(len(events[2]["files"]), 1)
        bot.send_action.assert_awaited_once()
        self.assertEqual(bot.send_action.await_args.args[0], "upload_group_file")
        self.assertIn("original report", Path(bot.send_action.await_args.args[1]["file"]).read_text(encoding="utf-8"))


class ImageGenerationTests(unittest.IsolatedAsyncioTestCase):
    def test_another_tools_success_does_not_prove_generation_succeeded(self):
        from skills.task_skills import completed_tools
        trace = [
            {"type": "PLANNER_RESPONSE", "tool_calls": [
                {"name": "search_web", "id": "search"}, {"name": "generate_image", "id": "image"}]},
            {"type": "TOOL_RESULT", "tool_call_id": "search", "status": "DONE"},
            {"type": "TOOL_RESULT", "tool_call_id": "image", "status": "DONE", "isError": True},
        ]
        self.assertEqual(completed_tools(trace), {"search_web"})

    async def test_native_generation_accepts_images_without_generation_tool_logs(self):
        from skills.image_generation import GenerateImageSkill
        from skills.task_skills import RunTaskSkill
        from test_agent_artifacts import AgentArtifactTests, task_result
        fixture = AgentArtifactTests()
        fixture.setUp()
        try:
            calls = []
            async def generate(key, prompt, **options):
                calls.append(prompt)
                self.assertIn("反重力", prompt)
                manifest = json.loads(task_result(prompt, response_trace=options["response_trace"], source=""))
                # Generation may omit native tool logs entirely. Only actual
                # final-image viewing evidence is available in this fixture.
                del options["response_trace"][:2]
                manifest["sources"] = []
                return json.dumps(manifest)
            fixture.ai.chat.side_effect = generate
            result = await GenerateImageSkill().execute({"prompt": "橙色的横向人物插画"}, fixture.context())
            self.assertTrue(result.success, result.message)
            self.assertEqual(result.action_type, "prepared")
            fixture.ctx.send_action.assert_not_awaited()
            self.assertEqual(len(calls), 1)
            with self.assertRaisesRegex(ValueError, "数量"):
                await RunTaskSkill()._prepare_manifest({"_image_generation": True}, fixture.ctx, fixture.service,
                    "生成图片", Path(result.data["archive"]).parent, {"id": "x"}, {"files": []}, [])
        finally:
            fixture.tearDown()

    async def test_api_generation_produces_validated_images_without_search_or_qq_send(self):
        from skills.image_generation import GenerateImageSkill
        from test_agent_artifacts import AgentArtifactTests
        fixture = AgentArtifactTests()
        fixture.setUp()
        try:
            fixture.service.config.image_generation_provider = "api"
            fixture.service.config.image_generation_api_url = "http://127.0.0.1:9000/v1/images/generations"
            fixture.service.config.image_generation_model = "configured-model"
            image = io.BytesIO()
            Image.new("RGB", (128, 128), "orange").save(image, format="PNG")
            payload = json.dumps({"data": [{"b64_json": base64.b64encode(image.getvalue()).decode()}]}).encode()
            class Content:
                async def iter_chunked(self, size):
                    yield payload
            class Response:
                status, content = 200, Content()
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
            class Session:
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
                def post(self, url, **options):
                    self.options = options
                    return Response()
            session = Session()
            with patch("skills.image_generation.aiohttp.ClientSession", return_value=session):
                result = await GenerateImageSkill().execute({"prompt": "一只白猫"}, fixture.context())
            self.assertTrue(result.success, result.message)
            self.assertFalse(session.options["allow_redirects"])
            self.assertTrue(Path(result.data["files"][0]["local_path"]).is_file())
            fixture.ai.chat.assert_not_awaited()
            fixture.ctx.send_action.assert_not_awaited()
        finally:
            fixture.tearDown()
