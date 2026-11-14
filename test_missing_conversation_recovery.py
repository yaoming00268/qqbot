"""Missing backend conversations recover without extra model or QQ calls."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from antigravity_client import (AgentServiceError, AntigravityClient,
    ConversationMissingError, ConversationRecoveryError, SessionInvalidatedError)


OLD = "deleted-conversation"
NEW = "replacement-conversation"
MISSING = "rpc error: code = NotFound desc = not_found: trajectory not found: " + OLD
KEY = "onebot_group_123"


class MissingConversationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot_missing_conversation_")
        self.root = Path(self.temp.name)
        self.ai = AntigravityClient(sessions_file=str(self.root / "sessions.json"))
        self.ai._is_ready = True
        self.ai._brain_dir = str(self.root / "brain")
        self.ai._cancel_native_conversation = AsyncMock()
        self.ai._CONVERSATION_CHECK_DELAY = 0

    def tearDown(self):
        self.temp.cleanup()

    def session(self, key=KEY):
        session = self.ai.get_or_create_session(key)
        session.conversation_id = OLD
        session.model = "pro"
        session.memory_context = {"conversation_id": OLD, "loaded_files": {"old-only": {}}}
        session.chat_state = {"conversation_id": OLD, "rules_hash": "old", "buffer_seen": ["old-message"]}
        self.ai._save_sessions()
        return session

    def transcript(self, cid, rows):
        path = self.root / "brain" / cid / ".system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def completion(self, cid=NEW, *, step=0, text="恢复后正常回复"):
        self.transcript(cid, [{"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE",
                               "status": "DONE", "content": text}])

    def new_response(self):
        self.completion()
        return {"response": {"newConversation": {"conversationId": NEW}}}

    def commands(self):
        return [call.args[0][0] for call in self.ai._run_agentapi_cmd.await_args_list]

    async def test_real_trajectory_error_rebuilds_group_and_private_conversations(self):
        for key in ("onebot_group_123", "onebot_private_123", "group_123", "c2c_123"):
            with self.subTest(key=key):
                session = self.session(key)
                generation = session.generation
                async def command(args, **kwargs):
                    if args[0] == "send-message":
                        raise RuntimeError(MISSING)
                    return self.new_response()
                self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
                with self.assertLogs("AntigravityClient", level="INFO") as logs:
                    result = await self.ai.chat(key, "继续刚才的问题", system_prompt="当前人设", allow_empty_response=True)
                self.assertEqual(result, "恢复后正常回复")
                self.assertEqual(self.commands(), ["send-message", "new-conversation"])
                args = self.ai._run_agentapi_cmd.await_args.args[0]
                self.assertIn("--model=pro", args)
                self.assertIn("当前人设", args[-1])
                self.assertIn("继续刚才的问题", args[-1])
                self.assertEqual(session.generation, generation)
                self.assertFalse(session.memory_context)
                self.assertFalse(session.chat_state)
                self.assertEqual(session.conversation_id, NEW)
                self.assertTrue(any("对话已自动重建" in line for line in logs.output))
                restarted = AntigravityClient(sessions_file=self.ai.sessions_file)
                self.assertEqual(restarted.get_or_create_session(key).conversation_id, NEW)

    async def test_success_exit_with_error_json_also_recovers(self):
        errors = ({"error": {"code": "NOT_FOUND", "message": "trajectory not found"}},
                  {"response": {"error": "cascade has been deleted"}},
                  {"response": {"sendMessage": {"success": False, "message": "对话不存在"}}})
        for response in errors:
            with self.subTest(response=response):
                self.session()
                self.ai._run_agentapi_cmd = AsyncMock(side_effect=[response, self.new_response()])
                self.assertEqual(await self.ai.chat(KEY, "hello"), "恢复后正常回复")
                self.assertEqual(self.commands(), ["send-message", "new-conversation"])

    async def test_raw_cli_zero_exit_does_not_hide_error_json(self):
        self.ai._executable = "fixture-agentapi.exe"
        process = MagicMock(returncode=0)
        process.communicate = AsyncMock(return_value=(json.dumps({"error": "trajectory not found"}).encode(), b""))
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            with self.assertRaises(ConversationMissingError):
                await self.ai._run_agentapi_cmd_once(["send-message", OLD, "hello"])

    async def test_deleted_during_generation_rebuilds_before_accepting_blank_done(self):
        self.session()
        async def command(args, **kwargs):
            if args[0] == "send-message":
                self.transcript(OLD, [
                    {"step_index": 0, "type": "SYSTEM_ERROR", "message": "cascade not found"},
                    {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": ""}])
                return {}
            return self.new_response()
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        self.assertEqual(await self.ai.chat(KEY, "请继续", allow_empty_response=True), "恢复后正常回复")
        self.assertEqual(self.commands(), ["send-message", "new-conversation"])

    async def test_missing_or_stale_logs_use_metadata_to_confirm_deletion(self):
        for stale_log in (False, True):
            with self.subTest(stale_log=stale_log):
                self.session()
                if stale_log:
                    self.completion(OLD, text="不能再次发送的旧答案")
                async def command(args, **kwargs):
                    if args[0] == "send-message":
                        return {}
                    if args[0] == "get-conversation-metadata":
                        self.assertEqual(args[1], OLD)
                        self.assertLessEqual(kwargs["timeout"], 5)
                        raise RuntimeError(MISSING)
                    return self.new_response()
                self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
                result = await asyncio.wait_for(self.ai.chat(KEY, "这条不能静默", allow_empty_response=True), 2)
                self.assertEqual(result, "恢复后正常回复")
                self.assertEqual(self.commands(), ["send-message", "get-conversation-metadata", "new-conversation"])

    async def test_late_logs_for_existing_conversation_do_not_create_another(self):
        session = self.session()
        async def command(args, **kwargs):
            if args[0] == "get-conversation-metadata":
                self.completion(OLD, text="日志稍晚出现的正常回答")
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        result = await asyncio.wait_for(self.ai.chat(KEY, "慢慢回答"), 2)
        self.assertEqual(result, "日志稍晚出现的正常回答")
        self.assertEqual(self.commands(), ["send-message", "get-conversation-metadata"])
        self.assertEqual(session.conversation_id, OLD)

    async def test_metadata_failures_do_not_claim_conversation_was_deleted(self):
        session = self.session()
        for error in (TimeoutError("slow metadata"), RuntimeError("503 unavailable"),
                      RuntimeError("HTTP 404 unrelated resource"), RuntimeError("unknown command get-conversation-metadata")):
            with self.subTest(error=error):
                self.ai._run_agentapi_cmd = AsyncMock(side_effect=error)
                await self.ai._ensure_conversation_present(OLD)
                self.assertEqual(session.conversation_id, OLD)
                self.assertEqual(self.commands(), ["get-conversation-metadata"])

    async def test_metadata_not_found_code_without_message_is_confirmed_missing(self):
        self.ai._run_agentapi_cmd = AsyncMock(return_value={"error": {"code": 5}})
        with self.assertRaises(ConversationMissingError):
            await self.ai._ensure_conversation_present(OLD)

    async def test_repeated_deletion_stops_after_one_rebuild_and_clears_stale_id(self):
        session = self.session()
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=[RuntimeError(MISSING), self.new_response()])
        self.ai._wait_for_model_response = AsyncMock(side_effect=ConversationMissingError(NEW))
        with self.assertLogs("AntigravityClient", level="WARNING") as logs:
            with self.assertRaises(ConversationRecoveryError) as caught:
                await self.ai.chat(KEY, "请继续")
        self.assertEqual(self.commands(), ["send-message", "new-conversation"])
        self.assertIsNone(session.conversation_id)
        self.assertIn("自动重建未完成", caught.exception.user_message)
        self.assertTrue(any("本轮不再重复重建" in line for line in logs.output))
        saved = json.loads(Path(self.ai.sessions_file).read_text(encoding="utf-8"))
        self.assertIsNone(saved[KEY]["conversation_id"])

    async def test_failed_creation_is_visible_and_next_message_can_retry(self):
        session = self.session()
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=[RuntimeError(MISSING), RuntimeError("503 unavailable")])
        with self.assertLogs("AntigravityClient", level="ERROR") as logs:
            with self.assertRaises(ConversationRecoveryError):
                await self.ai.chat(KEY, "当前问题")
        self.assertIsNone(session.conversation_id)
        self.assertTrue(any("对话自动重建失败" in line for line in logs.output))
        self.ai._run_agentapi_cmd = AsyncMock(return_value=self.new_response())
        self.assertEqual(await self.ai.chat(KEY, "再试一次"), "恢复后正常回复")
        self.assertEqual(self.commands(), ["new-conversation"])

    async def test_intentional_silence_is_not_treated_as_missing(self):
        session = self.session()
        async def command(args, **kwargs):
            self.completion(OLD, text="")
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        self.assertEqual(await self.ai.chat(KEY, "暂时不要回复", allow_empty_response=True), "")
        self.assertEqual(self.commands(), ["send-message"])
        self.assertEqual(session.conversation_id, OLD)

    async def test_system_failure_is_not_misread_as_intentional_silence(self):
        session = self.session()
        async def command(args, **kwargs):
            self.transcript(OLD, [
                {"step_index": 0, "type": "SYSTEM_ERROR", "message": "503 model unavailable"},
                {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": ""}])
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        with self.assertRaises(AgentServiceError):
            await self.ai.chat(KEY, "hello", allow_empty_response=True)
        self.assertEqual(self.commands(), ["send-message"])
        self.assertEqual(session.conversation_id, OLD)

    async def test_concurrent_messages_reuse_the_single_replacement(self):
        session = self.session()
        async def command(args, **kwargs):
            if args[:2] == ["send-message", OLD]:
                raise RuntimeError(MISSING)
            if args[0] == "new-conversation":
                return self.new_response()
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        self.ai._wait_for_model_response = AsyncMock(return_value="正常回答")
        replies = await asyncio.wait_for(asyncio.gather(self.ai.chat(KEY, "第一条"), self.ai.chat(KEY, "第二条")), 2)
        self.assertEqual(replies, ["正常回答", "正常回答"])
        self.assertEqual(self.commands(), ["send-message", "new-conversation", "send-message"])
        self.assertEqual(self.ai._run_agentapi_cmd.await_args.args[0][1], NEW)
        self.assertEqual(session.conversation_id, NEW)

    async def test_explicit_reset_during_rebuild_cannot_restore_cancelled_mapping(self):
        session = self.session()
        creating = asyncio.Event()
        async def command(args, **kwargs):
            if args[0] == "send-message":
                raise RuntimeError(MISSING)
            creating.set()
            await asyncio.Event().wait()
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        running = asyncio.create_task(self.ai.chat(KEY, "old request"))
        await asyncio.wait_for(creating.wait(), 1)
        self.ai.reset_session(KEY)
        with self.assertRaises(SessionInvalidatedError):
            await asyncio.wait_for(running, 1)
        self.assertIsNone(session.conversation_id)
        self.assertFalse(session.lock.locked())


class MissingConversationIngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import test_startup_participation
        self.fixture = test_startup_participation.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_deleted_backend_conversation_reaches_group_in_both_transports(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, key = self.fixture.bot(official, stored_cid=OLD)
                ai = bot.ai
                ai.chat = AntigravityClient.chat.__get__(ai)
                ai._is_ready = True
                ai._brain_dir = str(Path(f"brain_{official}").resolve())
                ai._cancel_native_conversation = AsyncMock()
                async def command(args, **kwargs):
                    if args[0] == "send-message":
                        raise RuntimeError(MISSING)
                    from console_tools.cli import execute
                    execute("reply", list(bot._get_main_coordinator().requests)[-1],
                            text="重建后继续回答当前问题", finish=True)
                    path = Path(ai._brain_dir) / NEW / ".system_generated/logs/transcript.jsonl"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    # The real backend logs the input before its model output;
                    # include it so this ingress test also checks turn binding.
                    path.write_text(json.dumps({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                        "status": "DONE", "content": args[-1]}) + "\n" + json.dumps({"step_index": 1,
                        "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                        "content": "重建后继续回答当前问题"}), encoding="utf-8")
                    return {"response": {"newConversation": {"conversationId": NEW}}}
                ai._run_agentapi_cmd = AsyncMock(side_effect=command)
                event = await self.fixture.receive(bot, official, "recover", "请继续回答这个问题")
                sends = event.reply if official else bot.send_group_message
                sends.assert_awaited_once()
                if official:
                    self.assertIn("重建后继续回答当前问题", sends.await_args.kwargs["content"])
                else:
                    self.assertIn("重建后继续回答当前问题", str(sends.await_args.args[1]))
                self.assertEqual(ai.get_or_create_session(key).conversation_id, NEW)
                self.assertEqual(ai.get_or_create_session(key).chat_state["conversation_id"], NEW)
                self.assertEqual([c.args[0][0] for c in ai._run_agentapi_cmd.await_args_list],
                                 ["send-message", "new-conversation"])

