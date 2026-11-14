"""Browser input must target a real editable control and report definite rejection."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from browser_control import BrowserBridge, BrowserError, installed_browser
from console_tools.server import handle
from test_desktop_control import FrameBackend


class BrowserFocusTests(unittest.TestCase):
    def bridge(self):
        bridge = BrowserBridge({"profile_path": "unused-profile"})
        page = {"id": "page", "url": "https://example.com/"}
        observed = {"browser_page": "page", "page_url": page["url"], "browser_viewport": [800, 600]}
        return bridge, page, observed

    def test_unfocused_input_is_rejected_before_text_is_sent(self):
        bridge, page, observed = self.bridge()
        with patch.object(bridge, "page", return_value=page), patch.object(bridge, "connection"), \
                patch.object(bridge, "input_state", return_value={"focus": {"editable": False}}), \
                patch.object(bridge, "command") as command:
            with self.assertRaises(BrowserError) as caught:
                bridge.act(observed, "type", text="中文")
            self.assertEqual(caught.exception.code, "BROWSER_INPUT_NOT_FOCUSED")
            self.assertIs(caught.exception.input_posted, False)
            command.assert_not_called()

    def test_failure_after_insert_text_remains_uncertain(self):
        bridge, page, observed = self.bridge()
        with patch.object(bridge, "page", return_value=page), patch.object(bridge, "connection"), \
                patch.object(bridge, "input_state", return_value={"focus": {"editable": True}}), \
                patch.object(bridge, "command", side_effect=BrowserError("BROWSER_TIMEOUT", "No acknowledgement")):
            with self.assertRaises(BrowserError) as caught:
                bridge.act(observed, "type", text="中文")
            self.assertIsNone(caught.exception.input_posted)

    def test_field_coordinates_use_screenshot_pixels_and_visual_viewport_offset(self):
        state = {"focus": {"editable": False}, "fields": [
            {"label": "搜索", "editable": True, "bounds": [110, 60, 100, 30]}]}
        metadata = BrowserBridge.input_metadata(state, (1600, 1200),
            {"clientWidth": 800, "clientHeight": 600, "offsetX": 10, "offsetY": 10})
        self.assertEqual(metadata["input_fields"][0]["bounds"], [200, 100, 200, 60])
        self.assertEqual(metadata["input_fields"][0]["center"], [300, 130])


class FocusBackend(FrameBackend):
    def __init__(self):
        super().__init__()
        self.focused = False
        self.attempts = 0

    def observe(self, target, path):
        frame = super().observe(target, path)
        frame.update(input_focus={"editable": self.focused},
                     input_fields=[{"label": "搜索", "editable": True, "center": [200, 80]}])
        return frame

    def act(self, target, observed, action, **params):
        self.attempts += 1
        if action == "type" and not self.focused:
            raise BrowserError("BROWSER_INPUT_NOT_FOCUSED", "请先点击输入框。", input_posted=False)
        result = super().act(target, observed, action, **params)
        if action == "click":
            self.focused = True
        return result


class FocusReceiptTests(unittest.TestCase):
    def setUp(self):
        import test_software_activity
        self.fixture = test_software_activity.SoftwareControlTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend = FocusBackend()
        self.fixture.patcher.stop()
        self.patcher = patch("sandbox_runtime.SandboxRuntime.device", return_value=self.backend)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def mcp(self, operation, **params):
        result = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "qqbot_desktop_" + operation,
            "arguments": {"context_file": self.fixture.worker, **params}}})["result"]
        return json.loads(result["content"][0]["text"]), result

    def test_known_rejection_returns_new_focus_frame_and_does_not_replay(self):
        before, _ = self.mcp("screenshot")
        args = {"observation_id": before["observation_id"], "text": "搜索内容", "wait_ms": 0}
        rejected, response = self.mcp("type", **args)
        self.assertTrue(response["isError"])
        self.assertEqual(response["content"][1]["type"], "image")
        self.assertEqual(rejected["state"], "rejected")
        self.assertIs(rejected["input_posted"], False)
        self.assertEqual(rejected["code"], "BROWSER_INPUT_NOT_FOCUSED")
        self.assertNotEqual(before["observation_id"], rejected["observation_id"])
        self.assertFalse(rejected["input_focus"]["editable"])
        repeated, _ = self.mcp("type", **args)
        self.assertEqual(repeated["action_id"], rejected["action_id"])
        self.assertEqual(self.backend.attempts, 1)
        self.assertFalse(self.backend.inputs)
        focused, _ = self.mcp("click", observation_id=repeated["observation_id"], x=200, y=80, wait_ms=0)
        self.assertTrue(focused["input_focus"]["editable"])
        typed, response = self.mcp("type", observation_id=focused["observation_id"], text="搜索内容", wait_ms=0)
        self.assertFalse(response["isError"], typed)
        self.assertTrue(typed["input_posted"])
        self.assertEqual([action for action, _ in self.backend.inputs], ["click", "type"])
        with self.fixture.runtime.connect() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT state FROM actions ORDER BY rowid")],
                             ["rejected", "posted", "posted"])

    def test_failed_snapshot_does_not_turn_definite_rejection_into_uncertainty(self):
        before, _ = self.mcp("screenshot")
        self.backend.fail_capture = True
        args = {"observation_id": before["observation_id"], "text": "未输入", "wait_ms": 0}
        rejected, _ = self.mcp("type", **args)
        self.assertEqual(rejected["state"], "rejected")
        self.assertIs(rejected["input_posted"], False)
        self.assertNotIn("image_path", rejected)
        self.assertIn("screenshot", rejected["hint"])
        self.backend.fail_capture = False
        observed, _ = self.mcp("screenshot")
        self.assertIn("input_fields", observed)
        self.mcp("type", **args)
        self.assertEqual(self.backend.attempts, 1)


HTML = b"""<!doctype html><meta charset="utf-8"><title>Local input test</title>
<style>body{margin:0}#search{position:absolute;left:100px;top:220px;width:600px;height:44px;
box-sizing:border-box}#secret{position:absolute;left:100px;top:300px}</style>
<form id="form"><input id="search" type="search" placeholder="Search games"></form>
<input id="secret" type="password" value="DO_NOT_EMIT_SECRET">
<script>window.submits=0;
document.querySelector('#form').onsubmit=e=>{e.preventDefault();window.submits++};
document.addEventListener('keydown',e=>{if(e.ctrlKey&&e.key==='k'){
e.preventDefault();document.querySelector('#search').focus()}});</script>"""


class HeadlessBrowserInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            executable = installed_browser()
        except BrowserError as error:
            raise unittest.SkipTest(str(error))
        cls.temp = tempfile.TemporaryDirectory(prefix="qqbot_browser_input_", ignore_cleanup_errors=True)
        cls.addClassCleanup(cls.temp.cleanup)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(HTML)))
                self.end_headers()
                self.wfile.write(HTML)

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.close_server)
        profile = Path(cls.temp.name) / "profile"
        cls.process = subprocess.Popen([str(executable), "--headless=new", "--disable-gpu",
            "--disable-background-networking", "--disable-component-update", "--disable-sync", "--no-pings",
            "--no-first-run", "--no-default-browser-check", "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0", "--user-data-dir=" + str(profile),
            "--window-size=1000,700", "--force-device-scale-factor=1",
            "http://127.0.0.1:" + str(cls.server.server_port) + "/"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        cls.bridge = BrowserBridge({"profile_path": str(profile)})
        cls.addClassCleanup(cls.close_browser)
        from testing_sandbox import own_browser_service
        own_browser_service(cls.process, profile)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                if cls.bridge.pages():
                    return
            except BrowserError:
                pass
            time.sleep(.05)
        raise RuntimeError("Local headless browser did not start")

    @classmethod
    def close_server(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    @classmethod
    def close_browser(cls):
        try:
            page = cls.bridge.page()
            with cls.bridge.connection(page) as socket:
                cls.bridge.command(socket, 99, "Browser.close")
        except BrowserError:
            pass
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=3)

    def evaluate(self, expression):
        with self.bridge.connection(self.bridge.page()) as socket:
            result = self.bridge.command(socket, 10, "Runtime.evaluate",
                {"expression": expression, "returnByValue": True})
        return result["result"].get("value")

    def test_real_browser_click_unicode_selection_shortcut_and_enter(self):
        self.evaluate("document.querySelector('#search').readOnly=false;document.activeElement.blur()")
        picture, observed = self.bridge.capture()
        self.assertFalse(observed["input_focus"]["editable"])
        self.assertNotIn("DO_NOT_EMIT_SECRET", json.dumps(observed))
        with self.assertRaises(BrowserError) as caught:
            self.bridge.act(observed, "type", text="花咲 Work Spring 中文😀")
        self.assertIs(caught.exception.input_posted, False)
        # Reproduce the screenshot's misplaced click on empty space.
        self.bridge.act(observed, "click", x=300 / picture.width, y=370 / picture.height)
        picture, observed = self.bridge.capture()
        self.assertFalse(observed["input_focus"]["editable"])
        field = next(field for field in observed["input_fields"] if field["label"] == "Search games")
        x, y = field["center"]
        self.bridge.act(observed, "click", x=x / picture.width, y=y / picture.height)
        _, observed = self.bridge.capture()
        self.assertTrue(observed["input_focus"]["editable"])
        self.bridge.act(observed, "type", text="replace me")
        self.bridge.act(observed, "key", key="ctrl+a")
        self.bridge.act(observed, "type", text="花咲 Work Spring 中文😀")
        self.assertEqual(self.evaluate("document.querySelector('#search').value"), "花咲 Work Spring 中文😀")
        self.assertEqual(self.evaluate("window.submits"), 0)
        self.bridge.act(observed, "key", key="enter")
        self.assertEqual(self.evaluate("window.submits"), 1)
        self.evaluate("document.activeElement.blur()")
        self.bridge.act(observed, "key", key="ctrl+k")
        _, observed = self.bridge.capture()
        self.assertTrue(observed["input_focus"]["editable"])
        self.bridge.act(observed, "key", key="tab")
        self.assertEqual(self.evaluate("document.activeElement.id"), "secret")
        self.bridge.act(observed, "key", key="shift+tab")
        self.assertEqual(self.evaluate("document.activeElement.id"), "search")

    def test_real_readonly_field_is_rejected_without_changing_its_text(self):
        self.evaluate("document.querySelector('#search').value='unchanged';"
                      "document.querySelector('#search').readOnly=true;document.querySelector('#search').focus()")
        _, observed = self.bridge.capture()
        with self.assertRaises(BrowserError) as caught:
            self.bridge.act(observed, "type", text="wrong target")
        self.assertEqual(caught.exception.code, "BROWSER_INPUT_NOT_FOCUSED")
        self.assertIs(caught.exception.input_posted, False)
        self.assertEqual(self.evaluate("document.querySelector('#search').value"), "unchanged")


if __name__ == "__main__":
    unittest.main()
