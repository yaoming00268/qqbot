"""Semantic desktop dispatch keeps existing input receipts and process boundaries."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

from contextlib import nullcontext
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from computer_control import ControlError, WindowsAutomation
from console_tools.cli import execute
from console_tools.server import handle
from test_desktop_control import FrameBackend


class SemanticBackend(FrameBackend):
    def inspect(self, target, observation):
        return {"engine": "fixture", "text": "正文", "elements": [{"id": "e1", "label": "正文"}]}


class SemanticDesktopTests(unittest.TestCase):
    def setUp(self):
        from test_software_activity import SoftwareControlTests
        self.fixture = SoftwareControlTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.patcher.stop()
        self.backend = SemanticBackend()
        patcher = patch("sandbox_runtime.SandboxRuntime.device", return_value=self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cli_and_mcp_share_inspect_fill_receipts_and_never_replay(self):
        frame = execute("desktop", self.fixture.worker, operation="inspect")
        self.assertEqual(frame["semantic"]["elements"][0]["id"], "e1")
        params = {"observation_id": frame["observation_id"], "element_id": "e1", "text": "中文输入", "wait_ms": 0}
        result = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "qqbot_desktop_fill", "arguments": {"context_file": self.fixture.worker, **params}}})["result"]
        self.assertFalse(result["isError"], result)
        filled = json.loads(result["content"][0]["text"])
        self.assertTrue(filled["input_posted"])
        repeated = execute("desktop", self.fixture.worker, operation="fill", params=params)
        self.assertEqual(filled["action_id"], repeated["action_id"])
        self.assertEqual(len(self.backend.inputs), 1)
        self.assertEqual(self.backend.inputs[0][1]["element_id"], "e1")
        self.assertNotIn("semantic", repeated)

    def test_evidence_backed_software_notes_join_actor_experiences(self):
        from agent_core.store import ActorStore
        store = ActorStore(self.fixture.root / "actors.sqlite3")
        store.upsert_actor("alice", "Alice")
        broker, token = self.fixture.broker.from_context(self.fixture.worker)
        context = broker.context(token)
        context.update(actor_id="alice", actor_database=str(store.path))
        worker = broker.issue_context(context, self.fixture.root / "actor-worker")
        frame = execute("desktop", worker, operation="inspect")
        execute("sandbox", worker, operation="note", params={"text": "实际页面说明录音方法", "kind": "observation", "evidence": [frame["observation_id"]]})
        memory = store.recall("alice", shared_only=True)
        self.assertEqual(memory[0]["kind"], "software_observation")
        self.assertEqual(memory[0]["evidence"], [frame["observation_id"]])
        self.assertIn("实际页面", memory[0]["text"])


class Control:
    def __init__(self, identity, label, *, password=False, pid=77):
        self.runtime, self.Name, self.ProcessId = identity, label, pid
        self.IsPassword, self.IsEnabled, self.AutomationId = password, True, label
        self.ControlTypeName, self.children, self.pattern = "EditControl", [], None

    def GetRuntimeId(self):
        return [self.runtime]

    def GetChildren(self):
        return self.children

    def GetPattern(self, pattern_id):
        return self.pattern


class UIAutomationTests(unittest.TestCase):
    def setUp(self):
        self.root = Control(1, "Root")
        self.edit = Control(2, "普通文本")
        self.root.children = [self.edit, Control(3, "密码", password=True), Control(4, "其他进程", pid=88)]
        self.automation = SimpleNamespace(UIAutomationInitializerInThread=lambda: nullcontext(),
            ControlFromHandle=lambda hwnd: self.root, PatternId=SimpleNamespace(InvokePattern=10000, ValuePattern=10002, SelectionItemPattern=10010))
        self.controller = WindowsAutomation({"hwnd": 11, "pid": 77}, automation=self.automation)

    def test_uia_uses_bound_patterns_and_excludes_passwords_and_other_processes(self):
        semantic = self.controller.inspect()
        self.assertEqual([row["label"] for row in semantic["elements"]], ["Root", "普通文本"])
        pattern = SimpleNamespace(IsReadOnly=False, Value="", invoked=0, selected=0)
        pattern.SetValue = lambda text: setattr(pattern, "Value", text)
        pattern.Invoke = lambda: setattr(pattern, "invoked", pattern.invoked + 1)
        pattern.Select = lambda: setattr(pattern, "selected", pattern.selected + 1)
        self.edit.pattern = pattern
        observed = {"semantic": semantic}
        result = self.controller.act(observed, "fill", "e2", text="实际文本")
        self.assertTrue(result["verified"])
        self.controller.act(observed, "invoke", "e2")
        self.controller.act(observed, "select", "e2")
        self.assertEqual((pattern.Value, pattern.invoked, pattern.selected), ("实际文本", 1, 1))
        self.root.ProcessId = 88
        with self.assertRaises(ControlError):
            self.controller.act(observed, "invoke", "e2")
        self.assertEqual(pattern.invoked, 1)

    def test_missing_pattern_and_changed_runtime_id_stop_before_input(self):
        observed = {"semantic": self.controller.inspect()}
        with self.assertRaises(ControlError) as caught:
            self.controller.act(observed, "fill", "e2", text="内容")
        self.assertIs(caught.exception.input_posted, False)
        self.edit.runtime = 99
        with self.assertRaises(ControlError) as caught:
            self.controller.act(observed, "invoke", "e2")
        self.assertEqual(caught.exception.code, "UIA_ELEMENT_CHANGED")
