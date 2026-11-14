"""Output commits must not cancel native tools or prematurely revoke work."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from antigravity_client import AntigravityClient
from companion_life import state_for
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.files import ConsoleFileError
from test_agent_reply_latency import inbound, final
from test_ai_fixture import prompt_object


class WorkAfterReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-natural-work-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.payload = {"namespace": "account", "scope": "group:1", "session_key": "persona:1",
            "main_agent": True, "sender_id": "456", "is_master": True, "generation": 0,
            "work_permissions": {"access": "all"}, "self_activity_config": {"enabled": True},
            "artifact_roots": [str(self.root)], "output_dir": str(self.root),
            "sandbox_config": {"enabled": True, "mode": "background", "directory": str(self.root / "sandbox")}}

    def context(self, **changes):
        path = self.broker.issue_context({**self.payload, **changes}, self.root / "contexts")
        return path, ConsoleBroker.from_context(path)[1]

    def test_reply_commit_and_delivery_finish_all_leave_later_work_open(self):
        for mode in ("reply", "commit", "deliver"):
            with self.subTest(mode=mode):
                context, token = self.context()
                if mode == "reply":
                    execute("reply", context, text="我去看看")
                elif mode == "commit":
                    execute("message", context, text="我去看看")
                    execute("commit", context)
                else:
                    ready = execute("submit", context, task="已有工作")
                    self.broker.update(ready["id"], "ready", {"success": True, "result": {"files": []}})
                    execute("deliver", context, request_id=ready["id"], text="结果已整理", finish=True)
                task = execute("submit", context, task="提交后的实际工作")
                agent = execute("agents", context, operation="create", params={"name": "资料员", "text": "实际任务"})
                execute("agents", context, operation="send", params={"agent_id": agent["id"], "text": "补充要求"})
                execute("companion", context, operation="remember", params={"text": "想继续了解配乐"})
                artifact = self.root / "result.txt"
                artifact.write_text("实际成果", encoding="utf-8")
                sent = execute("send", context, files=[str(artifact)])
                self.assertEqual(self.broker.record(sent["id"])["kind"], "send_files")
                self.assertEqual(self.broker.record(task["id"])["state"], "submitted")
                self.assertEqual(self.broker.reply_control(token)["mode"], "committed")
                followup = execute("message", context, text="工作后补充的新一批回复")
                self.assertNotIn(followup["id"], [row["command_id"] for row in self.broker.committed_messages(token)])
                receipt = execute("commit", context)
                self.assertEqual(receipt["message_ids"], [followup["id"]])
                self.broker.seal_messages(token)
                with self.assertRaises(ConsoleFileError):
                    execute("message", context, text="已结束，不能再追加回复")
                with self.assertRaises(ConsoleFileError):
                    execute("submit", context, task="已结束，不能再追加工作")
                with self.assertRaises(ConsoleFileError):
                    execute("agents", context, operation="send", params={"agent_id": agent["id"], "text": "迟到任务"})

    def test_delivery_after_reply_is_atomic_idempotent_and_does_not_commit_a_second_reply(self):
        context, token = self.context()
        reply = execute("reply", context, text="已经整理好了")
        task = execute("submit", context, task="整理资料")
        self.broker.update(task["id"], "ready", {"success": True, "result": {"files": []}})
        receipt = execute("deliver", context, request_id=task["id"], text="资料在这里", finish=True)
        self.assertEqual(receipt["message_ids"], reply["message_ids"])
        self.assertEqual(len(self.broker.staged_messages(token)), 1)
        self.broker.seal_messages(token)
        self.assertEqual(execute("deliver", context, request_id=task["id"], text="资料在这里", finish=True), receipt)
        with self.assertRaises(ConsoleFileError):
            execute("deliver", context, request_id=task["id"], text="不能修改", finish=True)
        self.broker.seal_messages(token, cancel=True)
        with self.assertRaises(ConsoleFileError):
            execute("deliver", context, request_id=task["id"], text="资料在这里", finish=True)

    def test_reply_does_not_grant_disabled_work_permissions_or_worker_roles(self):
        context, _ = self.context(is_master=False, work_permissions={"access": "disabled"})
        execute("reply", context, text="普通回复")
        for action, kwargs in (("submit", {"task": "无权工作"}),
                ("agents", {"operation": "create", "params": {"name": "不能创建", "text": "无权工作"}})):
            with self.subTest(action=action), self.assertRaises(PermissionError):
                execute(action, context, **kwargs)

    def test_browser_and_personal_records_can_follow_a_reply_until_completion(self):
        context, token = self.context()
        executable = self.root / "chrome.exe"
        executable.write_bytes(b"fixture")
        execute("reply", context, text="我想继续看这篇访谈")
        execute("companion", context, operation="remember", params={"kind": "preference", "text": "游戏配乐"})
        wish = execute("companion", context, operation="wish", params={"text": "想学编曲"})
        self.assertTrue(wish)
        with patch("browser_control.installed_browser", return_value=executable):
            result = execute("companion", context, operation="browse",
                params={"url": "https://example.com/interview", "text": "想了解编曲过程"})
        self.assertEqual(self.broker.record(result["id"])["state"], "submitted")
        self.assertEqual(state_for(self.broker, "account", "persona:1")["interests"], ["游戏配乐"])
        self.broker.seal_messages(token)
        with self.assertRaises(PermissionError):
            execute("companion", context, operation="remember", params={"text": "迟到的记录"})

    def test_seal_serializes_with_post_reply_work_submission(self):
        for _ in range(6):
            context, token = self.context()
            execute("reply", context, text="已提交")
            barrier = threading.Barrier(2)
            def submit():
                barrier.wait()
                try:
                    return execute("submit", context, task="并发工作")
                except ConsoleFileError:
                    return None
            def seal():
                barrier.wait()
                self.broker.seal_messages(token)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = pool.submit(submit), pool.submit(seal)
                accepted = first.result()
                second.result()
            with self.broker.connect() as db:
                rows = db.execute("SELECT id FROM requests WHERE token=?", (token,)).fetchall()
            self.assertEqual([row[0] for row in rows], [accepted["id"]] if accepted else [])
            with self.assertRaises(ConsoleFileError):
                execute("submit", context, task="迟到的新工作")


class NativeCompletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-native-completion-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ai = AntigravityClient(sessions_file=str(self.root / "sessions.json"))
        self.ai._is_ready = True
        self.ai._brain_dir = str(self.root / "brain")
        self.ai._cancel_native_conversation = AsyncMock()
        self.ai._ensure_conversation_present = AsyncMock()
        self.key = "onebot_group_natural"
        self.ai.get_or_create_session(self.key).conversation_id = "natural-cid"
        self.path = self.root / "brain/natural-cid/.system_generated/logs/transcript.jsonl"
        self.path.parent.mkdir(parents=True)

    async def asyncTearDown(self):
        # Cancellation can leave a short to_thread transcript read finishing.
        # Drain it before Windows removes the fixture's open log file.
        await asyncio.get_running_loop().shutdown_default_executor()

    async def test_legacy_commit_or_report_callback_cannot_cut_off_tools_or_release_next_input(self):
        for mode in ("committed", "reported"):
            with self.subTest(mode=mode):
                self.path.write_text("", encoding="utf-8")
                submitted, finish = asyncio.Event(), asyncio.Event()
                count = 0
                async def command(args, **options):
                    nonlocal count
                    count += 1
                    step = count * 10
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(inbound(step, args[-1].splitlines()[0])) + "\n")
                    submitted.set()
                    await finish.wait()
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(final(step + 1, "完整结束")) + "\n")
                    return {}
                self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
                first = asyncio.create_task(self.ai.chat(self.key, "第一条输入", bind_response=True, stop_requested=lambda: mode))
                second = None
                try:
                    await asyncio.wait_for(submitted.wait(), 2)
                    second = asyncio.create_task(self.ai.chat(self.key, "下一条输入", bind_response=True))
                    await asyncio.sleep(.15)
                    self.assertFalse(first.done())
                    self.assertEqual(self.ai._run_agentapi_cmd.await_count, 1)
                    self.ai._cancel_native_conversation.assert_not_awaited()
                    finish.set()
                    self.assertEqual(await asyncio.wait_for(first, 2), "完整结束")
                    self.assertEqual(await asyncio.wait_for(second, 2), "完整结束")
                    self.ai._cancel_native_conversation.assert_not_awaited()
                finally:
                    finish.set()
                    tasks = [task for task in (first, second) if task is not None]
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    async def test_explicit_cancellation_still_stops_an_unfinished_committed_invocation(self):
        submitted = asyncio.Event()
        async def command(args, **options):
            self.path.write_text(json.dumps(inbound(1, args[-1].splitlines()[0])) + "\n", encoding="utf-8")
            submitted.set()
            await asyncio.Event().wait()
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        task = asyncio.create_task(self.ai.chat(self.key, "输入", bind_response=True, stop_requested=lambda: "committed"))
        await asyncio.wait_for(submitted.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.ai._cancel_native_conversation.assert_awaited_once_with("natural-cid")


class WorkerReportCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_report_waits_for_native_tool_receipt_and_keeps_search_evidence(self):
        await self.check_worker_report("search", "search_web")

    async def test_code_worker_can_finish_native_command_then_report_without_retrieval_only_rejection(self):
        await self.check_worker_report("code", "run_command")

    async def test_process_worker_can_finish_native_file_read_then_report_without_retrieval_only_rejection(self):
        await self.check_worker_report("process", "view_file")

    async def check_worker_report(self, task_type, work_tool):
        from test_subagent_runtime import SubagentRuntimeTests
        fixture = SubagentRuntimeTests()
        fixture.setUp()
        submitted, finish = asyncio.Event(), asyncio.Event()
        task = None
        try:
            bot, key, coordinator, _ = await fixture.prepare()
            context = fixture.context(coordinator, key)
            result = execute("agents", context, operation="create",
                params={"name": "资料员", "task_type": task_type, "text": "实际完成工作并汇报"})
            runtime = coordinator.agents
            agent = runtime.mailbox.agent(result["id"])
            message = runtime.mailbox.claim(result["id"])
            ai = bot.ai
            ai.chat = AntigravityClient.chat.__get__(ai)
            ai._is_ready = True
            ai._brain_dir = str(Path(fixture.fixture.temp.name) / "brain")
            ai._cancel_native_conversation = AsyncMock()
            ai._ensure_conversation_present = AsyncMock()
            ai.get_or_create_session("__work__:agent:" + result["id"]).conversation_id = "worker-natural"
            path = Path(ai._brain_dir) / "worker-natural/.system_generated/logs/transcript.jsonl"
            path.parent.mkdir(parents=True)
            worker_token = []
            async def command(args, **options):
                worker = prompt_object(args[-1], "console_context_file")["console_context_file"]
                worker_token.append(ConsoleBroker.from_context(worker)[1])
                rows = [inbound(1, args[-1].splitlines()[0]),
                    {**final(2, ""), "tool_calls": [{"name": work_tool, "id": "search-1"}]},
                    {"step_index": 3, "type": "TOOL_RESULT", "status": "DONE", "tool_call_id": "search-1"},
                    {**final(4, ""), "tool_calls": [{"name": "qqbot_cli", "id": "report-1"}]}]
                path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
                execute("agents", worker, operation="report", params={"text": "实际来源 https://example.com/interview", "finish": True})
                submitted.set()
                await finish.wait()
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"step_index": 5, "type": "TOOL_RESULT", "status": "DONE", "tool_call_id": "report-1"}) + "\n")
                    stream.write(json.dumps(final(6, "")) + "\n")
                return {}
            ai._run_agentapi_cmd = AsyncMock(side_effect=command)
            task = asyncio.create_task(runtime._execute(agent, message))
            await asyncio.wait_for(submitted.wait(), 2)
            await asyncio.sleep(.15)
            self.assertFalse(task.done())
            self.assertEqual(coordinator.broker.reply_control(worker_token[0])["mode"], "reported")
            self.assertFalse(runtime.pending(key))
            ai._cancel_native_conversation.assert_not_awaited()
            finish.set()
            await asyncio.wait_for(task, 2)
            report = runtime.pending(key)[0]
            self.assertTrue(json.loads(report["payload"])["success"])
            log = json.loads((runtime.mailbox.workspace(agent) / ("run-" + str(message["seq"]) + ".json")).read_text(encoding="utf-8"))
            self.assertTrue(any(row.get("tool_call_id") == "report-1" for row in log["trace"]))
            self.assertEqual(coordinator.broker.reply_control(worker_token[0])["mode"], "sealed")
            ai._cancel_native_conversation.assert_not_awaited()
        finally:
            finish.set()
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await fixture.asyncTearDown()
