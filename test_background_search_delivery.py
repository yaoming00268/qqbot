"""Background retrieval and file-only command delivery regression coverage."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
import os
from test_ai_fixture import ConsoleAgentMock

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from antigravity_client import AntigravityClient
from config import AppConfig
from conversation_runtime import EventContext, current_event
from messaging import SendUnknown
from onebot_client import OneBotClient
from skills.base import SkillContext, SkillResult
from skills.task_skills import RunTaskSkill


class BackgroundSearchDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_search_files_")
        self.previous = Path.cwd()
        os.chdir(self.tmp.name)
        self.config = AppConfig()
        self.config.bot.max_reply_length = 50
        self.config.bot.search_enabled = False
        self.ai = AntigravityClient(sessions_file="sessions.json")
        self.bot = OneBotClient(self.config, self.ai)
        self.bot.self_id = "999"
        self.bot._is_ws_connected = lambda: True
        self.bot.send_action = AsyncMock(return_value={"status": "ok", "data": {"file_id": "accepted"}})
        self.bot.send_group_message = AsyncMock()
        self.bot.send_private_message = AsyncMock()

    async def asyncTearDown(self):
        try:
            await self.bot._close_runtime()
        finally:
            os.chdir(self.previous)
            self.tmp.cleanup()

    def context(self, *, private=False, message_id="request"):
        ctx = self.bot._make_media_context(session_key="onebot_private_123" if private else "onebot_group_123", user_id="456",
            conversation_id="123", message_id=message_id, text="查询最新消息", is_group=not private)
        ctx.send_text = AsyncMock()
        return SkillContext(ctx.ai_session_key, "123", "456", media_context=ctx,
            app_config=self.config, ai_client=self.ai, media_service=self.bot._get_media_service())

    def agent(self, answer):
        async def run(key, prompt, **kwargs):
            self.assertTrue(key.startswith("__work__:task:"))
            self.assertEqual(kwargs["timeout"], 0)
            kwargs["response_trace"].extend([
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "search_web"}]},
                {"type": "GENERIC", "status": "DONE"}])
            return json.dumps({"answer": answer, "sources": [{"title": "来源", "url": "https://example.com/source"}],
                               "files": []})
        return run

    async def test_short_and_large_search_answers_are_complete_files_in_group_and_private(self):
        for private, answer in ((False, "核实答案"), (True, "详细结果\n" * 5000)):
            with self.subTest(private=private):
                context = self.context(private=private, message_id=str(private))
                self.ai.chat = ConsoleAgentMock(side_effect=self.agent(answer))
                result = await RunTaskSkill().execute({"task": "查询资料", "_requires_network": True}, context)
                self.assertTrue(result.success)
                action, payload = self.bot.send_action.await_args.args
                self.assertEqual(action, "upload_private_file" if private else "upload_group_file")
                content = Path(payload["file"]).read_text(encoding="utf-8")
                self.assertTrue(content.startswith(answer))
                self.assertIn("https://example.com/source", content)
                context.media_context.send_text.assert_awaited_once()
                self.assertLessEqual(len(context.media_context.send_text.await_args.args[0]), 50)
                self.assertNotIn(answer, result.message)

    async def test_queued_search_does_not_hold_chat_or_add_reply_reference(self):
        context = self.context()
        ctx = context.media_context
        # Use the production quote wrapper for this test.
        ctx = self.bot._make_media_context(session_key=ctx.ai_session_key, user_id="456",
            conversation_id="123", message_id="original-message", text=ctx.text, is_group=True)
        context.media_context = ctx
        entered, release, chatted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        answer = "完整查询结果\n" * 100
        async def agent(key, prompt, **kwargs):
            entered.set()
            await release.wait()
            return await self.agent(answer)(key, prompt, **kwargs)
        self.ai.chat = ConsoleAgentMock(side_effect=agent)
        runtime = self.bot._get_runtime()
        token = current_event.set(EventContext(ctx.scope, ctx.message_id, time.time()))
        try:
            queued = await self.bot._execute_skill("web_search", {"query": "最新资料"}, context)
        finally:
            current_event.reset(token)
        self.assertEqual(queued.action_type, "queued")
        await asyncio.wait_for(entered.wait(), 1)
        async def chat():
            chatted.set()
        runtime.submit_chat(EventContext(ctx.scope, "new-chat", time.time()), chat)
        await asyncio.wait_for(chatted.wait(), 1)
        self.bot.send_action.assert_not_awaited()
        release.set()
        await asyncio.wait_for(runtime.work_worker, 3)
        self.assertEqual(runtime.store.jobs(ctx.owner)[0]["state"], "completed")
        self.assertEqual(self.bot.send_action.await_args.args[0], "upload_group_file")
        messages = [call.args[1] for call in self.bot.send_group_message.await_args_list]
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(message.quote == "" for message in messages))
        self.assertNotIn(answer, "".join(message.body for message in messages))

    async def test_unknown_file_send_is_not_retried_or_claimed_complete(self):
        context = self.context()
        self.bot.send_action.side_effect = SendUnknown("submission uncertain")
        result = await self.bot._deliver_work_text(context.media_context, "complete answer")
        self.assertFalse(result.success)
        self.assertEqual(result.data["state"], "unknown")
        context.media_context.send_text.assert_not_awaited()
        self.bot.send_action.assert_awaited_once()
        self.assertTrue(next(Path(self.config.media.cache_dir).rglob("answer.md")).is_file())

    async def test_disabled_files_do_not_fall_back_to_long_chat_text(self):
        context = self.context()
        context.media_service.config.file_enabled = False
        result = await self.bot._deliver_work_text(context.media_context, "large answer" * 1000)
        self.assertFalse(result.success)
        self.bot.send_action.assert_not_awaited()
        context.media_context.send_text.assert_not_awaited()

    async def test_other_content_skills_also_deliver_text_as_files(self):
        context = self.context()
        content = "已确认黑话：完整释义\n" * 200
        with patch("skills.manager.default_skill_manager.execute_skill", new=AsyncMock(
                return_value=SkillResult(True, content, data={"entries": ["confirmed"]}))):
            result = await self.bot._execute_skill("group_slang", {"query": "黑话"}, context)
        self.assertTrue(result.success)
        self.assertTrue(result.reply_handled)
        self.assertEqual(self.bot.send_action.await_args.args[0], "upload_group_file")
        path = Path(self.bot.send_action.await_args.args[1]["file"])
        self.assertEqual(path.read_text(encoding="utf-8"), content)
        context.media_context.send_text.assert_awaited_once()

    async def test_completed_work_does_not_archive_concurrent_chat_or_capture_sessions(self):
        ctx = self.context().media_context
        owned = ("__work__:task:unique-task",)
        unrelated = ("__work__:onebot_group_123", "onebot_group_123", "__native__:onebot_group_123", "__vision__:onebot_group_123",
                     "__qq_capture__:" + ctx.scope)
        for key in (*owned, *unrelated):
            self.ai.get_or_create_session(key)
        self.ai.archive_session = AsyncMock(return_value=True)
        await self.bot._archive_work("completed", EventContext(ctx.scope, ctx.message_id, time.time(), "work"),
            SkillResult(True, "delivered", data={"agent_session_key": owned[0]}))
        archived = {call.args[0] for call in self.ai.archive_session.await_args_list}
        self.assertEqual(archived, set(owned))
        for call in self.ai.archive_session.await_args_list:
            self.assertTrue(Path(call.args[1]).is_file())


if __name__ == "__main__":
    unittest.main()
