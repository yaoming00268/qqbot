"""DSH session bookkeeping, tool boundaries and typed configuration regressions."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from antigravity_client import AntigravityClient
from config import AppConfig
from dsh_client import DeepSeekHarnessClient, normalized_trace
from dsh_profile import build_patch
from model_backend import create_ai_client
import yaml


class BackendTests(unittest.TestCase):
    def test_factory_uses_separate_session_maps_and_model_change_keeps_history(self):
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig()
            config.model_backend.backend = "deepseek_harness"
            config.deepseek_harness.home = str(Path(directory) / "dsh")
            ai = create_ai_client(config, sessions_file=str(Path(directory) / "sessions.json"))
            self.assertIsInstance(ai, DeepSeekHarnessClient)
            self.assertTrue(ai.sessions_file.endswith("sessions_dsh.json"))
            session = ai.get_or_create_session("group_fixture")
            session.conversation_id = "qqbot-saved-identity"
            session.chat_state["protocol"] = "loaded"
            self.assertTrue(ai.set_session_model("group_fixture", "pro"))
            self.assertEqual(session.conversation_id, "qqbot-saved-identity")
            self.assertEqual(session.chat_state["protocol"], "loaded")
            restored = create_ai_client(config, sessions_file=str(Path(directory) / "sessions.json"))
            self.assertEqual(restored.get_or_create_session("group_fixture").conversation_id, "qqbot-saved-identity")

    def test_patch_inserts_official_mcp_and_preserves_persistence_root(self):
        with tempfile.TemporaryDirectory() as directory:
            patch = yaml.safe_load(build_patch(directory, scope="main", system_prompt="persistent persona").read_text(encoding="utf-8"))
            entries = {row["id"]: row for row in patch if "id" in row}
            self.assertEqual(entries["session-persistence-jsonl"]["config"]["root"], str((Path(directory) / "sessions").resolve()))
            self.assertTrue(entries["tool-fs"]["disabled"])
            self.assertTrue(entries["tool-pwsh"]["disabled"])
            mcp = next(row["insert"][0] for row in patch if "insert" in row)
            self.assertEqual(mcp["name"], "@deepseek-ai/dsh-mcp-client")
            self.assertEqual(mcp["config"]["env"]["QQBOT_MCP_ROLE"], "main")

    def test_parallel_memory_tool_results_are_bound_to_matching_calls(self):
        def notification(kind, data):
            return SimpleNamespace(method="session.event", payload={"event": {"type": kind, "data": data}})
        path1, path2 = str(Path("one.md").resolve()), str(Path("two.md").resolve())
        notifications = [notification("tool/call", {"callId": "a", "name": "mcp__qqbot__qqbot_read_memory", "arguments": json.dumps({"path": path1})}),
            notification("tool/call", {"callId": "b", "name": "mcp__qqbot__qqbot_read_memory", "arguments": json.dumps({"path": path2})}),
            notification("tool/result", {"message": {"toolCallId": "b", "content": [{"type": "text", "text": '{"ok":true}'}]}}),
            notification("tool/result", {"message": {"toolCallId": "a", "content": [{"type": "text", "text": '{"ok":false}'}], "isError": True}})]
        completed = AntigravityClient._completed_memory_reads(normalized_trace(notifications))
        self.assertEqual(completed, {AntigravityClient._memory_file_key(path2)})
