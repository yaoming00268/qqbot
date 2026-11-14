"""Behavior checks for attachment handoff and the restricted chat transport."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from PIL import Image

from agent_transport import configured_command, resolve_model
from antigravity_client import AntigravityClient
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.server import handle
from main_agent_policy import main_cascade_config
from skills.base import SkillContext, SkillResult
from skills.image_generation import GenerateImageSkill
from test_ai_fixture import drain_runtime


def mcp(name, **arguments):
    result = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": name, "arguments": arguments}})["result"]
    return json.loads(result["content"][0]["text"])


class AttachmentAndMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.memory = self.root / "group.json"
        self.memory.write_text('{"summary":"记得上次的话题"}', encoding="utf-8")
        self.payload = {"namespace": "account", "scope": "group", "sender_id": "owner", "is_group": True,
                        "source_message_id": "current", "quoted_message_id": "quoted", "main_agent": True,
                        "memory_files": [str(self.memory)], "reply_sources": []}
        self.context = self.broker.issue_context(self.payload, self.root / "contexts")
        _, self.token = ConsoleBroker.from_context(self.context)

    def upload(self, source, sender="owner", scope="group", count=1):
        return self.broker.record_files("account", scope, source, sender,
            [{"native_id": source + str(index), "name": f"{source}{index}.txt", "size": 7}
             for index in range(count)])

    def test_every_content_command_binds_current_and_quoted_attachments(self):
        current = self.upload("current", count=2)
        quoted = self.upload("quoted", sender="peer")
        self.upload("unrelated", sender="peer")
        for command in ("code", "process", "search", "generate-image", "image", "video", "file",
                        "task", "command", "analyze", "summarize", "translate", "write"):
            with self.subTest(command=command):
                receipt = execute("command", self.context, text=f"/{command} 完整要求")
                inputs = json.loads(self.broker.record(receipt["id"])["result"])["input_file_ids"]
                self.assertEqual(set(inputs), set(current + quoted))

    def test_explicit_selection_and_submission_snapshot_do_not_drift_to_new_uploads(self):
        explicit = self.upload("explicit")
        self.upload("current")
        receipt = execute("submit", self.context, task_type="generate_image", task="参考 " + explicit[0])
        self.upload("later")
        self.assertEqual(json.loads(self.broker.record(receipt["id"])["result"])["input_file_ids"], explicit)

    def test_only_own_recent_file_is_used_for_legacy_code_fallback(self):
        expected = self.upload("previous")
        self.upload("newer-other", sender="peer")
        self.upload("cross-group", scope="another")
        self.assertEqual(self.broker.task_inputs(self.token, "写代码", "code")["input_file_ids"], expected)
        self.assertEqual(self.broker.task_inputs(self.token, "画一只猫", "generate_image")["input_file_ids"], [])

    def test_quoted_and_explicit_files_survive_inbox_pagination(self):
        quoted = self.upload("quoted", sender="peer")
        explicit = self.upload("explicit", sender="peer")
        self.upload("newer", count=105)
        self.assertNotIn(quoted[0], {item["file_id"] for item in self.broker.inbox(self.token, 100)})
        self.assertEqual(self.broker.task_inputs(self.token, "按文件画图", "generate_image")["input_file_ids"], quoted)
        self.assertEqual(self.broker.task_inputs(self.token, explicit[0], "generate_image")["input_file_ids"], explicit)

    def test_resident_reply_preserves_cli_transactions_and_worker_restrictions(self):
        args = {"action": "reply", "context_file": self.context, "params": {"text": ["第一句", "第二句"]}}
        first = mcp("qqbot_cli", **args)
        self.assertEqual(first["state"], "committed")
        self.assertEqual(mcp("qqbot_cli", **args), first)
        self.assertEqual([item["text"] for item in self.broker.staged_messages(self.token)], ["第一句", "第二句"])
        worker = self.broker.issue_context({**self.payload, "parent_request_id": "task"}, self.root / "worker")
        self.assertEqual(mcp("qqbot_cli", **{**args, "context_file": worker})["code"], "FORBIDDEN")
        self.assertFalse(mcp("qqbot_cli", action="reply", context_file=worker,
                             params={"context_file": self.context, "text": ["绕过"]})["ok"])

    def test_main_cannot_read_source_or_control_desktop_through_the_dispatcher(self):
        self.assertEqual(mcp("qqbot_read_memory", context_file=self.context, path=str(self.memory))["text"],
                         self.memory.read_text(encoding="utf-8"))
        outside = self.root / "source.py"
        outside.write_text("print('private')", encoding="utf-8")
        self.assertEqual(mcp("qqbot_read_memory", context_file=self.context, path=str(outside))["code"], "FORBIDDEN")
        self.assertEqual(mcp("qqbot_cli", action="desktop", operation="screenshot", context_file=self.context)["code"], "FORBIDDEN")
        self.assertEqual(mcp("qqbot_cli", action="sandbox", operation="read", context_file=self.context,
                             params={"path": "source.py"})["code"], "FORBIDDEN")
        execute("reply", self.context, text="完成")
        self.assertEqual(mcp("qqbot_read_memory", context_file=self.context, path=str(self.memory))["text"],
                         self.memory.read_text(encoding="utf-8"))
        self.broker.seal_messages(self.token)
        self.assertEqual(mcp("qqbot_read_memory", context_file=self.context, path=str(self.memory))["code"], "TURN_CLOSED")

    def test_controlled_memory_reads_count_as_evidence(self):
        trace = [{"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "qqbot_read_memory", "id": "r",
                  "arguments": {"context_file": self.context, "path": str(self.memory)}}]},
                 {"type": "TOOL_RESULT", "tool_call_id": "r", "status": "DONE", "content": "memory"}]
        self.assertIn(AntigravityClient._memory_file_key(self.memory), AntigravityClient._completed_memory_reads(trace))
        trace[-1]["content"] = '{"ok":false,"error":"read failed"}'
        self.assertEqual(AntigravityClient._completed_memory_reads(trace), set())


class WorkerInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_quoted_uploads_bind_after_chat_cache_eviction_on_both_transports(self):
        for official in (False, True):
            for group in (False, True):
                with self.subTest(official=official, group=group):
                    bot, _ = self.fixture.bot(official)
                    console = bot._get_main_coordinator()
                    target = "12345678" if group else "456"
                    source_key = ("group_" if official else "onebot_group_") + target if group else (
                        "c2c_" if official else "onebot_private_") + target
                    source = bot._make_media_context(session_key=source_key, user_id="456",
                        conversation_id=target, message_id="persisted-upload", is_group=group)
                    expected = console.broker.record_files(console.namespace, source.scope,
                        "persisted-upload", "789" if group else "456",
                        [{"native_id": "old-upload", "name": "reference.txt", "size": 7}])
                    captured = []
                    async def capture(ctx, text):
                        context_file, _, _ = console.issue_chat_context(ctx, bot._chat_session_key(ctx.ai_session_key))
                        receipt = execute("command", context_file, text=text)
                        inputs = json.loads(console.broker.record(receipt["id"])["result"])["input_file_ids"]
                        captured.append((ctx.quoted_message_id, inputs))
                        return receipt
                    with patch.object(console, "submit_chat_command", new=AsyncMock(side_effect=capture)), \
                         patch.object(bot, "_stored_image_reference", new=AsyncMock(return_value=None)):
                        if official:
                            message = SimpleNamespace(id="quoted-command", group_openid=target, content="/生图 按文件绘图",
                                author=SimpleNamespace(member_openid="456", user_openid="456", username="User", bot=False),
                                mentions=[], attachments=None, message_reference=SimpleNamespace(message_id="persisted-upload"),
                                reply=AsyncMock())
                            if group:
                                await bot._handle_group_message(message, is_at_event=False)
                            else:
                                await bot._process_c2c_message(message)
                        else:
                            message = {"post_type": "message", "message_type": "group" if group else "private",
                                "self_id": 999, "user_id": 456, "message_id": "quoted-command",
                                "raw_message": "[CQ:reply,id=persisted-upload] /生图 按文件绘图",
                                "message": [{"type": "reply", "data": {"id": "persisted-upload"}},
                                            {"type": "text", "data": {"text": "/生图 按文件绘图"}}],
                                "sender": {"nickname": "User"}, **({"group_id": 12345678} if group else {})}
                            await (bot._handle_group_message(message) if group else bot._handle_private_message(message))
                        await drain_runtime(bot)
                    self.assertEqual(captured, [("persisted-upload", expected)])

    async def test_inputs_reach_code_generation_analysis_and_search_workers(self):
        bot, key = self.fixture.bot(stored_cid="main")
        console = bot._get_main_coordinator()
        source = Path("uploaded.txt").resolve()
        source.write_text("user material", encoding="utf-8")
        reference = Path("data/cache/images/reference.png").resolve()
        reference.parent.mkdir(parents=True)
        Image.new("RGB", (8, 8), "orange").save(reference)
        async def receive(client, broker, token, identity, ctx):
            return {"file_id": identity, "name": source.name, "local_path": str(source)}
        async def work(params, context):
            self.assertEqual({Path(item["local_path"]) for item in params["_input_files"]}, {source, reference})
            return SkillResult(True, "完成", data={"files": []})
        with patch("console_files.receive_qq_file", new=AsyncMock(side_effect=receive)), \
             patch("skills.task_skills.RunTaskSkill.execute", new=AsyncMock(side_effect=work)) as worker:
            for index, command in enumerate(("code", "生图", "总结", "翻译", "search")):
                mid = "command-" + str(index)
                console.broker.record_files(console.namespace, bot._image_scope("12345678"), mid, "456",
                    [{"native_id": mid, "name": source.name, "size": source.stat().st_size}])
                ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
                    message_id=mid, text=f"/{command} 使用附件", is_group=True,
                    quoted_images=[{"local_path": str(reference), "vision_verified": True}])
                await console.submit_chat_command(ctx, ctx.text)
                await drain_runtime(bot)
            self.assertEqual(worker.await_count, 5)

    async def test_compact_rules_and_tools_are_bound_without_changing_persona_or_cid(self):
        bot, key = self.fixture.bot(stored_cid="keep-me")
        await self.fixture.receive(bot, False, "one", "你好")
        first = bot.ai.chat.await_args
        self.assertIn(bot.personas.get_active_persona().prompt, first.kwargs["system_prompt"])
        self.assertNotIn("工具与扩展技能", first.kwargs["system_prompt"])
        self.assertIn("declarativeMixinConfig", first.kwargs["cascade_config"]["plannerConfig"])
        await self.fixture.receive(bot, False, "two", "还在吗", elapsed=1)
        self.assertNotIn("@rules:", bot.ai.chat.await_args.args[1])
        self.assertIn('"console_context_file":', bot.ai.chat.await_args.args[1])
        self.assertNotIn('"reply_call":', bot.ai.chat.await_args.args[1])
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "keep-me")

    async def test_natural_final_continues_until_explicit_finish_then_waits_for_native_stop(self):
        from test_agent_reply_latency import inbound, final
        bot, key = self.fixture.bot(stored_cid="cancel-pending")
        ai = bot.ai
        ai.chat = AntigravityClient.chat.__get__(ai)
        ai._is_ready = True
        ai._brain_dir = str(Path("brain").resolve())
        release = asyncio.Event()
        entered = asyncio.Event()
        async def cancel(*args, **kwargs):
            entered.set()
            await release.wait()
        ai._cancel_native_conversation = AsyncMock(side_effect=cancel)
        path = Path(ai._brain_dir) / "cancel-pending/.system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True)
        calls = []
        async def send(args, **options):
            calls.append(args)
            context = list(bot._get_main_coordinator().requests)[-1]
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(inbound(len(calls) * 10, args[-1].splitlines()[0])) + "\n")
            if len(calls) == 1:
                execute("reply", context, text="直接交付")
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(final(11, "")) + "\n")
            else:
                self.assertIn("turn_continuation", args[-1])
                execute("contacts", context, operation="upsert", params={"user_id": "456", "revision": 0,
                    "reason": "回复后记录习惯", "notes": "说完后才结束"})
                execute("finish", context)
            await asyncio.Event().wait()
        ai._run_agentapi_cmd = AsyncMock(side_effect=send)
        turn = asyncio.create_task(self.fixture.receive(bot, False, "source", "说一句"))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            self.assertEqual(len(calls), 2)
            self.assertFalse(turn.done())
            self.assertFalse(release.is_set())
            bot.send_group_message.assert_awaited_once()
            self.assertEqual(str(bot.send_group_message.await_args.args[1]), "直接交付")
            release.set()
            await asyncio.wait_for(turn, 3)
            bot.send_group_message.assert_awaited_once()
            ai._cancel_native_conversation.assert_awaited_once_with("cancel-pending", kill_background_tasks=False)
            self.assertEqual(ai.get_or_create_session(key).conversation_id, "cancel-pending")
        finally:
            release.set()
            if not turn.done():
                turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)


class ConfiguredTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.failed = False
        async def rpc(request):
            method = request.match_info["method"]
            body = await request.json()
            self.calls.append((method, body))
            if method == "GetCascadeModelConfigData":
                return web.json_response({"clientModelConfigs": [{"label": "Gemini Flash", "modelId": "gemini-flash",
                    "modelOrAlias": {"model": "ADVERTISED_MODEL"}}]})
            if method == "StartCascade":
                return web.json_response({"cascadeId": "restricted-cid"})
            if self.failed:
                return web.json_response({"message": "policy rejected"}, status=400)
            return web.json_response({})
        app = web.Application()
        app.router.add_post("/exa.language_server_pb.LanguageServerService/{method}", rpc)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.env = {"ANTIGRAVITY_LS_ADDRESS": f"127.0.0.1:{port}", "ANTIGRAVITY_CSRF_TOKEN": "fixture",
                    "ANTIGRAVITY_PROJECT_ID": "project"}
        self.client = SimpleNamespace(_connection_generation=0, _content_is_blocked=lambda _: False,
                                     _invalid_media=AntigravityClient._invalid_media,
                                     _cancel_native_conversation=AsyncMock())

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_new_and_existing_conversations_use_the_restricted_direct_transport(self):
        config = main_cascade_config()
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("must not spawn CLI")):
            created = await configured_command(self.client, ["new-conversation", "--model=flash", "hello"], config,
                                               timeout=2, env=self.env)
            cid = created["response"]["newConversation"]["conversationId"]
            await configured_command(self.client, ["send-message", cid, "next"], config, timeout=2, env=self.env)
        sends = [body for method, body in self.calls if method == "SendUserCascadeMessage"]
        self.assertEqual([body["items"][0]["text"] for body in sends], ["hello", "next"])
        self.assertTrue(all(body["cascadeConfig"]["plannerConfig"]["toolConfig"]["runCommand"]["forceDisable"] for body in sends))
        self.assertEqual(sends[0]["cascadeConfig"]["plannerConfig"]["requestedModel"], {"model": "ADVERTISED_MODEL"})
        self.assertNotIn("requestedModel", config["plannerConfig"])

    async def test_rejected_policy_never_retries_without_restrictions(self):
        self.failed = True
        with self.assertRaisesRegex(RuntimeError, "policy rejected"):
            await configured_command(self.client, ["new-conversation", "--model=flash", "hello"], main_cascade_config(),
                                     timeout=2, env=self.env)
        self.client._cancel_native_conversation.assert_awaited_once_with("restricted-cid")
        self.assertEqual(sum(method == "SendUserCascadeMessage" for method, _ in self.calls), 1)

    def test_missing_model_is_reported_instead_of_changing_tier(self):
        with self.assertRaisesRegex(RuntimeError, "pro"):
            resolve_model({"clientModelConfigs": [{"label": "Gemini Flash", "modelOrAlias": {"model": "FLASH"}}]}, "pro")


class ReferenceImageApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_reference_images_use_multipart_edits_and_preserve_input(self):
        from test_startup_participation import StartupParticipationTests
        fixture = StartupParticipationTests()
        fixture.setUp()
        runner = None
        try:
            bot, key = fixture.bot()
            source = Path("reference.png").resolve()
            Image.new("RGB", (8, 8), "orange").save(source)
            original = source.read_bytes()
            seen = []
            async def edit(request):
                fields = []
                reader = await request.multipart()
                async for part in reader:
                    fields.append((part.name, await part.read()))
                seen.extend(fields)
                return web.json_response({"data": [{"b64_json": base64.b64encode(original).decode()}]})
            app = web.Application()
            app.router.add_post("/v1/images/edits", edit)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            service = bot._get_media_service()
            service.config.image_generation_provider = "api"
            service.config.image_generation_api_url = f"http://127.0.0.1:{port}/v1/images/generations"
            service.config.image_generation_model = "configured-model"
            ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
                                         message_id="image-edit", text="改成蓝色", is_group=True)
            context = SkillContext(key, "12345678", "456", is_group=True, media_context=ctx,
                                   media_service=service, ai_client=bot.ai)
            with patch("skills.task_skills.RunTaskSkill._deliver_result", new=AsyncMock(return_value=SkillResult(True, "done"))):
                result = await GenerateImageSkill().execute({"prompt": "改成蓝色", "_input_files": [{"local_path": str(source)}]}, context)
            self.assertTrue(result.success, result.message)
            self.assertEqual(dict(seen)["image"], original)
            self.assertEqual(dict(seen)["prompt"].decode(), "改成蓝色")
            self.assertEqual(source.read_bytes(), original)
        finally:
            if runner:
                await runner.cleanup()
            await fixture.asyncTearDown()


if __name__ == "__main__":
    unittest.main()
