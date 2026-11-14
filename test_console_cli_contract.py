"""Exercise public commands through JSON receipts, including real child shells."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from console_tools.broker import ConsoleBroker
from console_tools.cli import main
from console_tools.contracts import invocation
from console_tools.server import handle
from sandbox_runtime import SandboxRuntime
from test_desktop_control import FrameBackend


class PublicCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qq CLI 中文 ' ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / "应用 空间/chrome.exe"
        self.exe.parent.mkdir()
        self.exe.touch()
        self.output = self.root / "成果 目录"
        self.output.mkdir()
        self.material = self.root / "资料 目录"
        self.material.mkdir()
        self.metadata = {"namespace": "account", "scope": "group", "sender_id": "owner", "is_master": True,
            "source_text": f"使用 {self.exe} https://example.com/ 允许写入 {self.material}",
            "output_dir": str(self.output), "artifact_roots": [str(self.output)],
            "sandbox_config": {"enabled": True, "directory": str(self.root / "sandbox"), "mode": "background",
                "max_actions": 0, "browser_profiles": {"video": str(self.root / "登录目录")}}}
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.owner = self.broker.issue_context(self.metadata, self.root / "上下文 ' 目录")
        self.backend = FrameBackend()
        self.backend.launch = lambda target: {"pid": 77}
        device = patch("sandbox_runtime.SandboxRuntime.device", return_value=self.backend)
        device.start()
        self.addCleanup(device.stop)
        self.runtime = SandboxRuntime(self.root / "sandbox")

    def cli(self, *arguments, context=None, exit_code=0):
        output, errors = io.StringIO(), io.StringIO()
        argv = [*arguments, "--context-file", context or self.owner]
        code = 0
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                main(argv)
            except SystemExit as error:
                code = error.code
        self.assertEqual(code, exit_code, (argv, output.getvalue(), errors.getvalue()))
        self.assertNotIn("Traceback", errors.getvalue())
        return json.loads(output.getvalue())

    def start(self):
        receipt = self.cli("sandbox", "use", "--executable", str(self.exe), "--profile", "video",
            "--url", "https://example.com/", "--text", "先阅读指定的配乐讨论。")
        self.request_id = receipt["id"]
        self.target = json.loads(self.broker.record(self.request_id)["result"])["target_id"]
        self.sid, _ = self.runtime.begin(self.metadata, self.target, self.request_id)
        self.broker.update(self.request_id, "running", {"target_id": self.target, "sandbox_session_id": self.sid})
        self.worker_metadata = {**self.metadata, "sandbox_session_id": self.sid, "parent_request_id": self.request_id, "activity_revision": 0}
        self.worker = self.broker.issue_context(self.worker_metadata, self.root / "worker")

    def test_complete_observe_input_note_redirect_finish_workflow_without_parameter_files(self):
        self.start()
        context = self.cli("context", context=self.worker)
        self.assertEqual(context["role"], "worker")
        self.assertEqual(context["binding"]["request_id"], self.request_id)
        self.assertIn(self.worker, context["examples"]["sandbox note"]["argv"])
        self.assertNotIn("steer", context["capabilities"]["sandbox"])
        frame = self.cli("desktop", "screenshot", context=self.worker)
        typed = self.root / "中文输入.txt"
        typed.write_text("花咲 'Work Spring'\n配乐与人物", encoding="utf-16")
        frame = self.cli("desktop", "type", "--observation-id", frame["observation_id"], "--text-file", str(typed), "--wait-ms", "0", context=self.worker)
        self.assertEqual(self.backend.inputs[-1][1]["text"], typed.read_text(encoding="utf-16"))
        frame = self.cli("desktop", "scroll", "--observation-id", frame["observation_id"], "--delta", "600", "--wait-ms", "0", context=self.worker)
        note = self.cli("sandbox", "note", "--kind", "observation", "--text", "配乐访谈讨论钢琴主题。",
            "--evidence", frame["observation_id"], context=self.worker)
        self.assertEqual(note["source"], "software_observation")
        notes = self.cli("sandbox", "notes", "--query", "钢琴", context=self.worker)
        self.assertEqual(notes["notes"][0]["id"], note["id"])
        changed = self.cli("sandbox", "steer", "--text", "继续阅读钢琴主题的作曲访谈。")
        self.assertEqual(changed["id"], self.request_id)
        self.assertEqual(changed["direction_revision"], 1)
        self.assertIn("activities", changed["next_commands"][0]["argv"])
        old = self.cli("desktop", "key", "--observation-id", frame["observation_id"], "--key", "enter", context=self.worker, exit_code=1)
        self.assertEqual(old["code"], "ACTIVITY_DIRECTION_CHANGED")
        self.assertEqual(len(self.backend.inputs), 2)
        fresh = self.broker.issue_context({**self.worker_metadata, "activity_revision": 1}, self.root / "fresh")
        frame = self.cli("desktop", "screenshot", context=fresh)
        self.cli("sandbox", "note", "--kind", "observation", "--text", "访谈解释了主题旋律。", "--evidence", frame["observation_id"], context=fresh)
        self.cli("sandbox", "finish", "--text", "指定的一篇访谈已读完并记录。", context=fresh)
        self.assertEqual(self.runtime.control(self.metadata, self.sid)["action"], "finish")
        self.assertFalse(list(self.root.rglob("params.json")))

    def test_workspace_grants_booleans_multiline_files_and_pause_have_direct_flags(self):
        self.start()
        self.assertTrue(self.cli("sandbox", "autoplay", "--target-id", self.target, "--enabled", "true")["enabled"])
        self.assertFalse(self.cli("sandbox", "autoplay", "--target-id", self.target, "--enabled", "false")["enabled"])
        grant = self.cli("sandbox", "grant", "--path", str(self.material), "--writable", "false")
        self.assertFalse(grant["writable"])
        text = "第一行 '引号'\n第二行 ` 与 $(文字)\n"
        source = self.root / "内容 UTF16.txt"
        source.write_text(text, encoding="utf-16")
        self.cli("sandbox", "write", "--path", "笔记.txt", "--content-file", str(source), context=self.worker)
        self.assertEqual(self.cli("sandbox", "read", "--path", "笔记.txt", context=self.worker)["text"], text)
        self.assertEqual(self.cli("sandbox", "list", context=self.worker)["files"][0]["name"], "笔记.txt")
        self.cli("sandbox", "pause", "--text-file", str(source), context=self.worker)
        self.assertEqual(self.runtime.control(self.metadata, self.sid)["text"], text.strip())

    def test_tasks_are_filtered_by_sender_scope_account_and_bound_request(self):
        self.start()
        own = self.cli("submit", "--task", "自己的第二项工作")
        for name, changed in (("sender", {"sender_id": "other"}), ("scope", {"scope": "other"}), ("account", {"namespace": "other"})):
            context = self.broker.issue_context({**self.metadata, **changed}, self.root / name)
            self.cli("submit", "--task", "不能出现在自己的列表中", context=context)
        tasks = self.cli("tasks")["tasks"]
        self.assertEqual({item["id"] for item in tasks}, {self.request_id, own["id"]})
        self.assertEqual(self.cli("tasks", "--state", "running")["tasks"][0]["id"], self.request_id)
        self.assertEqual([item["id"] for item in self.cli("tasks", context=self.worker)["tasks"]], [self.request_id])
        self.assertEqual(self.cli("status", context=self.worker)["id"], self.request_id)
        self.assertFalse(self.cli("status", "--request-id", own["id"], context=self.worker, exit_code=1)["ok"])
        controller = self.broker.issue_context({**self.worker_metadata, "activity_controller": True, "activity_target_id": self.target}, self.root / "controller")
        capabilities = self.cli("context", context=controller)["capabilities"]
        self.assertNotIn("desktop", capabilities)
        self.assertNotIn("grant", capabilities["sandbox"])
        self.assertEqual(self.cli("status", context=controller)["id"], self.request_id)

    def test_task_file_legacy_operation_and_params_files_and_file_queues_remain_usable(self):
        task = self.root / "要求.txt"
        task.write_text("完整要求\n保留换行与中文。", encoding="utf-16")
        request = self.cli("submit", "--type", "command", "--task-file", str(task))
        self.assertEqual(self.broker.record(request["id"])["task"], task.read_text(encoding="utf-16"))
        params = self.root / "兼容参数.json"
        params.write_text(json.dumps({"executable": str(self.exe), "text": "继续阅读。"}), encoding="utf-8-sig")
        used = self.cli("sandbox", "--operation", "use", "--params-file", str(params))
        self.assertEqual(used["state"], "submitted")
        path = self.output / "报告 中文.txt"
        path.write_text("已完成的成果", encoding="utf-8")
        listing = self.root / "files.json"
        listing.write_text(json.dumps({"files": [{"path": str(path), "name": "阅读报告.txt"}]}), encoding="utf-16")
        sent = self.cli("send", "--files-from", str(listing))
        self.assertEqual(self.cli("status", "--request-id", sent["id"])["state"], "submitted")
        self.broker.record_files("account", "group", "upload", "owner", [{"native_id": "qq-file", "name": "原文.txt", "size": 10}])
        incoming = self.cli("inbox")["files"][0]["file_id"]
        received = self.cli("receive", "--file-id", incoming)
        self.assertEqual(self.cli("status", "--request-id", received["id"])["kind"], "receive_file")
        self.assertEqual(self.cli("cancel", "--request-id", request["id"])["state"], "cancel_requested")

    def test_invalid_or_ignored_arguments_are_actionable_json_without_side_effects(self):
        self.start()
        before = len(self.cli("tasks")["tasks"])
        for args in (("sandbox", "note", "--text", "缺截图依据"), ("sandbox", "autoplay", "--target-id", self.target),
                ("sandbox", "apps", "--text", "此前会被忽略的字段"), ("desktop", "scroll", "--delta", "nan", "--observation-id", "old"),
                ("sandbox", "use", "--enabled", "false"), ("sandbox", "use", "--executable", str(self.exe), "--target-id", self.target),
                ("sandbox", "steer", "--operation", "stop", "--text", "冲突"), ("sandbox", "grant", "--path", str(self.material), "--writable", "maybe")):
            error = self.cli(*args, exit_code=2)
            self.assertEqual(error["code"], "INVALID_ARGUMENT")
            self.assertIn("help", error["hint"])
        params = self.root / "不是对象.json"
        params.write_text("[]", encoding="utf-8")
        self.assertEqual(self.cli("sandbox", "apps", "--params-file", str(params), exit_code=2)["code"], "INVALID_ARGUMENT")
        self.assertEqual(len(self.cli("tasks")["tasks"]), before)
        self.assertFalse(self.backend.inputs)

    def test_output_files_include_failure_receipts_and_output_errors_preserve_queued_task(self):
        target = self.root / "回执.json"
        target.write_text('{"ok":true,"state":"旧结果"}', encoding="utf-8")
        error = self.cli("sandbox", "note", "--text", "缺少依据", "--output", str(target), exit_code=2)
        self.assertEqual(error["code"], "INVALID_ARGUMENT")
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["code"], "INVALID_ARGUMENT")
        failure = self.cli("submit", "--task", "已提交，不得因输出文件错误而重做。", "--output", str(self.output), exit_code=1)
        self.assertEqual(failure["code"], "OUTPUT_WRITE_FAILED")
        queued = failure["operation_result"]
        self.assertTrue(queued["ok"])
        self.assertEqual(self.broker.record(queued["id"])["state"], "submitted")
        self.assertEqual(len(self.cli("tasks")["tasks"]), 1)

    def test_help_and_absolute_entry_point_work_from_an_unrelated_child_process_directory(self):
        cli = str(Path(__file__).resolve().parent / "console_tools/cli.py")
        for args, expected in ((["--help"], 0), (["help", "sandbox", "note"], 0), (["desktop", "click", "--help"], 0),
                               (["sandbox", "--operation", "steer", "--help"], 0), (["unknown"], 2)):
            child = subprocess.run([sys.executable, cli, *args], cwd=self.root, capture_output=True, encoding="utf-8", timeout=20)
            result = json.loads(child.stdout)
            self.assertEqual(child.returncode, expected, child.stderr)
            self.assertNotIn("Traceback", child.stderr)
            if args == ["help", "sandbox", "note"]:
                self.assertEqual(result["input_schema"]["required"], ["text", "evidence"])
                self.assertEqual(result["flags"]["evidence"], "--evidence")
                self.assertIn("--session-id", result["example"]["argv"])
        child = subprocess.run([sys.executable, "-m", "console_tools", "help", "sandbox", "autoplay"],
            cwd=Path(__file__).resolve().parent, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(json.loads(child.stdout)["input_schema"]["properties"]["enabled"]["type"], "boolean")

    @unittest.skipUnless(os.name == "nt", "PowerShell invocation is a Windows public interface")
    def test_generated_powershell_command_preserves_quotes_unicode_and_literal_shell_text(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(shell)
        task = "阅读 '配乐'；保留 $(1+1) 和 ` 字符，不执行文本。"
        example = invocation("submit", context_file=self.owner, task=task)
        child = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", example["powershell"]],
            cwd=self.root, capture_output=True, encoding="utf-8", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(child.returncode, 0, child.stderr)
        receipt = json.loads(child.stdout)
        self.assertEqual(self.broker.record(receipt["id"])["task"], task)

    def test_mcp_discovers_the_same_parameters_and_uses_the_same_queue(self):
        help_result = handle({"id": 1, "method": "tools/call", "params": {"name": "qqbot_console_help",
            "arguments": {"command": "sandbox", "operation": "note"}}})["result"]
        self.assertFalse(help_result["isError"])
        contract = json.loads(help_result["content"][0]["text"])
        self.assertEqual(contract["input_schema"], self.cli("help", "sandbox", "note")["input_schema"])
        task = self.cli("submit", "--task", "查找资料。")
        response = handle({"id": 2, "method": "tools/call", "params": {"name": "qqbot_list_tasks", "arguments": {"context_file": self.owner}}})["result"]
        self.assertFalse(response["isError"])
        self.assertEqual(json.loads(response["content"][0]["text"])["tasks"][0]["id"], task["id"])


if __name__ == "__main__":
    unittest.main()
