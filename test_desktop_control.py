"""Verify process-bound, one-input/one-image calls through public MCP and CLI."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from background_window import BackgroundWindowError
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute, main
from console_tools.server import handle, tools_schema
from sandbox_runtime import SandboxRuntime


class FrameBackend:
    def __init__(self):
        self.inputs = []
        self.captures = 0
        self.fail_capture = False
        self.fail_input = False
        self.on_input = None

    def observe(self, target, path):
        self.captures += 1
        if self.fail_capture:
            raise BackgroundWindowError("BACKGROUND_CAPTURE_BLANK", "画面为空。")
        image = Image.new("RGB", (400, 200), "navy")
        ImageDraw.Draw(image).rectangle((5, 5, 20 + len(self.inputs) * 20, 50), fill="orange")
        image.save(path, "PNG")
        return {"pid": 77, "title": "游戏标题", "size": [400, 200], "hwnd": 11, "rect": [0, 0, 400, 200]}

    def act(self, target, observed, action, **params):
        params.pop("guard")()
        self.inputs.append((action, params))
        if self.fail_input:
            raise BackgroundWindowError("BACKGROUND_INPUT_REJECTED", "输入未确认。")
        if self.on_input:
            self.on_input()
        return {"posted": True, "verified": False, "host_input": False}


class DesktopControlTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.backend = FrameBackend()
        self.device_patch = patch("sandbox_runtime.SandboxRuntime.device", return_value=self.backend)
        self.device_patch.start()
        self.addCleanup(self.device_patch.stop)
        exe = self.root / "game/Game.exe"
        exe.parent.mkdir()
        exe.touch()
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.metadata = {"namespace": "account", "scope": "group", "sender_id": "owner", "is_master": True,
                         "source_text": str(exe), "sandbox_config": {"enabled": True, "mode": "background",
                         "directory": str(self.root / "sandbox"), "max_actions": 10}}
        self.context = self.broker.issue_context(self.metadata, self.root / "main-context")
        request = execute("sandbox", self.context, operation="play", params={"executable": str(exe)})
        self.request_id = request["id"]
        self.runtime = SandboxRuntime(self.root / "sandbox")
        target_id = json.loads(self.broker.record(self.request_id)["result"])["target_id"]
        self.sid, self.target = self.runtime.begin(self.metadata, target_id, self.request_id)
        self.broker.update(self.request_id, "running", {"target_id": target_id, "sandbox_session_id": self.sid})
        self.work = self.broker.issue_context({**self.metadata, "sandbox_session_id": self.sid}, self.root / "work-context")

    def mcp(self, operation, **params):
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "qqbot_desktop_" + operation, "arguments": {"context_file": self.work, **params}}})["result"]
        value = json.loads(response["content"][0]["text"])
        return value, response

    def screenshot(self):
        value, response = self.mcp("screenshot")
        self.assertFalse(response["isError"], value)
        return value

    def test_tools_are_typed_and_discoverable_without_params_files(self):
        schemas = {item["name"]: item["inputSchema"] for item in tools_schema()}
        for operation in ("targets", "screenshot", "key", "click"):
            schema = schemas["qqbot_desktop_" + operation]
            self.assertNotIn("params", schema["properties"])
            self.assertNotIn("params_file", schema["properties"])
        self.assertIn("key", schemas["qqbot_desktop_key"]["required"])
        self.assertEqual(schemas["qqbot_desktop_click"]["properties"]["coordinate_space"]["default"], "pixels")

    def test_key_returns_new_image_in_same_call_and_compact_receipt(self):
        first = self.screenshot()
        result, response = self.mcp("key", observation_id=first["observation_id"], key="enter", pid=77, wait_ms=0)
        self.assertFalse(response["isError"], result)
        self.assertTrue(result["input_posted"])
        self.assertNotEqual(first["observation_id"], result["observation_id"])
        self.assertEqual(self.backend.captures, 2)
        self.assertEqual(len(self.backend.inputs), 1)
        self.assertEqual(result["title"], "游戏标题")
        self.assertNotIn("hwnd", result)
        self.assertNotIn("image_path", result)
        self.assertNotIn("sha256", result)
        self.assertNotIn("process_started", result)
        with self.runtime.connect() as db:
            stored = json.loads(db.execute("SELECT data FROM observations WHERE id=?", (result["observation_id"],)).fetchone()[0])
        self.assertEqual(stored["after_action"], result["action_id"])
        raw = base64.b64decode(response["content"][1]["data"])
        self.assertEqual(raw, Path(stored["image_path"]).read_bytes())
        with Image.open(io.BytesIO(raw)) as picture:
            self.assertEqual(picture.size, (400, 200))

    def test_pixels_are_converted_by_console_and_duplicate_call_reuses_result(self):
        first = self.screenshot()
        params = {"observation_id": first["observation_id"], "x": 300, "y": 50, "button": "right", "wait_ms": 0}
        one, _ = self.mcp("click", **params)
        two, _ = self.mcp("click", **params)
        self.assertEqual(one["action_id"], two["action_id"])
        self.assertEqual(one["observation_id"], two["observation_id"])
        self.assertEqual(len(self.backend.inputs), 1)
        self.assertEqual(self.backend.captures, 2)
        self.assertEqual(int(self.backend.inputs[0][1]["x"] * 400), 300)
        self.assertEqual(int(self.backend.inputs[0][1]["y"] * 200), 50)
        self.assertEqual(self.backend.inputs[0][1]["button"], "right")

    def test_pixel_centres_survive_normalization_at_edges_and_rounding_boundaries(self):
        current = self.screenshot()
        for x, y in ((0, 0), (7, 29), (29, 57), (57, 113), (399, 199)):
            current, response = self.mcp("click", observation_id=current["observation_id"], x=x, y=y, wait_ms=0)
            self.assertFalse(response["isError"], current)
            self.assertEqual(int(self.backend.inputs[-1][1]["x"] * 400), x)
            self.assertEqual(int(self.backend.inputs[-1][1]["y"] * 200), y)
        current, response = self.mcp("click", observation_id=current["observation_id"], x=.75, y=.25,
                                     coordinate_space="normalized", wait_ms=0)
        self.assertFalse(response["isError"], current)
        self.assertEqual(self.backend.inputs[-1][1]["x"], .75)
        self.assertEqual(self.backend.inputs[-1][1]["y"], .25)

    def test_bad_pid_coordinates_and_old_frame_never_input(self):
        first = self.screenshot()
        for operation, params in (("key", {"key": "enter", "pid": 78}),
                                  ("click", {"x": 400, "y": 50}), ("click", {"x": float("nan"), "y": 50}),
                                  ("key", {"key": "alt+f4"}), ("key", {"key": "enter", "wait_ms": 2001})):
            with self.subTest(params=params):
                _, response = self.mcp(operation, observation_id=first["observation_id"], **params)
                self.assertTrue(response["isError"])
        self.screenshot()
        error, response = self.mcp("key", observation_id=first["observation_id"], key="enter", wait_ms=0)
        self.assertTrue(response["isError"])
        self.assertEqual(error["code"], "DESKTOP_OBSERVATION_STALE")
        self.assertEqual(self.runtime.session(self.metadata, self.sid)["actions"], 0)
        self.assertFalse(self.backend.inputs)

    def test_capture_failure_after_input_reports_posted_and_retry_does_not_replay(self):
        first = self.screenshot()
        self.backend.fail_capture = True
        params = {"observation_id": first["observation_id"], "key": "enter", "wait_ms": 0}
        error, response = self.mcp("key", **params)
        self.assertTrue(response["isError"])
        self.assertEqual(error["code"], "BACKGROUND_CAPTURE_BLANK")
        self.assertEqual(error["state"], "posted")
        self.assertTrue(error["input_posted"])
        self.assertEqual(len(response["content"]), 1)
        self.backend.fail_capture = False
        result, response = self.mcp("key", **params)
        self.assertFalse(response["isError"], result)
        self.assertEqual(len(self.backend.inputs), 1)

    def test_unknown_input_never_replays_even_if_same_call_retried(self):
        first = self.screenshot()
        self.backend.fail_input = True
        params = {"observation_id": first["observation_id"], "key": "enter", "wait_ms": 0}
        for _ in range(2):
            error, response = self.mcp("key", **params)
            self.assertTrue(response["isError"])
            self.assertEqual(error["state"], "unknown")
            self.assertIsNone(error["input_posted"])
        self.assertEqual(len(self.backend.inputs), 1)

    def test_stop_after_input_does_not_return_frame_or_accept_another_input(self):
        first = self.screenshot()
        self.backend.on_input = lambda: self.broker.update(self.request_id, "cancel_requested")
        error, response = self.mcp("key", observation_id=first["observation_id"], key="enter", wait_ms=0)
        self.assertTrue(response["isError"])
        self.assertTrue(error["input_posted"])
        self.assertEqual(error["code"], "DESKTOP_STOPPED")
        self.assertEqual(self.backend.captures, 1)
        _, response = self.mcp("screenshot")
        self.assertTrue(response["isError"])
        self.assertEqual(len(self.backend.inputs), 1)

    def test_context_scope_process_selection_and_work_binding_stay_enforced(self):
        self.screenshot()
        targets, _ = self.mcp("targets")
        self.assertEqual(targets["targets"][0]["pid"], 77)
        for changed in ({"namespace": "other"}, {"scope": "other"}, {"sender_id": "other"}):
            context = self.broker.issue_context({**self.metadata, **changed}, self.root / "other-context")
            self.assertFalse(execute("desktop", context, operation="targets")["targets"])
            with self.assertRaises((ValueError, PermissionError)):
                execute("desktop", context, operation="screenshot", params={"session_id": self.sid})
        for params in ({"session_id": "other"}, {"target_id": "other"}):
            _, response = self.mcp("screenshot", **params)
            self.assertTrue(response["isError"])
        self.assertFalse(self.backend.inputs)

    def test_main_context_can_choose_one_of_two_authorized_processes_by_pid(self):
        self.screenshot()
        exe = self.root / "second/Game.exe"
        exe.parent.mkdir()
        exe.touch()
        other_context = {**self.metadata, "source_text": str(exe)}
        target = self.runtime.register(other_context, str(exe))
        sid, _ = self.runtime.begin(other_context, target["id"], self.request_id)
        original = self.backend.observe
        def second_process(target, path):
            return {**original(target, path), "pid": 88}
        with patch.object(self.backend, "observe", side_effect=second_process):
            self.runtime.observe(other_context, sid)
        result = execute("desktop", self.context, operation="screenshot", params={"pid": 77})
        self.assertEqual(result["session_id"], self.sid)
        with self.assertRaisesRegex(ValueError, "唯一"):
            execute("desktop", self.context, operation="screenshot")

    def test_missing_cached_image_is_recaptured_without_repeating_input(self):
        first = self.screenshot()
        params = {"observation_id": first["observation_id"], "key": "enter", "wait_ms": 0}
        posted, _ = self.mcp("key", **params)
        with self.runtime.connect() as db:
            raw = json.loads(db.execute("SELECT data FROM observations WHERE id=?", (posted["observation_id"],)).fetchone()[0])
        Path(raw["image_path"]).unlink()
        retried, response = self.mcp("key", **params)
        self.assertFalse(response["isError"])
        self.assertNotEqual(posted["observation_id"], retried["observation_id"])
        self.assertEqual(len(self.backend.inputs), 1)

    def test_expired_context_disabled_control_and_schema_bypass_are_rejected(self):
        _, token = ConsoleBroker.from_context(self.work)
        with self.broker.connect() as db:
            db.execute("UPDATE contexts SET expires=0 WHERE token=?", (token,))
        _, response = self.mcp("screenshot")
        self.assertTrue(response["isError"])
        for changed in ({"is_master": False}, {"sandbox_config": {"enabled": False}}):
            context = self.broker.issue_context({**self.metadata, **changed}, self.root / "restricted")
            with self.assertRaises((ValueError, PermissionError)):
                execute("desktop", context, operation="targets")
        response = handle({"id": 1, "method": "tools/call", "params": {"name": "qqbot_desktop_screenshot", "arguments": {
            "context_file": self.context, "operation": "shell", "command": "bad"}}})["result"]
        self.assertTrue(response["isError"])
        self.assertFalse(self.backend.inputs)

    def test_cli_takes_key_flags_prints_unicode_and_returns_image_without_parameter_file(self):
        output = io.StringIO()
        with redirect_stdout(output):
            main(["desktop", "--context-file", self.work, "--operation", "screenshot"])
        first = json.loads(output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            main(["desktop", "--context-file", self.work, "--operation", "key", "--key", "enter",
                  "--observation-id", first["observation_id"], "--pid", "77", "--wait-ms", "0"])
        raw = output.getvalue()
        after = json.loads(raw)
        self.assertIn("游戏标题", raw)
        self.assertTrue(Path(after["image_path"]).is_file())
        self.assertTrue(after["input_posted"])
        self.assertFalse(list(self.root.rglob("params.json")))


if __name__ == "__main__":
    unittest.main()
