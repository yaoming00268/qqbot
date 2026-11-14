"""Exercise real ingress routing with offline QQ and model fixtures."""
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
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from antigravity_client import AntigravityClient
from bot_client import GeminiQQBot
from config import AppConfig
from conversation_runtime import EventContext, current_event
from media_router import is_work_request
from onebot_client import OneBotClient
from prompt_builder import compose_autonomous_decision_prompt, compose_user_prompt
from request_routing import prepare_slang_context, route_explicit_work
from slang_glossary import SlangGlossary
from test_ai_fixture import drain_runtime, orchestrated_response, prompt_object


class RequestRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot_request_routing_")
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        self.config = AppConfig()
        self.config.bot.chat_buffer_seconds = 0
        self.config.bot.memory_enabled = False
        self.config.bot.search_enabled = False
        self.ai = AntigravityClient(sessions_file="sessions.json")
        self.ai.chat = ConsoleAgentMock(return_value='{"should_reply":true,"reply":"正常聊天"}')
        self.bots = []

    async def asyncTearDown(self):
        for bot in self.bots:
            await bot._close_runtime()
        os.chdir(self.previous)
        self.temp.cleanup()

    def bot(self, official=False):
        bot = GeminiQQBot(config=self.config, antigravity_client=self.ai) if official else OneBotClient(self.config, self.ai)
        self.bots.append(bot)
        if official:
            bot._connection = MagicMock()
            bot._connection.state.robot = MagicMock(id="999", name="FixtureBot")
            self.config.media.official_file_enabled = True
            bot._get_media_service().sender.public_asset = AsyncMock(return_value="https://example.com/result")
            bot.api.post_group_file = AsyncMock(return_value={"file_info": "opaque-file"})
            bot.api.post_group_message = AsyncMock(return_value={"id": "accepted"})
        else:
            bot.self_id = "999"
            bot._is_ws_connected = lambda: True
            bot.send_action = AsyncMock(return_value={"status": "ok", "data": {"file_id": "accepted"}})
            bot.send_group_message = AsyncMock()
            bot.send_private_message = AsyncMock()
        return bot

    def group_event(self, message_id="lyrics", text="给我一下终结的炽天使op的歌词"):
        return {"post_type": "message", "message_type": "group", "message_id": message_id,
            "group_id": 123, "user_id": 456, "sender": {"nickname": "Fixture user"},
            "raw_message": "[CQ:at,qq=999] " + text}

    def task_agent(self, entered=None, release=None):
        async def run(key, prompt, **options):
            if not key.startswith("__work__:task:"):
                if '"type": "task_result"' in prompt:
                    return '{"should_reply":true,"reply":"任务资料整理好了"}'
                from console_tools.broker import ConsoleBroker
                context_file = prompt_object(prompt, "console_context_file")["console_context_file"]
                broker, token = ConsoleBroker.from_context(context_file)
                if "歌词" in broker.context(token).get("source_text", ""):
                    return '{"should_reply":false,"tasks":[{"type":"search","task":"核实歌词资料"}]}'
                return '{"should_reply":true,"reply":"正常聊天"}'
            self.assertEqual(options["timeout"], 0)
            self.assertIn("歌词", prompt)
            if entered:
                entered.set()
            if release:
                await release.wait()
            options["response_trace"].extend([
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "search_web"}]},
                {"type": "GENERIC", "status": "DONE", "content": "https://example.com/song"}])
            return json.dumps({"answer": "核实后的查询结果\n" * 1000, "files": [],
                "sources": [{"title": "来源", "url": "https://example.com/song"}]})
        return run

    async def test_main_dispatches_lyrics_to_worker_and_chat_stays_live(self):
        bot = self.bot()
        entered, release = asyncio.Event(), asyncio.Event()
        self.ai.chat.side_effect = self.task_agent(entered, release)
        bot._queue_event(self.group_event())
        runtime = bot._get_runtime()
        await asyncio.wait_for(entered.wait(), 2)
        bot._queue_event(self.group_event("chat", "晚上好"))
        await asyncio.wait_for(runtime.chat_workers["onebot:account_1:group:123"], 2)
        self.assertIn("正常聊天", str(bot.send_group_message.await_args.args[1]))
        self.assertFalse(runtime.work_worker.done())
        release.set()
        await drain_runtime(bot)
        self.assertEqual(bot.send_action.await_args.args[0], "upload_group_file")
        result_file = Path(bot.send_action.await_args.args[1]["file"])
        self.assertIn("核实后的查询结果", result_file.read_text(encoding="utf-8"))
        self.assertEqual(bot.send_group_message.await_args.args[1].quote, "")
        self.assertLess(len(str(bot.send_group_message.await_args.args[1])), 100)
        self.assertEqual(self.ai.chat.await_args_list[0].args[0], bot._chat_session_key("onebot_group_123"))
        self.assertTrue(any(c.args[0].startswith("__work__:task:") for c in self.ai.chat.await_args_list))

    async def test_official_group_lyrics_uses_main_branch_console_delivery(self):
        bot = self.bot(official=True)
        self.ai.chat.side_effect = self.task_agent()
        message = SimpleNamespace(id="lyrics", group_openid="123", content="给我终结的炽天使op的歌词",
            author=SimpleNamespace(member_openid="456", username="Fixture user"), attachments=None,
            message_reference=None, reply=AsyncMock(return_value={"id": "sent"}))
        bot._queue_official_group(message, True)
        await drain_runtime(bot)
        self.assertEqual(self.ai.chat.await_count, 3)
        self.assertTrue(self.ai.chat.await_args_list[1].args[0].startswith("__work__:task:"))
        self.assertNotIn("核实后的查询结果", str(message.reply.await_args_list))
        self.assertEqual(bot._runtime.store.jobs("official::group:123:456")[0]["state"], "completed")

    async def test_unknown_quoted_image_reference_is_queued_before_fetching(self):
        bot = self.bot()
        bot.send_action.return_value = {"status": "ok", "data": {"group_id": 123, "user_id": 789,
            "message": [{"type": "image", "data": {"url": "https://example.com/quoted.png"}}]}}
        lanes = []
        async def dispatch(data):
            lanes.append(current_event.get().lane)
        bot._dispatch_event = dispatch
        bot._queue_event(self.group_event(text="[CQ:reply,id=uncached]怎么看"))
        await asyncio.gather(*list(bot._event_tasks))
        await bot._get_runtime().work_worker
        self.assertEqual(lanes, ["work"])

    async def test_pending_slang_schedules_once_without_blocking_then_injects_cached_meaning(self):
        bot = self.bot()
        self.config.bot.search_enabled = True
        glossary = SlangGlossary(Path("slang.md"))
        glossary.record("测试群黑话")
        entered, release = asyncio.Event(), asyncio.Event()
        async def resolve(terms, ai, key, *, check_current):
            entered.set()
            await release.wait()
            check_current()
            return [glossary.record("测试群黑话", meaning="仅供测试的本群解释", tags=["本群"])]
        glossary.resolve_missing = AsyncMock(side_effect=resolve)
        ctx = bot._make_media_context(session_key="onebot_group_123", user_id="456", conversation_id="123",
            message_id="slang", is_group=True, text="测试群黑话")
        with patch("slang_glossary.default_slang_glossary", glossary):
            self.assertIsNone(await prepare_slang_context(bot, ctx, ctx.text))
            await asyncio.wait_for(entered.wait(), 1)
            await prepare_slang_context(bot, ctx, ctx.text)
            self.assertEqual(len(bot._runtime.store.jobs(ctx.owner)), 1)
            for composer in (compose_user_prompt, compose_autonomous_decision_prompt):
                prompt = composer(user_text=ctx.text, author_name="tester")
                self.assertIn("测试群黑话", prompt)
            release.set()
            await asyncio.wait_for(bot._runtime.work_worker, 2)
            await prepare_slang_context(bot, ctx, ctx.text)
            self.assertIn("仅供测试的本群解释", compose_user_prompt(user_text=ctx.text, author_name="tester"))
            self.assertEqual(glossary.resolve_missing.await_count, 1)
        bot.send_action.assert_not_awaited()

    async def test_cancelled_slang_job_can_be_scheduled_by_a_later_message(self):
        bot = self.bot()
        self.config.bot.search_enabled = True
        glossary = SlangGlossary(Path("slang.md"))
        glossary.record("待补词")
        glossary.resolve_missing = AsyncMock(return_value=[])
        ctx = bot._make_media_context(session_key="onebot_group_123", user_id="456", conversation_id="123",
            message_id="slang", is_group=True, text="待补词")
        with patch("slang_glossary.default_slang_glossary", glossary):
            await prepare_slang_context(bot, ctx, ctx.text)
            bot._runtime.cancel(scope=ctx.scope)
            await prepare_slang_context(bot, ctx, ctx.text)
            await bot._runtime.work_worker
        self.assertEqual(glossary.resolve_missing.await_count, 1)

    async def test_asking_recorded_slang_meaning_uses_local_definition_as_a_file(self):
        bot = self.bot()
        glossary = SlangGlossary(Path("slang.md"))
        glossary.record("测试黑话", meaning="这是一条已确认的本地解释", aliases=["测试别名"], tags=["本群"])
        self.ai.chat.side_effect = orchestrated_response('{"should_reply":true,"action":"skill","skill":"group_slang","params":{"query":"测试别名"}}')
        with patch("slang_glossary.default_slang_glossary", glossary), patch("skills.slang_skills.default_slang_glossary", glossary):
            bot._queue_event(self.group_event("meaning", "查一下测试别名是什么意思"))
            await drain_runtime(bot)
        self.assertEqual(self.ai.chat.await_count, 2)
        self.assertFalse(any(c.args[0].startswith("__work__:task:") for c in self.ai.chat.await_args_list))
        self.assertEqual(bot.send_action.await_args.args[0], "upload_group_file")
        delivered = Path(bot.send_action.await_args.args[1]["file"]).read_text(encoding="utf-8")
        self.assertIn("这是一条已确认的本地解释", delivered)
        self.assertEqual(bot.send_group_message.await_args.args[1].quote, "")

    async def test_quoted_cancel_does_not_wait_behind_long_work_or_metadata_fetch(self):
        bot = self.bot()
        entered, release, dispatched = asyncio.Event(), asyncio.Event(), asyncio.Event()
        runtime = bot._get_runtime()
        async def work():
            entered.set()
            await release.wait()
        runtime.submit_work(EventContext("long-work", owner="another-owner"), "long search", work)
        await entered.wait()
        async def dispatch(data):
            self.assertEqual(current_event.get().lane, "chat")
            dispatched.set()
        bot._dispatch_event = dispatch
        bot._queue_event(self.group_event("cancel", "[CQ:reply,id=unknown]取消任务"))
        await asyncio.wait_for(dispatched.wait(), 1)
        bot.send_action.assert_not_awaited()
        release.set()
        await runtime.work_worker

    async def test_official_queued_reply_keeps_original_receipt_time(self):
        bot = self.bot(official=True)
        original_time = time.time() - 400
        captured = []
        original_make = bot._make_media_context
        def make(**kwargs):
            context = original_make(**kwargs)
            captured.append(context.received_at)
            return context
        bot._make_media_context = make
        message = SimpleNamespace(id="old", group_openid="123", content="晚上好",
            author=SimpleNamespace(member_openid="456", username="Fixture user"), attachments=[],
            message_reference=None, reply=AsyncMock())
        token = current_event.set(EventContext("official::group:123", "old", original_time, "work"))
        try:
            await bot._handle_group_message(message, True)
        finally:
            current_event.reset(token)
        self.assertEqual(captured, [original_time])

    def test_work_verbs_and_casual_mentions_are_distinguished(self):
        for text in ("给我一下终结的炽天使op的歌词", "请给我一份调研报告", "帮我找一下歌词", "搜索最新资料"):
            self.assertTrue(is_work_request(text), text)
        for text in ("这首歌的歌词真好听", "晚上好", "报告写完了", "我刚才搜索过了"):
            self.assertFalse(is_work_request(text), text)
