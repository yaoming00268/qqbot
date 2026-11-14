"""Public task delivery boundaries. All agents, network and QQ are offline fixtures."""
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
from unittest.mock import AsyncMock, MagicMock, patch

from antigravity_client import AntigravityClient
from bot_client import GeminiQQBot
from config import AppConfig
from conversation_runtime import EventContext, current_event
from media_models import MediaIntent
from messaging import SendUnknown, split_text
from onebot_client import OneBotClient
from skills.base import SkillContext, SkillResult
from skills.task_skills import RunTaskSkill
from test_agent_artifacts import task_result
from test_ai_fixture import drain_runtime, orchestrated_response


CAPTION = "任务完成，结果已发送 (´▽`)"
PLACEHOLDER = "立绘这就帮哥哥找来发到群里啦"
REPORT = ("已成功联网检索并下载立绘文件到任务文件夹。通过图像工具实际查看核对，"
          "确认包含制服、毕业和新年和服差分，画质细腻，主体明确。" * 8)
SOURCES = [{"title": "VNDB", "url": "https://vndb.org/v7302"},
           {"title": "Safebooru", "url": "https://safebooru.org/index.php?page=post&s=list&tags=tamaki_sakura"}]


class TaskReplyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_reply_delivery_")
        self.previous = os.getcwd()
        os.chdir(self.tmp.name)
        self.config = AppConfig()
        self.config.bot.search_enabled = False
        self.config.bot.max_reply_length = 100
        self.config.bot.enable_action_descriptions = True
        self.ai = AntigravityClient(sessions_file="sessions.json")
        self.ai.chat = ConsoleAgentMock()
        self.bots = []

    async def asyncTearDown(self):
        try:
            for bot in self.bots:
                await bot._close_runtime()
        finally:
            os.chdir(self.previous)
            self.tmp.cleanup()

    def bot(self, official=False):
        cls = GeminiQQBot if official else OneBotClient
        bot = cls(config=self.config, antigravity_client=self.ai) if official else cls(self.config, self.ai)
        if official:
            bot._connection = MagicMock()
            bot._connection.state.robot = MagicMock(id="999", name="bot")
            self.config.media.official_file_enabled = True
            bot._get_media_service().sender.public_asset = AsyncMock(return_value="https://example.com/resource")
        else:
            bot.self_id = "999"
            bot._is_ws_connected = lambda: True
            bot.send_group_message = AsyncMock()
            bot.send_private_message = AsyncMock()
            bot.send_action = AsyncMock(return_value={"status": "ok", "retcode": 0,
                                                       "data": {"message_id": "receipt"}})
        self.bots.append(bot)
        return bot

    def context(self, bot, *, official=False):
        ctx = bot._make_media_context(session_key="onebot_group_123", user_id="456", conversation_id="123",
            message_id="request", text="帮我找角色立绘", is_group=True, reply=AsyncMock())
        if not official:
            ctx.send_text = AsyncMock()
        return SkillContext(ctx.ai_session_key, "123", "456", media_context=ctx,
            app_config=self.config, ai_client=self.ai, media_service=bot._get_media_service())

    @staticmethod
    def media_result(key, prompt, **kwargs):
        result = json.loads(task_result(prompt, response_trace=kwargs["response_trace"], count=3))
        # Old agents may still put their entire work report in answer. It must
        # never become public merely because the new prompt was ignored.
        result.update(answer=REPORT, report="内部核对与下载详情", sources=SOURCES)
        return json.dumps(result)

    def decision(self, skill="send_image", params=None):
        return json.dumps({"should_reply": True, "action": "skill", "skill": skill,
                           "params": params or {"query": "character"}, "reply": PLACEHOLDER})

    async def test_media_report_is_archived_and_only_one_caption_is_sent(self):
        bot = self.bot()
        context = self.context(bot)
        self.ai.chat.side_effect = self.media_result
        result = await bot._execute_skill("send_image", {"query": "character"}, context)
        self.assertTrue(result.success)
        self.assertTrue(result.reply_handled)
        self.assertEqual(result.message, CAPTION)
        self.assertEqual(bot.send_action.await_count, 3)
        self.assertEqual([c.args[0] for c in context.media_context.send_text.await_args_list], [CAPTION])
        self.assertEqual(bot._last_chat_replies[context.media_context.media_owner], CAPTION)
        archive = json.loads(next(Path(bot.media.config.cache_dir).rglob("delivery.json")).read_text(encoding="utf-8"))
        self.assertEqual(archive["answer"], REPORT)
        self.assertEqual(archive["report"], "内部核对与下载详情")
        self.assertEqual(archive["sources"], SOURCES)

    async def test_onebot_group_does_not_append_model_completion_or_remember_it(self):
        bot = self.bot()
        self.ai.chat.side_effect = orchestrated_response(self.decision(), self.media_result)
        await bot._handle_group_message({"group_id": 123, "user_id": 456, "message_id": 111,
            "raw_message": "[CQ:at,qq=999] please help", "sender": {"nickname": "tester"}})
        await drain_runtime(bot)
        texts = [getattr(c.args[1], "body", str(c.args[1])) for c in bot.send_group_message.await_args_list]
        self.assertEqual(texts, ["任务完成"])
        messages = bot.context_buffer.get_recent_messages("123", 10)
        self.assertEqual([m["content"] for m in messages if m["is_bot"]], ["任务完成"])

    async def test_onebot_private_does_not_append_model_completion(self):
        bot = self.bot()
        self.ai.chat.side_effect = orchestrated_response(self.decision(), self.media_result)
        await bot._handle_private_message({"user_id": 456, "message_id": 112,
            "raw_message": "please help", "sender": {"nickname": "tester"}})
        await drain_runtime(bot)
        self.assertEqual([getattr(c.args[1], "body", str(c.args[1]))
                          for c in bot.send_private_message.await_args_list], ["任务完成"])

    async def test_official_group_and_basic_do_not_append_model_completion(self):
        for group in (False, True):
            with self.subTest(group=group):
                bot = self.bot(official=True)
                self.ai.chat.side_effect = None
                self.ai.chat.side_effect = orchestrated_response(self.decision())
                result = SkillResult(True, "已准备结果", action_type="prepared", data={"answer": REPORT, "files": []}, reply_handled=True)
                worker = AsyncMock(return_value=result)
                replies = AsyncMock()
                if group:
                    msg = MagicMock(id="official-1", group_openid="123", content="please help", attachments=[],
                        mentions=[], message_reference=None, author=MagicMock(member_openid="456"), reply=replies)
                    with patch.object(RunTaskSkill, "execute", worker):
                        await bot._handle_group_message(msg)
                        await drain_runtime(bot)
                    self.assertEqual([m["content"] for m in bot.context_buffer.get_recent_messages("123", 10)
                                      if m["is_bot"]], ["任务完成"])
                else:
                    with patch.object(RunTaskSkill, "execute", worker):
                        await bot._handle_message_core("c2c_456", "please help", replies, user_openid="456")
                        await drain_runtime(bot)
                worker.assert_awaited_once()
                replies.assert_awaited_once()
                self.assertEqual(replies.await_args.args[0] if replies.await_args.args else replies.await_args.kwargs["content"], "任务完成")

    async def test_queued_media_completes_once_while_chat_can_continue(self):
        bot = self.bot()
        runtime = bot._get_runtime()
        context = self.context(bot)
        entered, release = asyncio.Event(), asyncio.Event()
        async def agent(key, prompt, **kwargs):
            entered.set()
            await release.wait()
            return self.media_result(key, prompt, **kwargs)
        self.ai.chat.side_effect = agent
        token = current_event.set(EventContext(context.media_context.scope, "request", time.time()))
        try:
            result = await bot._execute_skill("send_image", {"query": "character"}, context)
        finally:
            current_event.reset(token)
        self.assertFalse(result.success)
        self.assertEqual(result.action_type, "queued")
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(bot.send_action.await_count, 0)
        release.set()
        await asyncio.wait_for(runtime.work_worker, 5)
        texts = [c.args[0] for c in context.media_context.send_text.await_args_list]
        self.assertEqual(texts.count(CAPTION), 1)
        self.assertEqual(len(texts), 2)
        self.assertIn("已加入任务队列", texts[0])
        bot.send_group_message.assert_not_awaited()
        self.assertEqual(runtime.store.jobs(context.media_context.owner)[0]["state"], "completed")

    async def test_legacy_media_and_send_file_get_one_completion_in_both_lanes(self):
        for queued in (False, True):
            for action in ("media", "file"):
                with self.subTest(queued=queued, action=action):
                    bot = self.bot()
                    context = self.context(bot)
                    ctx = context.media_context
                    token = current_event.set(EventContext(ctx.scope, "request", time.time())) if queued else None
                    try:
                        with patch("skills.manager.default_skill_manager.execute_skill", new=AsyncMock(
                                return_value=SkillResult(True, "receipt detail", action_type=action))):
                            result = await bot._execute_skill("fixture", {}, context)
                            if queued:
                                await asyncio.wait_for(bot._runtime.work_worker, 2)
                            else:
                                self.assertTrue(result.reply_handled)
                    finally:
                        if token is not None:
                            current_event.reset(token)
                    texts = [c.args[0] for c in ctx.send_text.await_args_list]
                    self.assertEqual(texts.count(CAPTION), 1)
                    self.assertEqual(len(texts), 2 if queued else 1)

    async def test_web_result_is_delivered_once(self):
        bot = self.bot()
        context = self.context(bot)
        with patch("skills.manager.default_skill_manager.execute_skill", new=AsyncMock(
                return_value=SkillResult(True, "已核实的查询答案", action_type="text"))):
            result = await bot._execute_skill("web_search", {}, context)
        self.assertTrue(result.reply_handled)
        self.assertEqual(result.message, CAPTION)
        context.media_context.send_text.assert_awaited_once_with(result.message)
        self.assertEqual(bot.send_action.await_args.args[0], "upload_group_file")
        path = Path(bot.send_action.await_args.args[1]["file"])
        self.assertEqual(path.read_text(encoding="utf-8"), "已核实的查询答案")

    async def test_text_only_task_preserves_substantive_answer_and_intact_sources(self):
        bot = self.bot()
        context = self.context(bot)
        answer = "用户要求的完整分析结论。\n" * 80
        async def agent(key, prompt, **kwargs):
            kwargs["response_trace"].extend([
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "search_web"}]},
                {"type": "GENERIC", "status": "DONE"}])
            return json.dumps({"answer": answer, "report": REPORT, "sources": SOURCES, "files": []})
        self.ai.chat.side_effect = agent
        result = await bot._execute_skill("run_task", {"task": "分析资料"}, context)
        self.assertTrue(result.success)
        self.assertTrue(result.reply_handled)
        context.media_context.send_text.assert_awaited_once_with(CAPTION)
        self.assertEqual(result.message, CAPTION)
        self.assertEqual(bot.send_action.await_args.args[0], "upload_group_file")
        delivered = Path(bot.send_action.await_args.args[1]["file"]).read_text(encoding="utf-8")
        self.assertTrue(delivered.startswith(answer.strip()))
        self.assertNotIn(REPORT, delivered)
        for source in SOURCES:
            self.assertIn(source["url"], delivered)

    async def test_long_official_text_result_is_delivered_in_full_as_document(self):
        bot = self.bot(official=True)
        context = self.context(bot, official=True)
        answer = "用户要求的完整分析结论。\n" * 80
        self.ai.chat.return_value = json.dumps({"answer": answer, "report": REPORT, "files": []})
        bot.api.post_group_file = AsyncMock(return_value={"file_info": "opaque-file"})
        bot.api.post_group_message = AsyncMock(return_value={"id": "accepted"})
        result = await RunTaskSkill().execute({"task": "分析资料"}, context)
        self.assertTrue(result.success)
        self.assertTrue(result.reply_handled)
        path = next(Path(bot.media.config.cache_dir).rglob("answer.md"))
        self.assertEqual(path.read_text(encoding="utf-8"), answer.strip())
        self.assertEqual(context.media_context.sequence, 2)
        context.media_context.send_text.assert_awaited_once_with(CAPTION, msg_seq=2)

    async def test_full_official_media_quota_does_not_send_or_queue_a_caption(self):
        bot = self.bot(official=True)
        context = self.context(bot, official=True)
        async def agent(key, prompt, **kwargs):
            return task_result(prompt, response_trace=kwargs["response_trace"], count=5)
        self.ai.chat.side_effect = agent
        bot.api.post_group_file = AsyncMock(return_value={"file_info": "opaque-file"})
        bot.api.post_group_message = AsyncMock(return_value={"id": "accepted"})
        result = await bot._execute_skill("send_image", {"query": "character", "count": 5}, context)
        self.assertTrue(result.success)
        self.assertTrue(result.reply_handled)
        self.assertEqual(result.message, "")
        self.assertEqual(context.media_context.sequence, 5)
        self.assertEqual(bot.api.post_group_message.await_count, 5)
        context.media_context.send_text.assert_not_awaited()

    async def test_unknown_send_and_expired_window_have_no_completion_claim(self):
        for unknown in (True, False):
            with self.subTest(unknown=unknown):
                bot = self.bot(official=not unknown)
                context = self.context(bot, official=not unknown)
                self.ai.chat.side_effect = self.media_result
                if unknown:
                    bot.send_action.side_effect = SendUnknown("unknown submission")
                else:
                    context.media_context.received_at = time.time() - 400
                result = await bot._execute_skill("send_image", {"query": "character"}, context)
                self.assertFalse(result.success)
                self.assertEqual(result.data["state"], "unknown" if unknown else "ready")
                self.assertNotIn(CAPTION, [c.args[0] for c in context.media_context.send_text.await_args_list])
                if unknown:
                    await bot.media.handle(context.media_context, MediaIntent(operation="status"))
                    bot.send_action.assert_awaited_once()

    async def test_duplicate_request_does_not_add_another_completion(self):
        bot = self.bot()
        context = self.context(bot)
        self.ai.chat.side_effect = self.media_result
        await bot._execute_skill("send_image", {"query": "character"}, context)
        context.media_context.send_text.reset_mock()
        result = await bot._execute_skill("send_image", {"query": "character"}, context)
        self.assertTrue(result.reply_handled)
        self.assertEqual(context.media_context.send_text.await_count, 1)
        self.assertNotEqual(context.media_context.send_text.await_args.args[0], CAPTION)
        self.assertEqual(bot.send_action.await_count, 3)

    async def test_normal_chat_still_replies_once(self):
        bot = self.bot()
        self.ai.chat.return_value = '{"should_reply":true,"action":"none","reply":"你好呀 (´▽`)"}'
        await bot._handle_private_message({"user_id": 456, "message_id": 114,
            "raw_message": "hello", "sender": {"nickname": "tester"}})
        bot.send_private_message.assert_awaited_once_with("456", "你好呀 (´▽`)")


class AtomicURLTests(unittest.TestCase):
    def test_multiple_urls_in_one_chunk_keep_the_crossing_url_intact(self):
        first, second = "https://vndb.org/v7302", SOURCES[1]["url"]
        text = f"{first}\n来源：{second}\n下一段答案"
        chunks = split_text(text, 50, None)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(any(first in chunk for chunk in chunks))
        self.assertTrue(any(second in chunk for chunk in chunks))

    def test_urls_survive_preferred_boundaries_and_second_transport_split(self):
        for url in (SOURCES[0]["url"], SOURCES[1]["url"], "https://example.com/" + "x" * 300):
            for limit in (1, 20, 100):
                with self.subTest(url=url, limit=limit):
                    text = "资料：" + url + "\n完整说明"
                    chunks = split_text(text, limit, None)
                    self.assertEqual("".join(chunks), text)
                    self.assertTrue(any(url in chunk for chunk in chunks))
                    sent = [part for chunk in chunks for part in split_text(chunk, limit)]
                    self.assertTrue(any(url in chunk for chunk in sent))


if __name__ == "__main__":
    unittest.main()
