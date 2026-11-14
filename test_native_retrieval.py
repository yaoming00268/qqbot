"""Native tools, provenance, migration and honest media delivery regressions."""
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

from antigravity_client import AntigravityClient, SessionInvalidatedError
from bot_client import GeminiQQBot
from config import AppConfig
from media_downloader import MediaError
from media_jobs import MediaService
from media_models import MediaAsset, MediaIntent, MessageEnvelope
from media_search import MediaSearcher
from messaging import SendReceipt
from native_retrieval import NativeRetrievalResult
from onebot_client import OneBotClient
from test_ai_fixture import drain_runtime, orchestrated_response, mock_agent
from skills.base import SkillContext, SkillResult
from skills.media_skills import SendImageSkill
from skills.web_skills import WebSearchSkill
from test_agent_artifacts import task_result


PAGE = "https://example.com/character"
IMAGE = "https://example.com/character.png"


def trace():
    return [
        {"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE",
         "tool_calls": [{"name": "search_web", "args": {"query": "character"}}]},
        {"step_index": 2, "type": "GENERIC", "source": "MODEL", "status": "DONE", "content": f"**[source]({PAGE})**[1]"},
        {"step_index": 3, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE",
         "tool_calls": [{"name": "read_url_content", "args": {"Url": PAGE}}]},
        {"step_index": 4, "type": "GENERIC", "source": "MODEL", "status": "DONE",
         "content": f"Source: {PAGE}\nImage: {IMAGE}"},
    ]


def evidence(payload):
    return NativeRetrievalResult.from_trace(json.dumps(payload), trace())


class NativeRetrievalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        vision = patch("visual_verification.verify_media_image", new=AsyncMock(return_value={"matched": True}))
        vision.start()
        self.addCleanup(vision.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_native_test_")
        self.previous = os.getcwd()
        os.chdir(self.tmp.name)
        self.config = AppConfig()
        self.config.bot.search_enabled = False
        self.ai = mock_agent()
        self.service = MediaService(self.config.media)

    def tearDown(self):
        self.service.cancel_all()
        os.chdir(self.previous)
        self.tmp.cleanup()

    def ctx(self, *, transport="onebot", group=True):
        return MessageEnvelope(transport=transport, account_id="test", conversation_id="1001",
            sender_id="456", message_id="msg1", text="search", send_text=AsyncMock(), is_group=group,
            ai_client=self.ai, ai_session_key="onebot_group_1001")

    async def test_legacy_speed_migrates_without_replacing_conversation(self):
        path = Path("sessions.json")
        path.write_text(json.dumps({"onebot_group_1001": {
            "conversation_id": "old-low", "thinking_level": "low", "model": "pro",
            "updated_at": time.time()}}), encoding="utf-8")
        client = AntigravityClient(sessions_file=str(path))
        session = client.get_or_create_session("onebot_group_1001")
        self.assertEqual(session.conversation_id, "old-low")
        self.assertEqual(session.model, "pro")
        self.assertNotIn("thinking_level", json.loads(path.read_text())["onebot_group_1001"])
        restarted = AntigravityClient(sessions_file=str(path))
        self.assertEqual(restarted.get_or_create_session("onebot_group_1001").conversation_id, "old-low")
        self.assertNotIn("thinking_level", self.config.antigravity.model_dump())

    async def test_new_chat_no_longer_injects_speed_prompt(self):
        client = AntigravityClient(sessions_file="sessions.json")
        client._is_ready = True
        client._run_agentapi_cmd = AsyncMock(return_value={"response": {"newConversation": {"conversationId": "fresh"}}})
        client._wait_for_model_response = AsyncMock(return_value="hello")
        await client.chat("onebot_group_1001", "hello", system_prompt="persona")
        prompt = client._run_agentapi_cmd.await_args.args[0][-1]
        self.assertEqual(prompt, "[系统指令: persona]\n\nhello")

    async def test_waiter_does_not_return_earlier_prose_while_latest_step_uses_tools(self):
        client = AntigravityClient(sessions_file="sessions.json")
        client._brain_dir = str(Path("brain").resolve())
        path = Path(client._brain_dir) / "conv/.system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True)
        rows = [
            {"step_index": 0, "status": "ERROR", "error": "old error"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "searching"},
            {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
             "tool_calls": [{"name": "search_web"}]},
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        async def complete():
            await asyncio.sleep(0.03)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE",
                                         "status": "DONE", "content": "verified result"}) + "\n")
        writer = asyncio.create_task(complete())
        reply = await client._wait_for_model_response("conv", expected_step_min=1, timeout=2)
        await writer
        self.assertEqual(reply, "verified result")

    async def test_planner_claims_and_tool_arguments_are_not_search_evidence(self):
        rows = [{"type": "PLANNER_RESPONSE", "status": "DONE", "content": PAGE,
                 "tool_calls": [{"name": "search_web", "args": {"query": IMAGE}}]}]
        result = NativeRetrievalResult.from_trace("I searched", rows)
        with self.assertRaises(MediaError):
            result.verify()
        self.assertFalse(result.urls)
        result = evidence({})
        result.verify()
        self.assertEqual(result.require_url(IMAGE), IMAGE)
        for unsupported in ("file:///G:/qqbot/random.png", "https://example.com/invented.png"):
            with self.assertRaises(MediaError):
                result.require_url(unsupported)

    async def test_search_only_answer_is_valid_but_cannot_prove_media_page_verification(self):
        result = NativeRetrievalResult.from_trace("answer", trace()[:2])
        result.verify(require_page=False)
        with self.assertRaises(MediaError):
            result.verify()

    async def test_native_waiter_cancels_shell_or_workspace_tool_attempts(self):
        client = AntigravityClient(sessions_file="sessions.json")
        client._brain_dir = str(Path("brain").resolve())
        client._cancel_native_conversation = AsyncMock()
        path = Path(client._brain_dir) / "conv/.system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
            "status": "DONE", "tool_calls": [{"name": "run_command", "args": {"CommandLine": "dir"}}]})
            + "\n" + json.dumps({"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE",
                                   "status": "DONE", "content": "claimed completed search"}), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            await client._wait_for_model_response("conv", timeout=1, native_only=True)
        client._cancel_native_conversation.assert_awaited_once_with("conv")

    async def test_native_query_retries_missing_tools_then_fails(self):
        client = AntigravityClient(sessions_file="sessions.json")
        client.chat = AsyncMock(return_value='{"assets":[]}')
        with self.assertRaises(MediaError):
            await client.native_retrieve("onebot_group_1001", "find art", timeout=1)
        self.assertEqual(client.chat.await_count, 2)

    async def test_regular_web_retrieval_allows_script_acquisition_and_does_not_enable_tool_lock(self):
        client = AntigravityClient(sessions_file="sessions.json")
        async def fetched(*args, response_trace=None, **kwargs):
            response_trace.extend([
                {"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [{"name": "run_command"}]},
                {"type": "GENERIC", "status": "DONE", "content": "Fetched source " + PAGE}])
            self.assertNotIn("native_only", kwargs)
            return json.dumps({"answer": "read page with script", "sources": [{"url": PAGE}]})
        client.chat = AsyncMock(side_effect=fetched)
        result = await client.native_retrieve("group", "read page using available tools", timeout=0)
        self.assertIn("run_command", result.tools)
        self.assertEqual(result.require_url(PAGE), PAGE)

    async def test_source_discovery_keeps_completed_search_when_final_summary_times_out(self):
        client = AntigravityClient(sessions_file="sessions.json")
        async def slow(*args, response_trace=None, **kwargs):
            response_trace.extend(trace()[:2])
            raise TimeoutError("slow summary")
        client.chat = slow
        result = await client.native_retrieve("group", "find source pages", require_page=False)
        self.assertEqual(result.require_url(PAGE), PAGE)
        self.assertEqual(result.tools, frozenset({"search_web"}))
        self.assertEqual(result.text, "")
        with self.assertRaises(TimeoutError):
            await client.native_retrieve("group", "find source pages", require_page=True)

    async def test_timeout_trace_excludes_results_from_previous_turns(self):
        client = AntigravityClient(sessions_file="sessions.json")
        client._is_ready = True
        client._brain_dir = str(Path("brain").resolve())
        session = client.get_or_create_session("__native__:group")
        session.conversation_id = "conv"
        path = Path(client._brain_dir) / "conv/.system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(json.dumps(row) for row in trace()) + "\n", encoding="utf-8")
        client._run_agentapi_cmd = AsyncMock(return_value={})
        client._cancel_native_conversation = AsyncMock()
        async def timeout(*args, **kwargs):
            new_rows = [dict(row, step_index=row["step_index"] + 4) for row in trace()[:2]]
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(json.dumps(row) for row in new_rows) + "\n")
            raise TimeoutError("slow summary")
        client._wait_for_model_response = timeout
        rows = []
        with self.assertRaises(TimeoutError):
            await client.chat("__native__:group", "find pages", response_trace=rows,
                              native_only=True, raise_on_error=True)
        self.assertEqual([row["step_index"] for row in rows], [5, 6])
        self.assertNotIn(IMAGE, NativeRetrievalResult.from_trace("", rows).urls)
        client._cancel_native_conversation.assert_awaited_once_with("conv")

    async def test_native_trace_only_contains_this_turn(self):
        client = AntigravityClient(sessions_file="sessions.json")
        client._is_ready = True
        client._brain_dir = str(Path("brain").resolve())
        session = client.get_or_create_session("onebot_group_1001")
        session.conversation_id = "conv"
        path = Path(client._brain_dir) / "conv/.system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(json.dumps(row) for row in trace()) + "\n", encoding="utf-8")
        async def respond(*args, **kwargs):
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"step_index": 5, "type": "PLANNER_RESPONSE", "source": "MODEL",
                                         "status": "DONE", "content": "new result"}) + "\n")
            return "new result"
        client._run_agentapi_cmd = AsyncMock(return_value={})
        client._wait_for_model_response = respond
        rows = []
        await client.chat("onebot_group_1001", "again", response_trace=rows)
        self.assertEqual([r["step_index"] for r in rows], [5])
        self.assertFalse(NativeRetrievalResult.from_trace("new result", rows).tools)

    async def test_parent_reset_cancels_native_task(self):
        client = AntigravityClient(sessions_file="sessions.json")
        client._is_ready = True
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        client._run_agentapi_cmd = blocked
        task = asyncio.create_task(client.native_retrieve("onebot_group_1001", "find art"))
        await entered.wait()
        client.reset_session("onebot_group_1001")
        with self.assertRaises(SessionInvalidatedError):
            await asyncio.wait_for(task, 2)
        self.assertIsNone(client.get_or_create_session("__native__:onebot_group_1001").conversation_id)

    async def test_media_uses_native_tools_and_ignores_model_local_path(self):
        self.ai.native_retrieve = AsyncMock(return_value=evidence({"assets": [{
            "title": "character", "url": IMAGE, "source_url": PAGE,
            "local_path": "G:/qqbot/unrelated.png", "width": 200, "height": 400}]}))
        searcher = MediaSearcher(self.config.media)
        with patch.object(searcher, "bing", AsyncMock()) as bing, patch.object(searcher, "library") as library, \
                patch.object(searcher.crawler, "discover", AsyncMock(return_value=[])) as crawl:
            assets = await searcher.search(MediaIntent(operation="search_send", query="character", kind="image"),
                                           ai_client=self.ai, session_key="onebot_group_1001")
        self.ai.native_retrieve.assert_awaited_once()
        bing.assert_not_awaited()
        library.assert_not_called()
        crawl.assert_awaited_once()
        self.assertEqual(assets[0].provider, "antigravity")
        self.assertEqual(assets[0].url, IMAGE)
        self.assertEqual(assets[0].local_path, "")

    async def test_unverified_media_and_missing_connection_do_not_fall_back(self):
        searcher = MediaSearcher(self.config.media)
        intent = MediaIntent(operation="search_send", query="character")
        with patch.object(searcher, "commons", AsyncMock()) as commons:
            with self.assertRaises(MediaError):
                await searcher.search(intent)
            self.ai.native_retrieve = AsyncMock(return_value=evidence({"assets": [{
                "url": "https://example.com/invented.png", "source_url": PAGE}]}))
            with patch.object(searcher.crawler, "discover", AsyncMock(return_value=[])) as crawl:
                with self.assertRaises(MediaError):
                    await searcher.search(intent, ai_client=self.ai, session_key="group")
            self.assertEqual(crawl.await_args.args[0][0].url, PAGE)
            commons.assert_not_awaited()

    async def test_handled_search_failure_does_not_mean_send_succeeded(self):
        self.service.searcher.search = AsyncMock(return_value=[])
        self.service.sender.send = AsyncMock()
        self.ai.chat = ConsoleAgentMock(side_effect=MediaError("agent could not find the requested artwork"))
        ctx = self.ctx()
        result = await SendImageSkill().execute({"query": "character"}, SkillContext(
            session_key="group", conversation_id="1001", user_id="456", app_config=self.config,
            media_context=ctx, media_service=self.service, ai_client=self.ai))
        self.assertFalse(result.success)
        self.assertEqual(result.data["state"], "failed")
        self.service.sender.send.assert_not_awaited()
        self.service.searcher.search.assert_not_awaited()
        ctx.send_text.assert_awaited_once()

    async def test_platform_receipt_is_required_for_success(self):
        self.ai.chat = ConsoleAgentMock(side_effect=lambda key, prompt, **kwargs:
            task_result(prompt, response_trace=kwargs["response_trace"]))
        self.service.searcher.search = AsyncMock(side_effect=AssertionError("old search pipeline"))
        async def accepted(*args, before_submit=None, **kwargs):
            before_submit()
            return SendReceipt(status="accepted", message_id="sent-1")
        self.service.sender.send = AsyncMock(side_effect=accepted)
        result = await SendImageSkill().execute({"query": "character"}, SkillContext(
            session_key="group", conversation_id="1001", user_id="456", app_config=self.config,
            media_context=self.ctx(), media_service=self.service, ai_client=self.ai))
        self.assertTrue(result.success)
        self.assertEqual(result.data["accepted"], 1)

    async def test_web_skill_returns_verified_answer_and_sources(self):
        self.ai.native_retrieve = AsyncMock(return_value=evidence({"answer": "verified answer",
            "sources": [{"title": "source", "url": PAGE}]}))
        result = await WebSearchSkill().execute({"query": "question"}, SkillContext(
            session_key="group", conversation_id="1001", user_id="456", app_config=self.config, ai_client=self.ai))
        self.assertTrue(result.success)
        self.assertIn("verified answer", result.message)
        self.assertIn(PAGE, result.message)

    async def test_group_web_skill_delivers_result_instead_of_premature_model_answer(self):
        async def chat(key, prompt, **kwargs):
            if "response_trace" in kwargs:
                kwargs["response_trace"].extend(trace())
                return json.dumps({"answer": "verified answer", "files": [],
                    "sources": [{"title": "source", "url": PAGE}]})
            if '"type": "task_result"' in prompt:
                self.assertIn("verified answer", prompt)
                return '{"should_reply":true,"reply":"任务完成，结果已发送"}'
            return json.dumps({"should_reply": True, "action": "skill",
                "skill": "web_search", "params": {"query": "question"}, "reply": "unverified model guess"})
        self.ai.chat = ConsoleAgentMock(side_effect=chat)
        self.ai.native_retrieve = AsyncMock(return_value=evidence({"answer": "verified answer",
            "sources": [{"title": "source", "url": PAGE}]}))
        ob = OneBotClient(self.config, self.ai)
        ob.self_id = "999"
        ob.send_group_message = AsyncMock()
        ob._is_ws_connected = lambda: True
        ob.send_action = AsyncMock(return_value={"status": "ok", "data": {"file_id": "accepted"}})
        await ob._handle_group_message({"group_id": 1001, "user_id": 456, "message_id": 123,
            "raw_message": "[CQ:at,qq=999] 这个问题最近有变化吗", "sender": {"nickname": "tester"}})
        await drain_runtime(ob)
        sent = "\n".join(str(call.args[1]) for call in ob.send_group_message.await_args_list)
        self.assertIn("任务完成", sent)
        self.assertEqual(self.ai.chat.await_count, 3)
        self.assertNotIn("verified answer", sent)
        self.assertEqual(ob.send_action.await_args.args[0], "upload_group_file")
        delivered = Path(ob.send_action.await_args.args[1]["file"]).read_text(encoding="utf-8")
        self.assertIn("verified answer", delivered)
        self.assertIn(PAGE, delivered)
        self.assertNotIn("unverified model guess", sent)
        self.ai.native_retrieve.assert_not_called()
        await ob._close_runtime()

    async def test_failed_image_task_suppresses_completion_claim(self):
        self.ai.chat = ConsoleAgentMock(side_effect=[json.dumps({"should_reply": True, "action": "skill",
            "skill": "send_image", "params": {"query": "character"}, "reply": "already sent the picture"}),
            MediaError("native tools unavailable"), '{"should_reply":true,"reply":"native tools unavailable"}'])
        self.ai.native_retrieve = AsyncMock(side_effect=MediaError("native tools unavailable"))
        ob = OneBotClient(self.config, self.ai)
        ob.self_id = "999"
        ob.send_group_message = AsyncMock()
        ob.send_action = AsyncMock()
        await ob._handle_group_message({"group_id": 1001, "user_id": 456, "message_id": 123,
            "raw_message": "[CQ:at,qq=999] send a picture", "sender": {"nickname": "tester"}})
        await drain_runtime(ob)
        sent = "\n".join(str(call.args[1]) for call in ob.send_group_message.await_args_list)
        self.assertIn("任务未完成", sent)
        coordinator = ob._get_main_coordinator()
        saved = next(coordinator.directory("onebot_group_1001").glob("result-*.json"))
        self.assertIn("native tools unavailable", saved.read_text(encoding="utf-8"))
        self.assertEqual(self.ai.chat.await_count, 3)
        self.assertNotIn("already sent the picture", sent)
        ob.send_action.assert_not_awaited()
        await ob._close_runtime()

    async def test_official_completion_respects_sequences_already_used_by_media(self):
        self.config.bot.max_reply_length = 20
        decision = '{"should_reply":false,"tasks":[{"type":"image","task":"角色立绘"}]}'
        self.ai.chat = ConsoleAgentMock(side_effect=orchestrated_response(decision))
        bot = GeminiQQBot(config=self.config, antigravity_client=self.ai)
        replies = AsyncMock()
        ctx = bot._make_media_context(session_key="c2c_456", user_id="456", conversation_id="456",
            message_id="original", text="send character art", reply=replies)
        async def sent(*args, **kwargs):
            ctx.sequence = 3
            return SkillResult(True, "已准备", data={"answer":"三张图片已核对", "files":[]})
        with patch("skills.task_skills.RunTaskSkill.execute", AsyncMock(side_effect=sent)):
            await bot._handle_message_core("c2c_456", "send character art", replies, user_openid="456", media_context=ctx)
            await drain_runtime(bot)
        self.assertEqual([call.kwargs.get("msg_seq") for call in replies.await_args_list], [4])
        await bot._close_runtime()


if __name__ == "__main__":
    unittest.main()
