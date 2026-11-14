"""Untimed command execution, isolated workspaces and recoverable handoffs."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_work import run_agent_work
from antigravity_client import AntigravityClient
from config import AppConfig
from conversation_runtime import current_event
from media_downloader import MediaError
from media_router import is_work_request, parse_media_intent
from onebot_client import OneBotClient
from skills.base import SkillContext, SkillResult
from skills.task_skills import RunTaskSkill
from test_agent_artifacts import task_result
from test_ai_fixture import ConsoleAgentMock


class UnlimitedCommandWorkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_unlimited_work_")
        self.previous = Path.cwd()
        os.chdir(self.tmp.name)
        self.client = AntigravityClient(timeout_seconds=0.01, session_timeout_minutes=1,
                                        sessions_file="sessions.json")
        self.config = AppConfig()
        self.config.bot.search_enabled = False
        self.config.bot.enable_action_descriptions = True
        self.config.bot.chat_settle_seconds = 0  # This suite verifies work/chat independence.
        self.config.bot.chat_buffer_seconds = 0
        self.bots = []

    async def asyncTearDown(self):
        try:
            for bot in self.bots:
                await bot._close_runtime()
        finally:
            os.chdir(self.previous)
            self.tmp.cleanup()

    def bot(self):
        bot = OneBotClient(self.config, self.client)
        bot.self_id = "999"
        bot._is_ws_connected = lambda: True
        bot.send_action = AsyncMock(return_value={"status": "ok", "data": {"message_id": "receipt"}})
        bot.send_group_message = AsyncMock()
        self.bots.append(bot)
        return bot

    def context(self, bot):
        ctx = bot._make_media_context(session_key="__work__:onebot_group_123", user_id="456",
            conversation_id="123", message_id="request", text="帮我搜索とける風花とシロうさぎ的cg", is_group=True)
        ctx.send_text = AsyncMock()
        return SkillContext(ctx.ai_session_key, "123", "456", media_context=ctx, app_config=self.config,
                            ai_client=self.client, media_service=bot._get_media_service())

    async def test_plain_completion_uses_saved_handoff_without_another_model_turn(self):
        bot = self.bot()
        context = self.context(bot)
        async def agent(key, prompt, **kwargs):
            result = task_result(prompt, response_trace=kwargs["response_trace"])
            path = Path(re.search(r"交付 JSON 写入：([^\n]+)", prompt)[1])
            path.write_text(result, encoding="utf-8")
            return "文件已完成，交付清单已经保存。"
        self.client.chat = ConsoleAgentMock(side_effect=agent)
        result = await bot._execute_skill("send_image", {"query": "とける風花とシロうさぎ CG"}, context)
        self.assertTrue(result.success)
        self.assertTrue(result.reply_handled)
        self.client.chat.assert_awaited_once()
        bot.send_action.assert_awaited_once()
        self.assertEqual(context.media_context.send_text.await_count, 1)

    async def test_powershell_utf16_handoff_is_accepted(self):
        directory = Path("powershell-work")
        async def ask(prompt, trace, timeout):
            (directory / "agent_result.json").write_text('{"answer":"complete result"}', encoding="utf-16")
            return "交付文件已经保存。"
        _, answer = await run_agent_work(ask, "write result", directory, [], lambda data, trace: data["answer"])
        self.assertEqual(answer, "complete result")

    async def test_malformed_answer_is_repaired_and_original_download_is_kept(self):
        bot = self.bot()
        context = self.context(bot)
        manifest = None
        calls = []
        async def agent(key, prompt, **kwargs):
            nonlocal manifest
            calls.append(key)
            self.assertEqual(kwargs["timeout"], 0)
            if manifest is None:
                manifest = task_result(prompt, response_trace=kwargs["response_trace"])
                return "成果已经下载，但还在整理清单。"
            for item in json.loads(manifest)["files"]:
                self.assertTrue(Path(item["path"]).is_file())
            self.assertIn("保留已下载文件", prompt)
            return manifest
        self.client.chat = ConsoleAgentMock(side_effect=agent)
        result = await RunTaskSkill().execute({"task": context.media_context.text, "output_kind": "image"}, context)
        self.assertTrue(result.success)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertNotEqual(calls[0], context.session_key)
        self.assertTrue(calls[0].startswith("__work__:task:"))
        bot.send_action.assert_awaited_once()

    async def test_more_than_two_work_turns_can_complete_without_a_time_budget(self):
        directory = Path("long-work")
        steps = 0
        async def ask(prompt, trace, timeout):
            nonlocal steps
            self.assertEqual(timeout, 0)
            steps += 1
            await asyncio.sleep(0.015)
            if steps < 6:
                (directory / f"page-{steps}.txt").write_text("completed page", encoding="utf-8")
                return json.dumps({"status": "in_progress", "answer": f"page {steps}"})
            return '{"status":"completed","answer":"full result"}'
        data, answer = await run_agent_work(ask, "search broadly", directory, [], lambda data, trace: data["answer"])
        self.assertEqual(steps, 6)
        self.assertEqual(answer, "full result")
        self.assertEqual(len(list(directory.glob("page-*.txt"))), 5)

    async def test_invalid_image_is_corrected_before_any_send(self):
        bot = self.bot()
        context = self.context(bot)
        count = 0
        async def agent(key, prompt, **kwargs):
            nonlocal count
            count += 1
            bot.send_action.assert_not_awaited()
            return task_result(prompt, response_trace=kwargs["response_trace"], matched=count > 1)
        self.client.chat = ConsoleAgentMock(side_effect=agent)
        result = await RunTaskSkill().execute({"task": "搜索并核对 CG", "output_kind": "image"}, context)
        self.assertTrue(result.success)
        self.assertEqual(count, 2)
        bot.send_action.assert_awaited_once()

    async def test_web_search_selected_for_cg_still_delivers_actual_resources(self):
        bot = self.bot()
        context = self.context(bot)
        self.client.chat = ConsoleAgentMock(side_effect=lambda key, prompt, **kwargs:
            task_result(prompt, response_trace=kwargs["response_trace"]))
        self.client.native_retrieve = AsyncMock(side_effect=AssertionError("limited text-only path used"))
        result = await bot._execute_skill("web_search", {"query": "とける風花とシロうさぎ CG"}, context)
        self.assertTrue(result.success)
        self.assertTrue(result.reply_handled)
        bot.send_action.assert_awaited_once()
        self.assertEqual(bot.send_action.await_args.args[1]["message"][0]["type"], "image")
        self.assertEqual(self.client.chat.await_args.kwargs["timeout"], 0)
        self.client.native_retrieve.assert_not_awaited()

    async def test_explicit_failure_stops_truthfully_and_retains_work(self):
        async def ask(prompt, trace, timeout):
            return '{"status":"failed","reason":"实际来源拒绝访问，未取得资源"}'
        directory = Path("failed-work")
        with self.assertRaisesRegex(MediaError, "拒绝访问"):
            await run_agent_work(ask, "search", directory, [], lambda data, trace: data)
        self.assertTrue((directory / "agent_response.txt").is_file())

    async def test_identical_stalled_reply_does_not_loop_or_claim_completion(self):
        ask = AsyncMock(return_value="需要继续，但尚未执行任何工作")
        with self.assertRaisesRegex(MediaError, "重复返回"):
            await run_agent_work(ask, "search", "stalled-work", [], lambda data, trace: data)
        self.assertEqual(ask.await_count, 2)

    async def test_cancel_keeps_workspace_and_does_not_submit_a_result(self):
        entered = asyncio.Event()
        directory = Path("cancelled-work")
        async def ask(prompt, trace, timeout):
            (directory / "partial.txt").write_text("partial work", encoding="utf-8")
            entered.set()
            await asyncio.Event().wait()
        running = asyncio.create_task(run_agent_work(ask, "search", directory, [], lambda data, trace: data))
        await entered.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertTrue((directory / "partial.txt").is_file())
        self.assertTrue((directory / "agent_trace.jsonl").is_file())

    async def test_command_chat_uses_no_default_or_agentapi_timeout(self):
        self.client._is_ready = True
        self.client._run_agentapi_cmd = AsyncMock(return_value={"response": {"newConversation": {"conversationId": "fixture"}}})
        async def delayed(*args, **kwargs):
            await asyncio.sleep(0.03)
            return "complete"
        self.client._wait_for_model_response = AsyncMock(side_effect=delayed)
        result = await self.client.chat("__work__:task:fixture", "perform command", raise_on_error=True)
        self.assertEqual(result, "complete")
        self.assertEqual(self.client._run_agentapi_cmd.await_args.kwargs["timeout"], 0)
        self.assertEqual(self.client._wait_for_model_response.await_args.kwargs["timeout"], float("inf"))

    async def test_agentapi_zero_timeout_means_wait_without_cancelling_process(self):
        self.client._executable = "fixture-agentapi.exe"
        proc = MagicMock(returncode=0)
        async def communicate():
            await asyncio.sleep(0.03)
            return b'{"ok":true}', b''
        proc.communicate = AsyncMock(side_effect=communicate)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            self.assertEqual(await self.client._run_agentapi_cmd(["new-conversation"], timeout=0), {"ok": True})
        proc.kill.assert_not_called()

    async def test_native_retrieval_default_wait_and_workspace_allow_multiple_repairs(self):
        turns = 0
        async def chat(key, prompt, **kwargs):
            nonlocal turns
            self.assertEqual(kwargs["timeout"], 0)
            self.assertNotIn("native_only", kwargs)
            self.assertIn("独立工作目录", prompt)
            self.assertNotIn("禁止扫描工作区", prompt)
            await asyncio.sleep(0.015)
            turns += 1
            if turns < 4:
                return json.dumps({"status": "in_progress", "answer": f"collecting {turns}"})
            kwargs["response_trace"].extend([
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command"}]},
                {"type": "GENERIC", "status": "DONE", "content": "fetched https://example.com/source"}])
            return '{"answer":"verified result","sources":[{"url":"https://example.com/source"}]}'
        self.client.chat = ConsoleAgentMock(side_effect=chat)
        result = await self.client.native_retrieve("group_123", "search and read all useful pages")
        self.assertEqual(turns, 4)
        self.assertIn("verified result", result.text)
        self.assertTrue(next(Path("agent_work").glob("*/agent_trace.jsonl")).is_file())

    async def test_tool_failure_keeps_waiting_while_agent_recovers(self):
        self.client._brain_dir = str(Path("brain").resolve())
        path = Path(self.client._brain_dir) / "fixture/.system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True)
        rows = [
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
             "tool_calls": [{"name": "manage_task"}]},
            {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "ERROR",
             "error": "cannot kill task: task is not running (status: DONE)"}]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        async def finish():
            await asyncio.sleep(0.03)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE",
                    "status": "DONE", "content": "completed after tool recovery"}) + "\n")
        completing = asyncio.create_task(finish())
        result = await self.client._wait_for_model_response("fixture", expected_step_min=1, timeout=1)
        await completing
        self.assertEqual(result, "completed after tool recovery")

    async def test_failed_model_turn_still_reports_failure(self):
        self.client._brain_dir = str(Path("brain").resolve())
        path = Path(self.client._brain_dir) / "fixture/.system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
            "status": "ERROR", "error": "model unavailable"}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            await self.client._wait_for_model_response("fixture", timeout=1)

    async def test_failed_work_conversation_is_retained_for_continuation(self):
        self.client._is_ready = True
        session = self.client.get_or_create_session("__work__:task:failed")
        session.conversation_id = "existing"
        self.client._run_agentapi_cmd = AsyncMock(side_effect=RuntimeError("temporary transport failure"))
        with self.assertRaises(RuntimeError):
            await self.client.chat(session.session_key, "continue", raise_on_error=True)
        self.assertEqual(session.conversation_id, "existing")
        session.last_active = 0
        self.assertEqual(self.client.get_or_create_session(session.session_key).conversation_id, "existing")

    async def test_failure_archive_does_not_delete_agent_conversation(self):
        bot = self.bot()
        self.client.archive_session = AsyncMock()
        from conversation_runtime import EventContext
        event = EventContext("onebot:account_1:group:123", "request", time.time(), "work", "u")
        await bot._archive_work("failed", event, SkillResult(False, "unfinished",
            data={"agent_session_key": "__work__:task:failed"}))
        self.client.archive_session.assert_not_awaited()
        self.assertTrue(Path("data/cache/work_archives/failed/result.json").is_file())

    async def test_natural_request_enters_main_then_worker_and_chat_continues(self):
        bot = self.bot()
        entered, release, chatted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []
        async def dispatch(data):
            order.append((data["message_id"], current_event.get().lane))
            if data["message_id"] == "search":
                async def work():
                    entered.set()
                    await release.wait()
                bot._get_runtime().submit_work(current_event.get(), "search", work)
            else:
                chatted.set()
        bot._dispatch_event = dispatch
        def event(mid, text):
            return {"post_type": "message", "group_id": 123, "user_id": 456, "message_id": mid,
                    "raw_message": "[CQ:at,qq=999] " + text, "sender": {"nickname": "tester"}}
        bot._queue_event(event("search", "帮我搜索とける風花とシロうさぎ的cg"))
        await asyncio.wait_for(entered.wait(), 1)
        bot._queue_event(event("chat", "你好"))
        await asyncio.wait_for(chatted.wait(), 1)
        self.assertEqual(order, [("search", "chat"), ("chat", "chat")])
        release.set()
        await bot._runtime.work_worker


class WorkRoutingTests(unittest.TestCase):
    def test_explicit_cg_request_keeps_full_title_and_has_no_implied_dimensions(self):
        text = "帮我搜索とける風花とシロうさぎ的cg"
        intent = parse_media_intent(text)
        self.assertEqual(intent.kind, "image")
        self.assertEqual(intent.request_text, text)
        self.assertFalse(intent.count_requested)
        self.assertEqual(intent.orientation, "")

    def test_action_requests_are_work_and_chatter_is_not(self):
        for text in ("帮我搜索某部作品", "请检索公开资料", "下载这个资源", "please search this topic",
                     "帮我网络搜索某部作品", "请在网上搜一下公开资料", "网上检索新闻"):
            self.assertTrue(is_work_request(text), text)
        for text in ("你好", "我昨天搜索过了", "怎么搜索图片", "别搜索了", "不要下载"):
            self.assertFalse(is_work_request(text), text)


if __name__ == "__main__":
    unittest.main()
