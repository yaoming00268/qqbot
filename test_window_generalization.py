"""Window lifecycle regressions across helper windows, renderers and dialogs."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image, ImageDraw

from activity_readiness import wait_for_observation
from background_window import BackgroundWindow, BackgroundWindowError, Win32Background
from window_selection import choose_window


def window(hwnd=22, **values):
    return {"hwnd": hwnd, "pid": 17, "process_started": 100, "executable": "Game.exe", "title": "Content",
            "size": [640, 480], "rect": [0, 0, 640, 480], "class_name": "UnfamiliarEngineWindow",
            "visible": True, "enabled": True, "minimized": False, "owner_hwnd": 0,
            "style": 0x00CF0000, "ex_style": 0, **values}


class SelectingApi:
    def __init__(self, windows):
        self.rows = windows
        self.restored = []
        self.posts = []
        self.u = SimpleNamespace(IsIconic=lambda hwnd: next(row for row in self.rows if row["hwnd"] == hwnd)["minimized"])

    def windows(self, root, *, include_hidden=False):
        return [row for row in self.rows if include_hidden or row["visible"]]

    def restore_window(self, row):
        self.restored.append(row["hwnd"])
        row.update(visible=True, minimized=False)
        if "normal_rect" in row:
            left, top, right, bottom = row["normal_rect"]
            row.update(size=[right - left, bottom - top], rect=row["normal_rect"])

    def capture(self, row):
        picture = Image.new("RGB", row["size"], "navy")
        ImageDraw.Draw(picture).rectangle((10, 10, 100, 100), fill="orange")
        return picture


class WindowSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = {"id": "test", "executable": str(self.root / "Game.exe")}
        self.backend = BackgroundWindow(self.root / "windows")
        self.desktop = self.backend.desktop_name(self.target)

    def observe(self, api):
        with patch("background_window.time.sleep"):
            return self.backend._observe(api, self.target, self.desktop, self.root / "frame.png")

    def test_zero_size_helper_never_wins_over_visible_content_regardless_of_titles(self):
        helper = window(1, size=[0, 0], rect=[1280, 720, 1280, 720], title="Game Title", class_name="FrameworkApp")
        renderer = window(2, title="", class_name="CustomRenderer")
        for rows in ([helper, renderer], [renderer, helper]):
            self.assertEqual(choose_window(rows)[0]["hwnd"], 2)

    def test_hidden_renderer_restores_without_restarting_or_using_helper(self):
        api = SelectingApi([window(1, size=[0, 0], class_name="FrameworkApp"),
                            window(2, visible=False, size=[2560, 1440], rect=[0, 0, 2560, 1440]),
                            window(3, visible=False, enabled=False, class_name="CiceroUIWndFrame", size=[200, 200])])
        frame = self.observe(api)
        self.assertEqual(frame["hwnd"], 2)
        self.assertEqual(frame["window_recovery"], "shown_without_activation")
        self.assertEqual(api.restored, [2])
        self.assertFalse(api.posts)

    def test_minimized_content_is_restored_during_observation(self):
        api = SelectingApi([window(minimized=True)])
        self.assertEqual(self.observe(api)["hwnd"], 22)
        self.assertEqual(api.restored, [22])

    def test_zero_client_minimized_window_uses_normal_placement_to_restore(self):
        api = SelectingApi([window(1, size=[0, 0]), window(2, minimized=True, size=[0, 0],
                            rect=[-32000, -32000, -31840, -31970], normal_rect=[50, 50, 690, 530])])
        frame = self.observe(api)
        self.assertEqual(frame["hwnd"], 2)
        self.assertEqual(frame["size"], [640, 480])
        self.assertEqual(api.restored, [2])

    def test_invalid_placement_or_disabled_minimized_helpers_are_not_restored(self):
        api = SelectingApi([window(1, minimized=True, size=[0, 0], normal_rect=[0, 0, 0, 0]),
                            window(2, minimized=True, size=[0, 0], enabled=False, normal_rect=[0, 0, 640, 480])])
        with self.assertRaises(BackgroundWindowError):
            self.observe(api)
        self.assertEqual(api.restored, [])

    def test_hidden_tools_disabled_forms_and_zero_size_windows_are_never_shown(self):
        api = SelectingApi([window(1, visible=False, ex_style=0x80), window(2, size=[0, 0]),
                            window(3, visible=False, enabled=False), window(4, visible=False, style=0)])
        with self.assertRaises(BackgroundWindowError) as caught:
            self.observe(api)
        self.assertEqual(caught.exception.code, "BACKGROUND_WINDOW_NOT_READY")
        self.assertTrue(caught.exception.details["windows"])
        self.assertFalse(api.restored)

    def test_owner_modal_selected_but_two_independent_documents_remain_ambiguous(self):
        main = window(1, enabled=False)
        dialog = window(2, size=[320, 160], owner_hwnd=1, class_name="#32770")
        self.assertEqual(choose_window([main, dialog]), (dialog, "modal"))
        main["enabled"] = True
        dialog["owner_hwnd"] = 0
        self.assertEqual(choose_window([main, dialog]), (None, "ambiguous"))
        api = SelectingApi([main, dialog])
        with self.assertRaises(BackgroundWindowError) as caught:
            self.observe(api)
        self.assertEqual(caught.exception.code, "BACKGROUND_WINDOW_AMBIGUOUS")
        self.assertFalse(caught.exception.retryable)

    def test_multiple_hidden_content_windows_are_not_restored_by_largest_area(self):
        api = SelectingApi([window(1, visible=False), window(2, visible=False, size=[1920, 1080])])
        with self.assertRaises(BackgroundWindowError) as caught:
            self.observe(api)
        self.assertEqual(caught.exception.code, "BACKGROUND_WINDOW_AMBIGUOUS")
        self.assertEqual(api.restored, [])

    def test_alive_helper_or_hidden_windows_attach_without_duplicate_launch(self):
        for row in (window(size=[0, 0]), window(visible=False)):
            api = SelectingApi([row])
            receipt = self.backend._launch(api, self.target, self.desktop)
            self.assertTrue(receipt["attached"])
            self.assertFalse(receipt["window_ready"])
            self.assertEqual(receipt["state"], "process_running")

    def test_exited_launcher_with_pending_child_does_not_launch_again(self):
        api = SelectingApi([])
        api.process = Mock(return_value=None)
        import time
        state = self.backend.root / (self.desktop + ".json")
        state.write_text(json.dumps({"pid": 17, "process": {"pid": 17}, "started": time.time(), "window_ready": False}))
        self.assertTrue(self.backend._launch(api, self.target, self.desktop)["pending"])

    def test_successful_frame_records_readiness_and_keeps_input_identity_checks(self):
        state = self.backend.root / (self.desktop + ".json")
        state.write_text(json.dumps({"pid": 17, "started": 1, "window_ready": False}))
        api = SelectingApi([window()])
        frame = self.observe(api)
        self.assertTrue(json.loads(state.read_text())["window_ready"])
        changed = {**frame, "hwnd": 99}
        with self.assertRaises(BackgroundWindowError) as caught:
            self.backend._check(api.rows[0], changed)
        self.assertIs(caught.exception.input_posted, False)

    def test_damaged_launch_cache_does_not_discard_a_valid_observation(self):
        state = self.backend.root / (self.desktop + ".json")
        state.write_text('{"pid":')
        with self.assertLogs("background_window", level="WARNING"):
            frame = self.observe(SelectingApi([window()]))
        self.assertTrue(Path(self.root / "frame.png").is_file())
        self.assertEqual(frame["hwnd"], 22)


class CaptureFallbackTests(unittest.TestCase):
    def setUp(self):
        self.api = Win32Background.__new__(Win32Background)
        self.api.u = SimpleNamespace(SendMessageTimeoutW=Mock(return_value=True))

    @staticmethod
    def pixels():
        picture = Image.new("RGB", (640, 480), "navy")
        ImageDraw.Draw(picture).rectangle((10, 10, 100, 100), fill="orange")
        return picture

    def test_blank_primary_uses_other_capture_method_and_returns_its_diagnostics(self):
        self.api._capture_method = Mock(side_effect=[Image.new("RGB", (640, 480)), self.pixels()])
        picture = self.api.capture(window())
        self.assertEqual(picture.info["capture_method"], "printwindow_client")
        self.assertEqual([item["state"] for item in picture.info["capture_attempts"]], ["blank", "captured"])
        self.assertEqual(picture.size, (640, 480))

    def test_known_working_method_is_tried_first_then_falls_back_if_it_stops_working(self):
        self.api.capture_preference = "printwindow_full"
        self.api._capture_method = Mock(side_effect=[BackgroundWindowError("BACKGROUND_CAPTURE_FAILED", "no paint"), self.pixels()])
        self.api.capture(window())
        self.assertEqual([call.args[1] for call in self.api._capture_method.call_args_list],
                         ["printwindow_full", "printwindow_client_full"])

    def test_all_capture_failures_include_each_attempt_without_claiming_valid_pixels(self):
        self.api._capture_method = Mock(return_value=Image.new("RGB", (640, 480)))
        with self.assertRaises(BackgroundWindowError) as caught:
            self.api.capture(window())
        self.assertEqual(caught.exception.code, "BACKGROUND_CAPTURE_BLANK")
        self.assertEqual(len(caught.exception.details["capture_attempts"]), 3)
        self.assertTrue(caught.exception.retryable)

    def test_resize_during_capture_keeps_transient_error_instead_of_claiming_unsupported(self):
        self.api._capture_method = Mock(side_effect=BackgroundWindowError("BACKGROUND_WINDOW_CHANGED", "resized"))
        with self.assertRaises(BackgroundWindowError) as caught:
            self.api.capture(window())
        self.assertEqual(caught.exception.code, "BACKGROUND_WINDOW_CHANGED")
        self.assertTrue(caught.exception.retryable)
        self.api._capture_method.assert_called_once()


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = 0
        self.runtime = SimpleNamespace(observe=Mock(), launch=Mock(), act=Mock())
        self.guard = Mock()
        self.progress = []

    async def wait(self, *, guard=None, timeout=2):
        async def advance(seconds):
            self.clock += seconds
        with patch("activity_readiness.time", SimpleNamespace(monotonic=lambda: self.clock)), \
             patch("activity_readiness.asyncio", SimpleNamespace(sleep=advance)), \
             patch("activity_readiness.finish_thread", new=AsyncMock(side_effect=lambda method, *args: method(*args))):
            return await wait_for_observation(self.runtime, {}, "session", check_current=guard or self.guard,
                                              timeout=timeout, progress=self.progress.append)

    async def test_waits_through_transient_startup_only_then_returns_actual_frame(self):
        frame = {"id": "real", "pid": 18, "capture_method": "printwindow_client"}
        self.runtime.observe.side_effect = [BackgroundWindowError("BACKGROUND_WINDOW_NOT_READY", "loading"),
                                           BackgroundWindowError("BACKGROUND_CAPTURE_BLANK", "pending paint"), frame]
        self.assertEqual(await self.wait(), frame)
        self.assertEqual([item["state"] for item in self.progress], ["waiting_window", "waiting_window", "ready"])
        self.assertTrue(self.progress[-1]["window_ready"])
        self.runtime.launch.assert_not_called()
        self.runtime.act.assert_not_called()

    async def test_permanent_failure_does_not_wait_or_retry(self):
        self.runtime.observe.side_effect = BackgroundWindowError("BACKGROUND_WINDOW_AMBIGUOUS", "two documents")
        with self.assertRaises(BackgroundWindowError):
            await self.wait()
        self.runtime.observe.assert_called_once()
        self.assertEqual(self.clock, 0)

    async def test_elapsed_deadline_bounds_wait_and_preserves_specific_failure(self):
        error = BackgroundWindowError("BACKGROUND_WINDOW_LOADING", "still loading")
        self.runtime.observe.side_effect = error
        with self.assertRaises(BackgroundWindowError) as caught:
            await self.wait(timeout=1)
        self.assertIs(caught.exception, error)
        self.assertLessEqual(self.clock, 1)
        self.assertGreater(self.runtime.observe.call_count, 1)
        self.runtime.launch.assert_not_called()

    async def test_cancel_during_startup_prevents_more_observations_and_all_input(self):
        self.runtime.observe.side_effect = BackgroundWindowError("BACKGROUND_WINDOW_LOADING", "loading")
        guard = Mock(side_effect=[None, asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await self.wait(guard=guard)
        self.runtime.observe.assert_called_once()
        self.runtime.act.assert_not_called()


class WindowTransitionTests(unittest.TestCase):
    def setUp(self):
        from test_desktop_control import DesktopControlTests
        self.fixture = DesktopControlTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_startup_progress_replaces_old_error_and_never_revives_cancelled_request(self):
        broker, identity = self.fixture.broker, self.fixture.request_id
        self.assertTrue(broker.update_running_result(identity,
            startup={"state": "waiting_window", "code": "BACKGROUND_WINDOW_LOADING"}))
        self.assertTrue(broker.update_running_result(identity, startup={"state": "ready", "window_ready": True},
            launch={"pid": 77}))
        payload = json.loads(broker.record(identity)["result"])
        self.assertEqual(payload["target_id"], self.fixture.target["id"])
        self.assertNotIn("code", payload["startup"])
        broker.update(identity, "cancel_requested", {**payload, "goal": "new direction"})
        self.assertFalse(broker.update_running_result(identity, startup={"state": "waiting_window"}))
        self.assertEqual(broker.record(identity)["state"], "cancel_requested")
        self.assertEqual(json.loads(broker.record(identity)["result"]), {**payload, "goal": "new direction"})

    def test_posted_action_can_transition_to_an_authorized_child_window_without_replay(self):
        from console_tools.cli import execute
        before = self.fixture.screenshot()
        original = self.fixture.backend.observe
        def observe(*args):
            return {**original(*args), "pid": 78, "hwnd": 12}
        self.fixture.backend.observe = observe
        params = {"observation_id": before["observation_id"], "key": "enter", "wait_ms": 0}
        result = execute("desktop", self.fixture.work, operation="key", params=params)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pid"], 78)
        self.assertTrue(result["window_changed"])
        repeated = execute("desktop", self.fixture.work, operation="key", params=params)
        self.assertEqual(result["observation_id"], repeated["observation_id"])
        self.assertEqual(len(self.fixture.backend.inputs), 1)

    def test_preflight_window_change_is_rejected_with_no_uncertain_input_claim(self):
        from console_tools.cli import execute
        frame = self.fixture.screenshot()
        self.fixture.backend.act = Mock(side_effect=BackgroundWindowError("BACKGROUND_WINDOW_CHANGED", "changed", input_posted=False))
        result = execute("desktop", self.fixture.work, operation="key",
                         params={"observation_id": frame["observation_id"], "key": "enter", "wait_ms": 0})
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "rejected")
        self.assertIs(result["input_posted"], False)
        self.assertFalse(self.fixture.backend.inputs)

    def test_explicit_pid_is_checked_again_for_cached_post_action_frame(self):
        from console_tools.cli import execute
        before = self.fixture.screenshot()
        original = self.fixture.backend.observe
        self.fixture.backend.observe = lambda *args: {**original(*args), "pid": 78, "hwnd": 12}
        params = {"observation_id": before["observation_id"], "key": "enter", "pid": 77, "wait_ms": 0}
        for _ in range(2):
            result = execute("desktop", self.fixture.work, operation="key", params=params)
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["code"], "DESKTOP_PROCESS_CHANGED")
            self.assertEqual(result["state"], "posted")
            self.assertIs(result["input_posted"], True)
        self.assertEqual(len(self.fixture.backend.inputs), 1)
        self.assertEqual(self.fixture.backend.captures, 2)


if __name__ == "__main__":
    unittest.main()
