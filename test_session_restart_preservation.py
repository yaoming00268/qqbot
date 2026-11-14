"""Offline regressions for durable user conversations and temporary cleanup."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from antigravity_client import AntigravityClient
from group_memory import GroupMemoryManager


TARGET_CID = "1a831595-4664-4dbb-96f0-c75e163b8a01"


class SessionRestartPreservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_session_restart_")
        self.directory = Path(self.tmp.name).resolve()
        self.path = self.directory / "sessions.json"

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self, keys, *, legacy=False):
        rows = {}
        for key in keys:
            rows[key] = {"conversation_id": TARGET_CID, "model": "pro", "updated_at": 1,
                         "memory_context": {"conversation_id": TARGET_CID, "loaded_files": {"fixture": {}}}}
            if legacy:
                rows[key]["thinking_level"] = "low"
        self.path.write_text(json.dumps(rows), encoding="utf-8")

    def client(self):
        ai = AntigravityClient(sessions_file=str(self.path), session_timeout_minutes=1)
        ai._is_ready = True
        ai._brain_dir = str(self.directory / "brain")
        ai._run_agentapi_cmd = AsyncMock(return_value={"response": {"newConversation": {"conversationId": "new-fixture-conversation"}}})
        ai._wait_for_model_response = AsyncMock(return_value="continued reply")
        ai._cancel_native_conversation = AsyncMock()
        ai._read_response_trace = MagicMock(return_value=[])
        return ai

    def persisted(self, key):
        return json.loads(self.path.read_text(encoding="utf-8"))[key]

    async def assert_reuses_original(self, key):
        restarted = self.client()
        self.assertEqual(await restarted.chat(key, "继续聊天", raise_on_error=True), "continued reply")
        restarted._run_agentapi_cmd.assert_awaited_once()
        args = restarted._run_agentapi_cmd.await_args.args[0]
        self.assertEqual(args[:2], ["send-message", TARGET_CID])
        self.assertEqual(self.persisted(key)["conversation_id"], TARGET_CID)

    async def test_old_group_and_private_ids_survive_idle_restart_and_legacy_migration(self):
        keys = ("onebot_group_1", "onebot_private_2", "group_short", "c2c_user", "channel_custom", "direct_custom")
        self.seed(keys, legacy=True)
        ai = self.client()
        for key in keys:
            with self.subTest(key=key):
                session = ai.get_or_create_session(key)
                self.assertEqual(session.conversation_id, TARGET_CID)
                self.assertEqual(session.model, "pro")
                self.assertEqual(session.memory_context["loaded_files"], {"fixture": {}})
                self.assertNotIn("thinking_level", self.persisted(key))
                await self.assert_reuses_original(key)

    async def test_auxiliary_idle_expiry_still_applies(self):
        self.seed(["temporary_fixture"])
        ai = self.client()
        self.assertIsNone(ai.get_or_create_session("temporary_fixture").conversation_id)
        session = ai.get_or_create_session("temporary_fixture")
        session.conversation_id = TARGET_CID
        session.last_active = 1
        self.assertIsNone(ai.get_or_create_session("temporary_fixture").conversation_id)

    async def test_malformed_timestamp_does_not_hide_later_conversation(self):
        self.seed(["onebot_group_bad", "onebot_group_good"])
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        rows["onebot_group_bad"]["updated_at"] = {"invalid": True}
        self.path.write_text(json.dumps(rows), encoding="utf-8")
        ai = self.client()
        self.assertEqual(ai.get_or_create_session("onebot_group_bad").conversation_id, TARGET_CID)
        self.assertEqual(ai.get_or_create_session("onebot_group_good").conversation_id, TARGET_CID)

    async def test_shutdown_archive_preserves_organizer_and_reuses_main_ids(self):
        keys = ["onebot_group_123", "group_123", "onebot_private_456"]
        self.seed(keys)
        ai = self.client()
        ai._wait_for_model_response.return_value = '{"summary":"saved discussion","member_portraits":{}}'
        ai.archive_session = AsyncMock(return_value=True)
        memory = GroupMemoryManager(str(self.directory / "memories"))
        memory.record_session_message("123", "456", "Member", "old discussion")
        saved = await memory.summarize_and_save_session(ai, cleanup_chats=True)
        self.assertTrue(Path(saved["123"]).is_file())
        ai.archive_session.assert_not_awaited()
        self.assertEqual(self.persisted("group_summary_123")["conversation_id"], "new-fixture-conversation")
        for key in keys:
            self.assertEqual(self.persisted(key)["conversation_id"], TARGET_CID)
            await self.assert_reuses_original(key)

    async def test_archiving_main_conversations_is_denied_before_backend_initialization(self):
        keys = ["onebot_group_123", "onebot_private_456", "group_short", "c2c_1", "unknown",
                "group_summary_123", "activity_summary_fixture"]
        self.seed(keys)
        ai = self.client()
        ai._is_ready = False
        ai.initialize = MagicMock(side_effect=AssertionError("must not initialize backend"))
        archive = self.directory / "archive.json"
        archive.write_text('{"summary":"saved"}', encoding="utf-8")
        for key in keys:
            with self.subTest(key=key):
                self.assertFalse(await ai.archive_session(key, str(archive)))
                self.assertEqual(ai.get_or_create_session(key).conversation_id,
                                 None if key == "unknown" else TARGET_CID)
        ai.initialize.assert_not_called()

    async def test_completed_temporary_branch_can_still_be_archived(self):
        for key in ("__work__:onebot_group_123", "__vision__:fixture", "__native__:fixture",
                    "__qq_capture__:fixture", "__system_probe__"):
            with self.subTest(key=key):
                self.seed([key])
                ai = self.client()
                session = ai.get_or_create_session(key)
                session.conversation_id = TARGET_CID
                session.last_active = time.time()
                ai._save_sessions()
                archive = self.directory / "archive.json"
                archive.write_text('{"summary":"saved"}', encoding="utf-8")
                response = MagicMock()
                response.read = AsyncMock()
                post = MagicMock()
                post.__aenter__ = AsyncMock(return_value=response)
                post.__aexit__ = AsyncMock(return_value=False)
                http = MagicMock()
                http.post.return_value = post
                http_context = MagicMock()
                http_context.__aenter__ = AsyncMock(return_value=http)
                http_context.__aexit__ = AsyncMock(return_value=False)
                with patch("aiohttp.ClientSession", return_value=http_context):
                    self.assertTrue(await ai.archive_session(key, str(archive), expected_id=TARGET_CID))
                self.assertIsNone(session.conversation_id)
                self.assertIsNone(self.persisted(key)["conversation_id"])
                self.assertEqual(http.post.call_args.kwargs["json"], {"cascadeId": TARGET_CID})

    async def test_shutdown_cancellation_preserves_main_id_on_disk(self):
        for key in ("onebot_group_123", "onebot_private_456", "group_short", "c2c_user"):
            with self.subTest(key=key):
                self.seed([key])
                ai = self.client()
                entered = asyncio.Event()
                async def waiting(*args, **kwargs):
                    entered.set()
                    await asyncio.Event().wait()
                ai._wait_for_model_response.side_effect = waiting
                task = asyncio.create_task(ai.chat(key, "稍后继续", raise_on_error=True))
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                ai._cancel_native_conversation.assert_awaited_once_with(TARGET_CID)
                self.assertEqual(ai.get_or_create_session(key).conversation_id, TARGET_CID)
                await self.assert_reuses_original(key)

    async def test_temporary_send_failures_never_replace_main_id(self):
        for key in ("onebot_group_123", "onebot_private_456"):
            for message in ("503 backend temporarily unavailable", "429 quota exhausted",
                            "agentapi executable not found", "HTTP 404: unrelated resource missing"):
                with self.subTest(key=key, message=message):
                    self.seed([key])
                    ai = self.client()
                    ai._run_agentapi_cmd.side_effect = RuntimeError(message)
                    with self.assertRaisesRegex(RuntimeError, message):
                        await ai.chat(key, "继续", raise_on_error=True)
                    self.assertEqual(ai._run_agentapi_cmd.await_count, 1)
                    self.assertEqual(ai._run_agentapi_cmd.await_args.args[0][:2], ["send-message", TARGET_CID])
                    await self.assert_reuses_original(key)

    async def test_timeout_or_model_error_preserves_main_id(self):
        for key in ("onebot_group_123", "onebot_private_456"):
            for error in (TimeoutError("slow model"), RuntimeError("model quota temporarily exhausted")):
                with self.subTest(key=key, error=error):
                    self.seed([key])
                    ai = self.client()
                    ai._wait_for_model_response.side_effect = error
                    with self.assertRaises(type(error)):
                        await ai.chat(key, "继续", raise_on_error=True)
                    self.assertEqual(ai._run_agentapi_cmd.await_count, 1)
                    await self.assert_reuses_original(key)

    async def test_confirmed_missing_conversation_rebuilds_once_and_persists(self):
        for message in ("Conversation not found on server", "404 unknown conversation", "会话已删除"):
            with self.subTest(message=message):
                key = "onebot_group_123"
                self.seed([key])
                ai = self.client()
                async def command(args, timeout=None):
                    if args[0] == "send-message":
                        raise RuntimeError(message)
                    return {"response": {"newConversation": {"conversationId": "replacement-fixture"}}}
                ai._run_agentapi_cmd.side_effect = command
                self.assertEqual(await ai.chat(key, "继续", raise_on_error=True), "continued reply")
                self.assertEqual([call.args[0][0] for call in ai._run_agentapi_cmd.await_args_list],
                                 ["send-message", "new-conversation"])
                self.assertEqual(self.persisted(key)["conversation_id"], "replacement-fixture")
                self.assertEqual(self.client().get_or_create_session(key).conversation_id, "replacement-fixture")

    async def test_explicit_clear_and_model_switch_still_invalidate_saved_mapping(self):
        for action in ("reset_session", "reset_all_sessions", "set_session_model"):
            with self.subTest(action=action):
                key = "onebot_group_123"
                self.seed([key])
                ai = self.client()
                if action == "reset_all_sessions":
                    ai.reset_all_sessions()
                elif action == "set_session_model":
                    self.assertTrue(ai.set_session_model(key, "flash"))
                else:
                    self.assertTrue(ai.reset_session(key))
                self.assertIsNone(self.persisted(key)["conversation_id"])
                self.assertIsNone(self.client().get_or_create_session(key).conversation_id)


if __name__ == "__main__":
    unittest.main()
