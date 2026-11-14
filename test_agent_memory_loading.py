"""File memory is read by the same conversational agent, using offline traces."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from antigravity_client import AntigravityClient


def read_trace(path, *, status="DONE", content='{"summary":"read by agent"}', tool="read_file", serialized=False):
    args = {"AbsolutePath": str(path)}
    return [{"type": "PLANNER_RESPONSE", "tool_calls": [{"id": "read-1", "name": tool,
             "arguments": json.dumps(args) if serialized else args}]},
            {"type": "TOOL_RESULT", "tool_call_id": "read-1", "status": status, "content": content}]


class AgentMemoryLoadingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot_agent_memory_")
        self.directory = Path(self.temp.name).resolve()
        self.current = self.directory / "group_123.json"
        self.current.write_text('{"summary":"PRIVATE_MEMORY_SENTINEL"}', encoding="utf-8")
        self.manifest = {"group_openid": "123", "fingerprint": "manifest-1", "files": [
            {"role": "current", "path": str(self.current), "version": "current-v1", "size": self.current.stat().st_size}]}
        self.ai = self.client()

    def tearDown(self):
        self.temp.cleanup()

    def client(self):
        ai = AntigravityClient(sessions_file=str(self.directory / "sessions.json"))
        ai._is_ready = True
        ai._brain_dir = str(self.directory / "brain")
        ai._run_agentapi_cmd = AsyncMock(return_value={"response": {"newConversation": {"conversationId": "fixture-123"}}})
        ai._wait_for_model_response = AsyncMock(return_value="normal reply")
        ai._read_response_trace = MagicMock(return_value=[])
        return ai

    async def chat(self, message="当前发言者 [tester]: 晚上好", *, ai=None, manifest=None, trace=None):
        return await (ai or self.ai).chat("onebot_group_123", message,
            memory_manifest=self.manifest if manifest is None else manifest, response_trace=trace)

    def prompt(self, ai=None):
        return (ai or self.ai)._run_agentapi_cmd.await_args.args[0][-1]

    async def load_current(self):
        self.ai._read_response_trace.return_value = read_trace(self.current)
        await self.chat()

    async def test_first_turn_gives_paths_to_same_agent_once_and_keeps_bodies_out_of_prompt(self):
        self.ai._read_response_trace.return_value = read_trace(self.current)
        returned_trace = []
        reply = await self.chat(trace=returned_trace)
        self.assertEqual(reply, "normal reply")
        self.ai._run_agentapi_cmd.assert_awaited_once()
        self.ai._wait_for_model_response.assert_awaited_once()
        self.assertEqual(self.ai._run_agentapi_cmd.await_args.args[0][0], "new-conversation")
        self.assertIn(self.current.as_posix(), self.prompt())
        self.assertNotIn("PRIVATE_MEMORY_SENTINEL", self.prompt())
        self.assertIn("晚上好", self.prompt())
        self.assertEqual(len(returned_trace), 2)
        state = self.ai.get_or_create_session("onebot_group_123").memory_context
        self.assertIn(self.ai._memory_file_key(self.current), state["loaded_files"])

    async def test_current_file_daily_version_changes_do_not_force_repeated_reads(self):
        await self.load_current()
        self.current.write_text('{"summary":"NEW_PRIVATE_CONTENT"}', encoding="utf-8")
        self.manifest["files"][0]["version"] = "current-v2"
        self.manifest["fingerprint"] = "manifest-2"
        self.ai._read_response_trace.return_value = []
        await self.chat("当前发言者 [tester]: 今天好吗")
        self.assertEqual(self.ai._run_agentapi_cmd.await_args.args[0][0], "send-message")
        self.assertEqual(self.prompt(), "当前发言者 [tester]: 今天好吗")
        self.assertNotIn("NEW_PRIVATE_CONTENT", self.prompt())
        self.assertEqual(self.ai._wait_for_model_response.await_count, 2)
        self.ai._read_response_trace.assert_called_once()

    async def test_read_failure_or_planner_claim_retries_next_turn_without_breaking_chat(self):
        for trace in (read_trace(self.current, status="ERROR", content="Error: missing"),
                      read_trace(self.current)[:1],
                      read_trace(self.current, content="Error: permission denied"),
                      read_trace(self.directory / "another-group.json")):
            with self.subTest(trace=trace):
                self.ai.reset_session("onebot_group_123")
                self.ai._read_response_trace.return_value = trace
                self.assertEqual(await self.chat(), "normal reply")
                self.ai._read_response_trace.return_value = []
                self.assertEqual(await self.chat(), "normal reply")
                self.assertIn(self.current.as_posix(), self.prompt())
                state = self.ai.get_or_create_session("onebot_group_123").memory_context
                self.assertNotIn(self.ai._memory_file_key(self.current), state["loaded_files"])

    async def test_serialized_mcp_view_arguments_and_matching_result_are_supported(self):
        self.ai._read_response_trace.return_value = read_trace(self.current,
            tool="mcp__filesystem__view_file", serialized=True)
        await self.chat()
        self.ai._read_response_trace.return_value = []
        await self.chat("next")
        self.assertEqual(self.prompt(), "next")

    async def test_short_windows_path_and_resolved_path_share_read_evidence(self):
        original_path = Path(self.temp.name) / self.current.name
        self.ai._read_response_trace.return_value = read_trace(original_path)
        await self.chat()
        self.ai._read_response_trace.return_value = []
        await self.chat("reuse")
        self.assertEqual(self.prompt(), "reuse")

    async def test_existing_chat_receives_one_contract_update_without_reset_or_prewarm(self):
        session = self.ai.get_or_create_session("onebot_group_123")
        session.conversation_id = "old-conversation"
        self.ai._read_response_trace.return_value = read_trace(self.current)
        await self.chat()
        command = self.ai._run_agentapi_cmd.await_args.args[0]
        self.assertEqual(command[:2], ["send-message", "old-conversation"])
        self.assertIn("【本轮运行约定】", command[-1])
        self.ai._run_agentapi_cmd.assert_awaited_once()
        self.ai._read_response_trace.return_value = []
        await self.chat("reuse old conversation")
        self.assertEqual(self.prompt(), "reuse old conversation")
        self.assertEqual(session.conversation_id, "old-conversation")
        saved = (self.directory / "sessions.json").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_MEMORY_SENTINEL", saved)

    async def test_explicit_refresh_rereads_current_but_generic_rules_do_not_trigger_refresh(self):
        await self.load_current()
        self.ai._read_response_trace.return_value = []
        await self.chat("【本群记忆文件】用户明确要求刷新记忆时再读。\n\n当前发言者 [tester]: 你好")
        self.assertNotIn(self.current.as_posix(), self.prompt())
        await self.chat("当前群内发言 [发言人: tester, QQ: 1, 消息ID: 2]: 请重新读取本群记忆")
        self.assertIn(self.current.as_posix(), self.prompt())
        # A failed explicit reread stays pending even after the next ordinary message.
        await self.chat("hello again")
        self.assertIn(self.current.as_posix(), self.prompt())

    async def test_new_archives_are_offered_for_optional_reading_without_reloading_current(self):
        await self.load_current()
        archive = self.directory / "group_123_20261029_120000.json"
        archive.write_text('{"summary":"ARCHIVE_BODY_SENTINEL"}', encoding="utf-8")
        raw = self.directory / "messages.jsonl"
        raw.write_text('{"text":"RAW_MESSAGE_SENTINEL"}', encoding="utf-8")
        self.manifest["files"].extend([
            {"role": "archive", "path": str(archive), "version": "archive-v1", "size": 20},
            {"role": "archive_messages", "path": str(raw), "version": "messages-v1", "size": 20}])
        self.ai._read_response_trace.return_value = []
        await self.chat("next")
        self.assertIn(archive.as_posix(), self.prompt())
        self.assertIn(raw.as_posix(), self.prompt())
        self.assertIn("无需预读", self.prompt())
        self.assertNotIn(self.current.as_posix(), self.prompt())
        self.assertNotIn("ARCHIVE_BODY_SENTINEL", self.prompt())
        self.assertNotIn("RAW_MESSAGE_SENTINEL", self.prompt())
        await self.chat("another normal turn")
        self.assertEqual(self.prompt(), "another normal turn")

    async def test_backend_recreation_requires_reading_again_in_the_new_conversation(self):
        await self.load_current()
        self.ai._run_agentapi_cmd.side_effect = [RuntimeError("404 unknown conversation"),
            {"response": {"newConversation": {"conversationId": "replacement-123"}}}]
        self.ai._read_response_trace.return_value = read_trace(self.current)
        await self.chat("next")
        self.assertIn(self.current.as_posix(), self.prompt())
        self.assertEqual(self.ai.get_or_create_session("onebot_group_123").memory_context["conversation_id"], "replacement-123")

    async def test_read_evidence_survives_restart_only_for_the_same_conversation(self):
        await self.load_current()
        ai = self.client()
        await self.chat("after restart", ai=ai)
        self.assertEqual(ai._run_agentapi_cmd.await_args.args[0][0], "send-message")
        self.assertEqual(self.prompt(ai), "after restart")
        ai.reset_session("onebot_group_123")
        await self.chat("after reset", ai=ai)
        self.assertIn(self.current.as_posix(), self.prompt(ai))

    async def test_probe_work_and_manifest_free_calls_keep_their_original_prompt(self):
        for key, manifest in (("__system_probe__", self.manifest), ("__work__:task:fixture", self.manifest),
                              ("__vision__:group_123", self.manifest), ("onebot_group_123", None)):
            with self.subTest(key=key):
                await self.ai.chat(key, "unchanged prompt", memory_manifest=manifest)
                self.assertEqual(self.prompt(), "unchanged prompt")

    async def test_group_file_sets_do_not_share_loaded_markers(self):
        await self.load_current()
        other = self.directory / "group_456.json"
        other.write_text("{}", encoding="utf-8")
        other_manifest = {"group_openid": "456", "files": [
            {"role": "current", "path": str(other), "version": "v1", "size": 2}]}
        self.ai._read_response_trace.return_value = read_trace(other)
        await self.ai.chat("onebot_group_456", "hello", memory_manifest=other_manifest)
        self.assertIn(other.as_posix(), self.prompt())
        self.assertNotIn(self.current.as_posix(), self.prompt())
        self.ai._read_response_trace.return_value = []
        await self.chat("back to first group")
        self.assertEqual(self.prompt(), "back to first group")
        await self.chat("wrong manifest", manifest=other_manifest)
        self.assertEqual(self.prompt(), "wrong manifest")


if __name__ == "__main__":
    unittest.main()
