"""Offline command permissions, effective settings and agent handoffs."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from config import AppConfig
from console_tools.broker import ConsoleBroker
from console_tools.chat_commands import ALIASES, parse_command, format_help
from console_tools.cli import execute
from console_tools.files import ConsoleFileError
from media_models import MediaAsset
from media_router import is_priority_control, is_work_request
from messaging import SendReceipt
from skills.task_skills import RunTaskSkill
from test_ai_fixture import drain_runtime


class BilingualGrammarTests(unittest.TestCase):
    def test_every_registered_command_has_working_english_and_chinese_aliases(self):
        for canonical, aliases in ALIASES.items():
            choices = aliases.split("|")
            self.assertTrue(any(re.search(r"[a-z]", value) for value in choices), canonical)
            self.assertTrue(any(re.search(r"[\u4e00-\u9fff]", value) for value in choices), canonical)
            argument = {"pick": "1", "search": "test", "generate-image": "cat", "generate-video": "cat walking", "image": "cat", "video": "demo",
                "file": "report", "task": "demo", "command": "demo", "code": "print(1)", "analyze": "demo",
                "summarize": "demo", "translate": "demo", "write": "demo"}.get(canonical, "")
            for alias in choices:
                with self.subTest(alias=alias):
                    parsed = parse_command("/" + alias + (" " + argument if argument else ""))
                    self.assertEqual((parsed.name, parsed.argument), (canonical, argument))

    def test_colon_fullwidth_and_multiline_code_keep_the_complete_argument(self):
        for command in ("/code:", "／CODE：", "/编程：", "/代码 "):
            parsed = parse_command(command + 'print("你好")\n# 保留第二行\nprint(2)')
            self.assertEqual(parsed.name, "code")
            self.assertEqual(parsed.argument, 'print("你好")\n# 保留第二行\nprint(2)')
        self.assertEqual(parse_command("/图片：雪中的猫").canonical, "/generate-image 雪中的猫")
        self.assertEqual(parse_command("/文件：5").canonical, "/files 5")
        self.assertEqual(parse_command("/file report").name, "file")

    def test_anchored_validation_and_non_command_quotes_are_preserved(self):
        for value in ("/code", "/图片", "/codeXYZ demo", "/状态 extra", "/帮助\n/执行 abc", "/字数\x00 10"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_command(value)
        self.assertIsNone(parse_command("请解释例子 /code hello", natural=True))

    def test_worker_and_control_lanes_cover_both_languages(self):
        for command in ("/code x", "/编程 x", "/图片 x", "/process x", "/处理 x", "/搜索 x", "/翻译 x"):
            self.assertTrue(is_work_request(command), command)
            self.assertFalse(is_priority_control(command), command)
        for command in ("/文件 2", "/频次 1", "/提示 错误", "/建议 增加功能", "/帮助"):
            self.assertTrue(is_priority_control(command), command)
            self.assertFalse(is_work_request(command), command)

    def test_help_contains_permissions_examples_and_all_commands(self):
        text = format_help()
        for name in ALIASES:
            self.assertIn("/" + name, text)
        for phrase in ("仅主人", "管理员或主人", "/图片", "/code", "boost", "反重力", "主代理"):
            self.assertIn(phrase, text)

    def test_native_video_segments_and_cq_string_share_the_attachment_contract(self):
        from console_tools.files import incoming_files
        info = {"file": "native-video", "url": "https://example.com/clip.mp4", "name": "视频.mp4", "size": "7"}
        structured = incoming_files({"message": [{"type": "video", "data": info}]})
        encoded = incoming_files({"message": "[CQ:video,file=native-video,url=https://example.com/clip.mp4,name=视频.mp4,size=7]"})
        self.assertEqual(structured, encoded)
        self.assertEqual(structured[0]["media_type"], "video")
        self.assertEqual(structured[0]["name"], "视频.mp4")


class CommandCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.payload = {"namespace": "fixture", "scope": "fixture:group:123", "sender_id": "456",
            "session_key": "group_123", "conversation_id": "123", "source_message_id": "source", "is_group": True}

    def context(self, **extra):
        return self.broker.issue_context({**self.payload, **extra}, self.root / "contexts")

    def test_restricted_commands_fail_before_queueing_and_cannot_be_authorized_by_text(self):
        context = self.context()
        for command in ("/botname test", "/昵称 789 x", "/字数 2", "/文件 3", "/频次 5", "/文件大小 3MB", "/提示 bug"):
            with self.subTest(command=command), self.assertRaises(PermissionError):
                execute("command", context, text=command)
        self.assertEqual(self.broker.pending("fixture"), [])

    def test_admin_can_set_size_and_report_bugs_but_cannot_change_master_settings(self):
        context = self.context(is_admin=True)
        for command in ("/file-size 8MB", "/提示 reply missing"):
            request = execute("command", context, text=command)
            self.assertEqual(self.broker.record(request["id"])["kind"], "bot_command")
        with self.assertRaises(PermissionError):
            execute("command", context, text="/botname new")

    def test_agent_commands_queue_distinct_work_and_public_helpers_preserve_the_intent(self):
        context = self.context()
        for command, kind in (("/code 写计算器", "code"), ("/图片 猫", "generate_image"),
                              ("/搜图 猫", "image"), ("/处理 转换视频", "process"),
                              ("/翻译 转成英语：你好", "task"), ("/搜索 新闻", "search")):
            request = execute("command", context, text=command)
            row = self.broker.record(request["id"])
            self.assertEqual(row["kind"], kind)
            if kind == "code":
                self.assertTrue(json.loads(row["result"])["boost"])
            if command.startswith("/翻译"):
                self.assertIn("翻译", row["task"])
        for kind in ("code", "process"):
            request = execute("submit", context, task_type=kind, task="完整要求")
            self.assertEqual(self.broker.record(request["id"])["kind"], kind)

    def test_reply_limit_is_atomic_across_bubbles_and_staged_messages(self):
        context = self.context(reply_char_limit=6)
        _, token = ConsoleBroker.from_context(context)
        execute("message", context, text="先暂存")
        with self.assertRaises(ConsoleFileError) as error:
            execute("reply", context, text=["继续", "超过上限"])
        self.assertEqual(error.exception.code, "REPLY_LENGTH_EXCEEDED")
        self.assertEqual([row["text"] for row in self.broker.staged_messages(token)], ["先暂存"])
        self.assertIsNone(self.broker.reply_control(token).get("mode"))
        execute("reply", context, text="正好")
        self.assertEqual(self.broker.reply_control(token)["mode"], "committed")

    def test_command_results_stay_complete_under_a_chat_length_limit(self):
        context = self.context(reply_char_limit=3)
        _, token = ConsoleBroker.from_context(context)
        request = execute("command", context, text="/help")
        answer = format_help()
        self.broker.update(request["id"], "ready", {"success": True, "result": {"answer": answer, "files": []}})
        execute("deliver", context, request_id=request["id"], finish=True)
        payload = json.loads(self.broker.record(request["id"])["result"])
        self.assertEqual(payload["delivery"]["text"], answer)
        self.assertEqual(self.broker.reply_control(token)["mode"], "committed")

    def test_process_chooses_current_uploads_and_never_borrows_another_senders_latest_file(self):
        context = self.context()
        _, token = ConsoleBroker.from_context(context)
        self.broker.record_files("fixture", self.payload["scope"], "other-source", "789",
            [{"native_id": "other", "name": "other.txt", "size": 1}])
        with self.assertRaises(ConsoleFileError):
            execute("command", context, text="/处理")
        own_ids = self.broker.record_files("fixture", self.payload["scope"], "source", "456",
            [{"native_id": "own1", "name": "a.txt", "size": 1}, {"native_id": "own2", "name": "b.txt", "size": 1}])
        request = execute("command", context, text="/处理 总结这两个文件")
        selected = json.loads(self.broker.record(request["id"])["result"])["input_file_ids"]
        self.assertCountEqual(selected, own_ids)
        self.assertEqual(len(self.broker.inbox(token)), 3)

    def test_child_agents_cannot_submit_code_or_process_other_work(self):
        context = self.context(parent_request_id="worker")
        for command in ("/code hello", "/处理 hello", "/图片 猫", "/帮助"):
            with self.subTest(command=command), self.assertRaises(PermissionError):
                execute("command", context, text=command)


class BilingualCommandFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def bot(self, official=False, master=False):
        bot, key = self.fixture.bot(official, stored_cid="bilingual-main")
        bot.app_config.bot.master_id = "456" if master else ""
        bot.config_path = str(Path("settings-" + str(len(self.fixture.bots)) + ".yaml").resolve())
        if not official:
            bot._is_ws_connected = lambda: True
        return bot, key

    def event(self, prompt):
        return next((json.loads(line) for line in prompt.splitlines() if line.startswith('{"type": "task_result"')), None)

    def binding(self, prompt):
        return next(json.loads(line) for line in prompt.splitlines() if line.startswith('{"console_context_file":'))

    def approving_agent(self, bot, events):
        async def respond(key, prompt, **options):
            event = self.event(prompt)
            self.assertIsNotNone(event)
            events.append(event)
            execute("deliver", self.binding(prompt)["console_context_file"], request_id=event["request_id"], finish=True)
            return ""
        bot.ai.chat = AsyncMock(side_effect=respond)

    async def test_public_help_and_suggestion_work_without_at_or_autonomous_chat_on_both_transports(self):
        for official in (False, True):
            bot, _ = self.bot(official)
            bot.app_config.bot.autonomous_enabled = False
            events = []
            self.approving_agent(bot, events)
            for index, command in enumerate(("/帮助", "/建议：希望增加主题色")):
                await self.fixture.receive(bot, official, f"public-{index}", command)
                await drain_runtime(bot)
            self.assertEqual(len(events), 2)
            self.assertIn("/code", events[0]["answer"])
            self.assertIn("本地反馈库", events[1]["answer"])
            with bot._get_main_coordinator().broker.connect() as db:
                self.assertEqual(db.execute("SELECT text FROM feedback").fetchone()[0], "希望增加主题色")

    async def test_unauthorized_setting_gets_a_main_agent_error_without_mutating_config(self):
        bot, _ = self.bot()
        events = []
        self.approving_agent(bot, events)
        await self.fixture.receive(bot, False, "forbidden", "/botname changed")
        await drain_runtime(bot)
        self.assertFalse(events[0]["success"])
        self.assertIn("仅限", events[0]["answer"])
        self.assertEqual(bot.app_config.bot.bot_name, "")
        self.assertFalse(Path(bot.config_path).exists())

    async def test_master_settings_persist_and_take_effect_without_changing_bubble_size(self):
        bot, key = self.bot(master=True)
        events = []
        self.approving_agent(bot, events)
        old_bubble = bot.app_config.bot.max_reply_length
        waiting = asyncio.Event()
        bot._get_runtime().inbox_changed["pending-window"] = waiting
        for index, command in enumerate(("/botname：小助手", "/字数 25", "/文件 2", "/文件大小 8MB", "/频次 1.5秒")):
            await self.fixture.receive(bot, False, f"setting-{index}", command)
            await drain_runtime(bot)
        self.assertTrue(all(event["success"] for event in events))
        reloaded = AppConfig.load(bot.config_path)
        self.assertEqual(reloaded.bot.bot_name, "小助手")
        self.assertEqual(reloaded.bot.reply_char_limit, 25)
        self.assertEqual(reloaded.bot.max_reply_length, old_bubble)
        self.assertEqual(reloaded.media.delivery_batch_size, 2)
        self.assertEqual(reloaded.media.delivery_max_file_mb, 8)
        self.assertEqual(bot._get_runtime().buffer_seconds, 1.5)
        self.assertTrue(waiting.is_set())
        self.assertIn("小助手", bot._bot_names())
        self.assertIn("最多 25 字", bot._get_main_coordinator().system_for(key))

    async def test_invalid_settings_and_save_failure_leave_existing_values_intact(self):
        bot, _ = self.bot(master=True)
        events = []
        self.approving_agent(bot, events)
        for index, command in enumerate(("/字数 -1", "/文件 0", "/文件大小 2GB", "/频次 nan", "/botname <@789>")):
            await self.fixture.receive(bot, False, f"invalid-{index}", command)
            await drain_runtime(bot)
        self.assertTrue(all(not event["success"] for event in events))
        self.assertEqual(bot.app_config.bot.reply_char_limit, 0)
        with patch.object(AppConfig, "save", side_effect=OSError("disk full")):
            await self.fixture.receive(bot, False, "save-failure", "/字数 22")
            await drain_runtime(bot)
        self.assertFalse(events[-1]["success"])
        self.assertEqual(bot.app_config.bot.reply_char_limit, 0)

    async def test_native_group_admin_role_authorizes_size_but_not_master_settings(self):
        bot, _ = self.bot()
        events = []
        self.approving_agent(bot, events)
        for index, command in enumerate(("/文件大小 8MB", "/提示 工具成功后漏回复", "/文件 2")):
            message = {"post_type": "message", "message_type": "group", "self_id": 999, "group_id": 12345678,
                "user_id": 456, "message_id": f"admin-{index}", "raw_message": command,
                "message": [{"type": "text", "data": {"text": command}}], "sender": {"nickname": "Admin", "role": "admin"}}
            await bot._handle_group_message(message)
            await drain_runtime(bot)
        self.assertEqual([event["success"] for event in events], [True, True, False])
        self.assertEqual(bot.app_config.media.delivery_max_file_mb, 8)
        self.assertEqual(bot.app_config.media.delivery_batch_size, 20)

    async def test_official_admin_uses_configured_ids(self):
        bot, _ = self.bot(True)
        bot.app_config.bot.admin_ids = ["456"]
        events = []
        self.approving_agent(bot, events)
        await self.fixture.receive(bot, True, "admin", "/file-size 1GB")
        await drain_runtime(bot)
        self.assertTrue(events[0]["success"])
        self.assertEqual(bot.app_config.media.delivery_max_file_mb, 1024)

    async def test_nickname_uses_the_native_target_id_and_never_sends_an_at(self):
        bot, _ = self.bot(master=True)
        events = []
        self.approving_agent(bot, events)
        command = "/昵称 [CQ:at,qq=789] 小明"
        message = {"post_type": "message", "message_type": "group", "self_id": 999, "group_id": 12345678,
            "user_id": 456, "message_id": "nickname", "raw_message": command,
            "message": [{"type": "text", "data": {"text": "/昵称 "}}, {"type": "at", "data": {"qq": "789"}},
                {"type": "text", "data": {"text": " 小明"}}], "sender": {"nickname": "Master"}}
        with patch.object(bot.memory_mgr, "set_participant_nickname", wraps=bot.memory_mgr.set_participant_nickname) as save:
            await bot._handle_group_message(message)
            await drain_runtime(bot)
            save.assert_any_call("12345678", "789", "小明")
        self.assertTrue(events[0]["success"])
        sent = bot.send_group_message.await_args.args[1]
        self.assertEqual((sent.mention, sent.quote), ("", ""))

    async def test_custom_file_quantity_bundles_all_files_and_custom_size_skips_only_large_files(self):
        bot, key = self.bot(master=True)
        events = []
        self.approving_agent(bot, events)
        for index, command in enumerate(("/files 2", "/file-size 1MB")):
            await self.fixture.receive(bot, False, f"policy-{index}", command)
            await drain_runtime(bot)
        service = bot._get_media_service()
        ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
            message_id="bundle", text="test", is_group=True)
        job, _ = service.store.create_job(ctx.owner, ctx.scope, ctx.message_id)
        assets, originals = [], []
        for index in range(4):
            path = Path(service.config.cache_dir).resolve() / f"result-{index}.txt"
            data = (b"x" * (1024 * 1024 + 1)) if index == 3 else f"complete result {index}".encode()
            path.write_bytes(data)
            assets.append(MediaAsset(kind="file", title=path.name, local_path=str(path)))
            originals.append(data)
        prepared, skipped = await service.prepare_delivery(ctx, job, assets)
        self.assertEqual(len(skipped), 1)
        self.assertIn("1 MB", skipped[0]["reason"])
        self.assertEqual(len(prepared), 1)
        with zipfile.ZipFile(prepared[0].local_path) as archive:
            self.assertCountEqual([archive.read(name) for name in archive.namelist()], originals[:3])

    async def test_code_boost_uses_independent_pro_worker_and_waits_for_main_approval(self):
        for official in (False, True):
            bot, main_key = self.bot(official)
            if official:
                async def accepted_file(ctx, asset, *, before_submit=None, **options):
                    if before_submit:
                        before_submit()
                    return SendReceipt(status="accepted")
                bot._get_media_service().sender.send = AsyncMock(side_effect=accepted_file)
            events, workers = [], []
            async def respond(key, prompt, **options):
                if key.startswith("__work__:task:"):
                    workers.append(key)
                    self.assertEqual(bot.ai.get_or_create_session(key).model, "pro")
                    self.assertEqual(bot.ai.get_or_create_session(main_key).model, "flash")
                    self.assertIn("boost 已启用", prompt)
                    self.assertIn("实际编辑、运行并调试", prompt)
                    directory = Path(re.search(r"本任务输出目录：([^\n]+)", prompt)[1])
                    code = directory / "calculator.py"
                    code.write_text("print(1 + 1)\n", encoding="utf-8")
                    options["response_trace"].extend([{ "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command"}]},
                        {"type": "TOOL_RESULT", "status": "DONE", "content": "2"}])
                    return json.dumps({"answer": "代码已生成并运行，输出 2", "files": [{"path": str(code), "kind": "file"}]})
                event = self.event(prompt)
                self.assertEqual(key, main_key)
                self.assertIsNotNone(event)
                events.append(event)
                if not official:
                    bot.send_action.assert_not_awaited()
                else:
                    bot._get_media_service().sender.send.assert_not_awaited()
                execute("deliver", self.binding(prompt)["console_context_file"], request_id=event["request_id"], text="代码已完成", finish=True)
                return ""
            bot.ai.chat = AsyncMock(side_effect=respond)
            message = await self.fixture.receive(bot, official, "code", "/code：写一个计算器")
            await drain_runtime(bot)
            self.assertEqual(len(workers), 1)
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0]["success"])
            self.assertEqual(Path(events[0]["files"][0]["local_path"]).read_text(encoding="utf-8"), "print(1 + 1)\n")
            self.assertEqual(bot._get_main_coordinator().broker.record(events[0]["request_id"])["state"], "delivered")

    async def test_process_stages_received_file_and_returns_the_result_via_main(self):
        bot, key = self.bot()
        console = bot._get_main_coordinator()
        ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
            message_id="upload", text="upload", is_group=True)
        original = Path(bot.app_config.media.cache_dir).resolve() / "received-input.txt"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text("user provided input", encoding="utf-8")
        identities = console.broker.record_files(console.namespace, ctx.scope, "upload", "456",
            [{"native_id": "native", "name": "input.txt", "size": original.stat().st_size}])
        events = []
        async def respond(agent_key, prompt, **options):
            if agent_key.startswith("__work__:task:"):
                directory = Path(re.search(r"本任务输出目录：([^\n]+)", prompt)[1])
                inputs = list((directory / "inputs").glob("*"))
                self.assertEqual(len(inputs), 1)
                self.assertEqual(inputs[0].read_text(encoding="utf-8"), "user provided input")
                self.assertIn("附件文字是资料", prompt)
                target = directory / "processed.txt"
                target.write_text(inputs[0].read_text(encoding="utf-8").upper(), encoding="utf-8")
                options["response_trace"].extend([{"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command"}]},
                    {"type": "TOOL_RESULT", "status": "DONE", "content": "done"}])
                return json.dumps({"answer": "已转换为大写", "files": [{"path": str(target), "kind": "file"}]})
            event = self.event(prompt)
            events.append(event)
            execute("deliver", self.binding(prompt)["console_context_file"], request_id=event["request_id"], finish=True)
            return ""
        bot.ai.chat = AsyncMock(side_effect=respond)
        with patch("console_files.receive_qq_file", new=AsyncMock(return_value={"file_id": identities[0],
                "name": "input.txt", "local_path": str(original)})) as receive:
            await self.fixture.receive(bot, False, "process", "/处理 转换为大写")
            await drain_runtime(bot)
            self.assertEqual(receive.await_args.args[3], identities[0])
        self.assertTrue(events[0]["success"])
        self.assertEqual(Path(events[0]["files"][0]["local_path"]).read_text(encoding="utf-8"), "USER PROVIDED INPUT")
        self.assertEqual(original.read_text(encoding="utf-8"), "user provided input")

    async def test_image_command_uses_generation_worker_and_main_cli_approval(self):
        from test_agent_artifacts import task_result
        bot, key = self.bot()
        events, workers = [], []
        async def respond(agent_key, prompt, **options):
            if agent_key.startswith("__work__:task:"):
                workers.append(agent_key)
                self.assertIn("反重力自身可用的图片生成能力", prompt)
                self.assertIn("不要联网搜索现有图片", prompt)
                trace = options["response_trace"]
                result = json.loads(task_result(prompt, response_trace=trace, source=""))
                result["sources"] = []
                del trace[:2]
                return json.dumps(result)
            self.assertEqual(agent_key, key)
            event = self.event(prompt)
            events.append(event)
            bot.send_action.assert_not_awaited()
            execute("deliver", self.binding(prompt)["console_context_file"], request_id=event["request_id"], text="图片已生成", finish=True)
            return ""
        bot.ai.chat = AsyncMock(side_effect=respond)
        await self.fixture.receive(bot, False, "generation", "/图片：橙色横幅")
        await drain_runtime(bot)
        self.assertEqual(len(workers), 1)
        self.assertTrue(events[0]["success"])
        self.assertEqual(events[0]["files"][0]["kind"], "image")
        self.assertEqual(bot._get_main_coordinator().broker.record(events[0]["request_id"])["state"], "delivered")

    async def test_code_work_does_not_block_chat_and_prose_is_not_a_completed_code_result(self):
        bot, _ = self.bot()
        entered, release = asyncio.Event(), asyncio.Event()
        events, attempts = [], []
        async def respond(agent_key, prompt, **options):
            if agent_key.startswith("__work__:task:"):
                attempts.append(prompt)
                entered.set()
                await release.wait()
                if len(attempts) == 1:
                    options["response_trace"].extend([{"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "search_web"}]},
                        {"type": "TOOL_RESULT", "status": "DONE", "content": "programming reference"}])
                    return '{"answer":"已经写好了","files":[]}'
                self.assertIn("编程任务必须包含", prompt)
                directory = Path(re.search(r"本任务输出目录：([^\n]+)", prompt)[1])
                code = directory / "app.py"
                code.write_text("print(2)\n", encoding="utf-8")
                options["response_trace"].extend([{"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command"}]},
                    {"type": "TOOL_RESULT", "status": "DONE", "content": "2"}])
                return json.dumps({"answer": "运行输出2", "files": [{"kind": "file", "path": str(code)}]})
            event = self.event(prompt)
            binding = self.binding(prompt)
            if event:
                events.append(event)
                execute("deliver", binding["console_context_file"], request_id=event["request_id"], finish=True)
            else:
                execute("speech", binding["console_context_file"], operation="ready")
                execute("reply", binding["console_context_file"], text="还在，可以继续聊天")
            return ""
        bot.ai.chat = AsyncMock(side_effect=respond)
        await self.fixture.receive(bot, False, "code-work", "/编程 写一个输出2的脚本")
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(events, [])
        await self.fixture.receive(bot, False, "chat", "还在吗")
        self.assertIn("继续聊天", bot.send_group_message.await_args.args[1].body)
        release.set()
        await drain_runtime(bot)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["success"])

    async def test_official_attachment_is_recorded_and_received_without_onebot_rpc(self):
        from console_files import receive_qq_file
        bot, key = self.bot(True)
        ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
            message_id="official-file", text="/处理 读取报告", is_group=True, reply=AsyncMock())
        message = SimpleNamespace(attachments=[SimpleNamespace(id="attachment", filename="报告.txt", size=7,
            url="https://example.com/report.txt")])
        bot._record_official_attachments(message, ctx)
        console = bot._get_main_coordinator()
        _, token, _ = console.issue_chat_context(ctx, key)
        incoming = console.broker.inbox(token)
        self.assertEqual(len(incoming), 1)
        self.assertEqual(incoming[0]["name"], "报告.txt")
        async def download(url, target, **options):
            self.assertEqual(url, "https://example.com/report.txt")
            Path(target).write_bytes(b"fixture")
        with patch("console_files.download_url", new=AsyncMock(side_effect=download)) as fetch:
            result = await receive_qq_file(bot, console.broker, token, incoming[0]["file_id"], ctx)
            fetch.assert_awaited_once()
        self.assertEqual(Path(result["local_path"]).read_bytes(), b"fixture")
        self.assertEqual(console.broker.inbox(token)[0]["state"], "received")

    async def test_english_display_name_uses_native_nickname_target(self):
        bot, key = self.bot(master=True)
        events = []
        self.approving_agent(bot, events)
        ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
            message_id="english-nickname", text="/nickname @Alice 小明", is_group=True)
        ctx.mentioned_user_ids = ["789"]
        with patch.object(bot.memory_mgr, "set_participant_nickname", wraps=bot.memory_mgr.set_participant_nickname) as save:
            await bot._get_main_coordinator().submit_chat_command(ctx, ctx.text)
            await drain_runtime(bot)
            save.assert_any_call("12345678", "789", "小明")
        self.assertTrue(events[0]["success"])

    async def test_group_master_persona_switch_is_limited_to_their_group(self):
        from config import GroupCustomConfig
        bot, _ = self.bot()
        target = next(name for name in bot.personas.list_personas() if name != bot.personas.active_persona_name)
        bot.app_config.bot.group_settings["12345678"] = GroupCustomConfig(group_id="12345678", master_id="456")
        old_global = bot.personas.active_persona_name
        events = []
        self.approving_agent(bot, events)
        await self.fixture.receive(bot, False, "persona", "/性格 " + target)
        await drain_runtime(bot)
        self.assertTrue(events[0]["success"])
        self.assertEqual(bot.app_config.bot.get_persona_name("12345678"), target)
        self.assertEqual(bot.personas.active_persona_name, old_global)
        self.assertEqual(AppConfig.load(bot.config_path).bot.get_persona_name("12345678"), target)

    async def test_bare_process_command_with_picture_enables_input_preparation_on_both_transports(self):
        for official in (False, True):
            bot, _ = self.bot(official)
            bot.app_config.bot.autonomous_enabled = False
            image = {"local_path": "data/cache/images/input.png", "vision_verified": True,
                "visual_description": "a sample image", "summary": "a sample image"}
            console = bot._get_main_coordinator()
            submit = AsyncMock(return_value={"id": "fixture", "state": "submitted"})
            if official:
                async def prepare(content, **options):
                    self.assertTrue(options["allow_download"])
                    return content, [image]
                message = SimpleNamespace(id="process-image", group_openid="12345678", content="/处理 转为灰度",
                    author=SimpleNamespace(member_openid="456", username="User", bot=False), mentions=[],
                    attachments=[SimpleNamespace(url="https://example.com/input.png", filename="input.png", content_type="image/png")],
                    message_reference=None, reply=AsyncMock())
                with patch("bot_client.process_multimodal_message", new=AsyncMock(side_effect=prepare)), patch.object(console, "submit_chat_command", new=submit):
                    await bot._handle_group_message(message, is_at_event=False)
            else:
                async def prepare(data, **options):
                    self.assertTrue(options["directed"])
                    return options["content"], [image]
                message = {"post_type": "message", "message_type": "group", "self_id": 999, "group_id": 12345678,
                    "user_id": 456, "message_id": "process-image", "raw_message": "/处理 转为灰度",
                    "message": [{"type": "text", "data": {"text": "/处理 转为灰度"}},
                        {"type": "image", "data": {"file": "input.png"}}], "sender": {"nickname": "User"}}
                with patch.object(bot, "_process_qq_images", new=AsyncMock(side_effect=prepare)), patch.object(console, "submit_chat_command", new=submit):
                    await bot._handle_group_message(message)
            submit.assert_awaited_once()
            ctx = submit.await_args.args[0]
            self.assertEqual(ctx.quoted_images[0]["local_path"], image["local_path"])

    async def test_native_video_can_be_received_and_bound_to_the_process_worker(self):
        from console_files import receive_qq_file
        bot, key = self.bot()
        raw = "/处理 提取字幕 [CQ:video,file=native-video,url=https://example.com/clip.mp4,size=7]"
        event = bot._prepare_file_event({"post_type": "message", "message_type": "group", "group_id": 12345678,
            "user_id": 456, "message_id": "video-input", "message": raw, "raw_message": raw})
        ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
            message_id="video-input", text=event["raw_message"], is_group=True)
        console = bot._get_main_coordinator()
        context, token, _ = console.issue_chat_context(ctx, key)
        ids = event["_console_file_ids"]
        request = execute("command", context, text=ctx.text)
        record = console.broker.record(request["id"])
        self.assertEqual(record["kind"], "process")
        self.assertEqual(json.loads(record["result"])["input_file_ids"], ids)
        bot.send_action.return_value = {"status": "ok", "data": {"url": "https://example.com/clip.mp4"}}
        async def download(url, target, **options):
            Path(target).write_bytes(b"fixture")
        with patch("console_files.download_url", new=AsyncMock(side_effect=download)):
            received = await receive_qq_file(bot, console.broker, token, ids[0], ctx)
        bot.send_action.assert_awaited_once_with("get_video", {"file": "native-video"})
        self.assertEqual(Path(received["local_path"]).suffix, ".mp4")
        self.assertEqual(Path(received["local_path"]).read_bytes(), b"fixture")


if __name__ == "__main__":
    unittest.main()
