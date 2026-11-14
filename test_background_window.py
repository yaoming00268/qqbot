"""Background input must stay inside the observed game's client area."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
from contextlib import contextmanager
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from background_window import BackgroundWindow, BackgroundWindowError


class FakeBackgroundApi:
    def __init__(self):
        self.row = {"pid": 17, "hwnd": 22, "process_started": 100, "executable": "Game.exe",
                    "title": "Test", "rect": [-1000, -400, -500, -50], "size": [480, 320]}
        self.posts = []
        self.points = []
        self.rows = [self.row]
        # Deliberately no global cursor, input or focus APIs.
        self.u = SimpleNamespace(IsIconic=lambda hwnd: False, MapVirtualKeyW=lambda code, mode: code)

    def windows(self, root, *, include_hidden=False):
        return self.rows

    def capture(self, window):
        return Image.new("RGB", window["size"], "blue")

    def input_window(self, window, root, point=None):
        if point is not None:
            self.points.append((point.x, point.y))
        return window["hwnd"], point

    def post(self, hwnd, message, wp, lp):
        self.posts.append((hwnd, message, wp, lp))


class BackgroundWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = {"id": "test-game", "executable": str(self.root / "Game.exe")}
        self.backend = BackgroundWindow(self.root / "windows")
        self.api = FakeBackgroundApi()
        self.desktop = self.backend.desktop_name(self.target)
        self.observation = {**self.api.row, "mode": "background", "desktop": self.desktop,
                            "target_id": self.target["id"], "coordinate_space": "client"}

    def act(self, action, **params):
        return self.backend._act(self.api, self.target, self.desktop, self.observation, action,
            **{"key": "", "x": None, "y": None, "button": "left", "guard": None, **params})

    def test_click_uses_client_pixels_even_with_negative_screen_origin(self):
        for button, down, up, state in (("left", 0x201, 0x202, 1), ("right", 0x204, 0x205, 2),
                                        ("middle", 0x207, 0x208, 16)):
            with self.subTest(button=button), patch("background_window.time.sleep"):
                self.api.posts.clear()
                result = self.act("click", x=.75, y=.25, button=button)
                lp = 360 | (80 << 16)
                self.assertEqual(self.api.points[-1], (360, 80))
                self.assertEqual(self.api.posts, [(22, 0x200, 0, lp), (22, down, state, lp), (22, up, 0, lp)])
                self.assertFalse(result["host_input"])
                self.assertFalse(result["verified"])

    def test_key_release_has_scan_extended_and_previous_state_bits(self):
        with patch("background_window.time.sleep"):
            self.act("key", key="left")
        lp = 1 | (0x25 << 16) | (1 << 24)
        self.assertEqual(self.api.posts, [(22, 0x100, 0x25, lp), (22, 0x101, 0x25, lp | (3 << 30))])

    def test_input_rejected_for_changed_identity_size_or_other_desktop(self):
        for field, value in (("hwnd", 23), ("pid", 18), ("process_started", 101), ("size", [400, 300]),
                             ("rect", [0, 0, 500, 350]), ("desktop", "other"), ("target_id", "other"),
                             ("coordinate_space", "screen"), ("mode", "qemu")):
            with self.subTest(field=field):
                original = self.observation[field]
                self.observation[field] = value
                with self.assertRaises(BackgroundWindowError):
                    self.act("key", key="enter")
                self.observation[field] = original
        self.assertEqual(self.api.posts, [])

    def test_cancel_prevents_input_and_interruption_still_releases_key(self):
        def cancel():
            raise ValueError("cancelled")
        with self.assertRaisesRegex(ValueError, "cancelled"):
            self.act("key", key="enter", guard=cancel)
        self.assertFalse(self.api.posts)
        with patch("background_window.time.sleep", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.act("key", key="enter")
        self.assertEqual([row[1] for row in self.api.posts], [0x100, 0x101])

    def test_missing_ambiguous_or_minimized_windows_do_not_get_input(self):
        for rows in ([], [self.api.row, {**self.api.row, "hwnd": 23}]):
            self.api.rows = rows
            with self.assertRaises(BackgroundWindowError):
                self.act("key", key="enter")
        self.api.rows = [self.api.row]
        self.api.u.IsIconic = lambda hwnd: True
        with self.assertRaises(BackgroundWindowError):
            self.act("key", key="enter")
        self.assertFalse(self.api.posts)

    def test_observation_is_actual_client_image_and_checks_window_after_capture(self):
        path = self.root / "observation.png"
        observed = self.backend._observe(self.api, self.target, self.desktop, path)
        with Image.open(path) as picture:
            self.assertEqual(list(picture.size), observed["size"])
        self.assertEqual(observed["coordinate_space"], "client")
        with patch.object(self.backend, "_locate", side_effect=[self.api.row, {**self.api.row, "hwnd": 23}]):
            with self.assertRaises(BackgroundWindowError):
                self.backend._observe(self.api, self.target, self.desktop, self.root / "changed.png")
        self.assertFalse((self.root / "changed.png").exists())

    def test_relaunch_attaches_without_starting_another_process(self):
        self.api.create_process = Mock()
        with patch("renpy_bridge.RenpyBridge.prepare") as prepare:
            receipt = self.backend._launch(self.api, self.target, self.desktop)
        prepare.assert_not_called()
        self.assertTrue(receipt["attached"])
        self.api.create_process.assert_not_called()

    def test_renpy_presplash_is_loading_and_never_an_observation_or_input_target(self):
        self.api.row["title"] = str(Path(self.target["executable"]).with_suffix(".py"))
        self.api.row["size"] = [667, 100]
        self.api.capture = Mock()
        path = self.root / "presplash.png"
        with patch("renpy_bridge.is_renpy", return_value=True):
            for operation in (lambda: self.backend._observe(self.api, self.target, self.desktop, path),
                              lambda: self.act("key", key="enter")):
                with self.assertRaises(BackgroundWindowError) as caught:
                    operation()
                self.assertEqual(caught.exception.code, "BACKGROUND_WINDOW_LOADING")
        self.api.capture.assert_not_called()
        self.assertFalse(path.exists())
        self.assertFalse(self.api.posts)

    def test_renpy_capture_and_actions_use_engine_only_and_keep_cancellation_guard(self):
        bridge = Mock()
        bridge.call.return_value = Image.new("RGB", self.api.row["size"], "blue")
        self.api.capture = Mock()
        guard = Mock()
        with patch.object(self.backend, "_bridge", return_value=bridge):
            self.observation = self.backend._observe(self.api, self.target, self.desktop, self.root / "engine.png")
            self.assertEqual(self.observation["capture"], "renpy")
            bridge.call.assert_called_once_with(self.api.row, "capture")
            receipt = self.act("click", x=.2, y=.3, guard=guard)
            self.assertEqual(receipt["input_transport"], "renpy_events")
            self.assertFalse(receipt["verified"])
            guard.assert_called_once()
            bridge.call.assert_called_with(self.api.row, "click", {"key": "", "x": .2, "y": .3, "button": "left"})
            bridge.call.reset_mock()
            guard.side_effect = ValueError("cancelled")
            with self.assertRaisesRegex(ValueError, "cancelled"):
                self.act("key", key="enter", guard=guard)
            bridge.call.assert_not_called()
        self.api.capture.assert_not_called()
        self.assertFalse(self.api.posts)

    def test_unadapted_renpy_blank_capture_requires_explicit_restart_and_sends_no_input(self):
        from renpy_bridge import RenpyBridgeError
        self.api.capture = Mock(side_effect=BackgroundWindowError("BACKGROUND_CAPTURE_BLANK", "blank"))
        self.api.create_process = Mock()
        path = self.root / "blank.png"
        with patch("renpy_bridge.is_renpy", return_value=True):
            with self.assertRaises(RenpyBridgeError) as caught:
                self.backend._observe(self.api, self.target, self.desktop, path)
        self.assertEqual(caught.exception.code, "RENPY_BRIDGE_RESTART_REQUIRED")
        self.api.create_process.assert_not_called()
        self.assertFalse(path.exists())
        self.assertFalse(self.api.posts)

    def test_new_renpy_launch_receives_overlay_environment(self):
        self.api.rows = []
        self.api.process = Mock(return_value=None)
        self.api.create_process = Mock(return_value=18)
        Path(self.target["executable"]).touch()
        (self.root / "renpy").mkdir()
        (self.root / "renpy/__init__.py").touch()
        (self.root / "game").mkdir()
        self.backend._launch(self.api, self.target, self.desktop)
        executable, desktop, environment = self.api.create_process.call_args.args
        self.assertEqual(executable, Path(self.target["executable"]).resolve())
        self.assertEqual(desktop, self.desktop)
        self.assertTrue(Path(environment["QQBOT_RENPY_ADAPTER"]).is_file())
        self.assertTrue(Path(environment["QQBOT_RENPY_CONFIG"]).is_file())
        self.assertFalse(list((self.root / "game").iterdir()))

    def test_window_operations_run_on_fresh_thread_inside_private_desktop(self):
        import threading
        main = threading.get_ident()
        entered = []
        @contextmanager
        def desktop(name):
            entered.append((threading.get_ident(), name))
            yield
            entered.append("closed")
        self.api.desktop = desktop
        with patch("background_window.Win32Background", return_value=self.api):
            result = self.backend._call(self.target, lambda api, target, desk: threading.get_ident())
        self.assertNotEqual(result, main)
        self.assertEqual(entered, [(result, self.desktop), "closed"])

    def test_mode_selects_backend(self):
        from config import SandboxConfig
        from game_window import GameWindow
        self.assertEqual(SandboxConfig().mode, "background")
        with patch("background_window.BackgroundWindow") as background, patch("game_window.QemuVM") as vm:
            GameWindow(directory=self.root).status()
            background.return_value.status.assert_called_once()
            vm.assert_not_called()
            GameWindow({"cpus": 3}, self.root, mode="qemu")
            vm.assert_called_once_with(self.root / "vm", {"cpus": 3})

    def test_launch_uses_native_desktop_structure_and_waits_for_child(self):
        import ctypes
        from background_window import ProcessInfo, StartupInfo, Win32Background
        api = Win32Background.__new__(Win32Background)
        startup_calls = []
        def create(exe, arguments, proc_sec, thread_sec, inherit, flags, env, cwd, startup, process):
            si = ctypes.cast(startup, ctypes.POINTER(StartupInfo)).contents
            startup_calls.append((exe, si.desktop, si.show, inherit, cwd))
            result = ctypes.cast(process, ctypes.POINTER(ProcessInfo)).contents
            result.pid, result.process, result.thread = 17, 100, 101
            return True
        api.k = SimpleNamespace(CreateProcessW=Mock(side_effect=create), CloseHandle=Mock())
        api.u = SimpleNamespace(WaitForInputIdle=Mock())
        executable = self.root / "game path" / "Game.exe"
        self.assertEqual(api.create_process(executable, self.desktop), 17)
        self.assertEqual(startup_calls, [(str(executable), "WinSta0\\" + self.desktop, 4, False, str(executable.parent))])
        api.u.WaitForInputIdle.assert_called_once_with(100, 5000)
        self.assertEqual([call.args[0] for call in api.k.CloseHandle.call_args_list], [101, 100])

    def test_exited_pending_launcher_can_start_again_after_grace_period(self):
        import json
        self.api.rows = []
        self.api.process = Mock(return_value=None)
        self.api.create_process = Mock(return_value=18)
        Path(self.target["executable"]).touch()
        state = self.backend.root / (self.desktop + ".json")
        state.write_text(json.dumps({"pid": 17, "process": None, "started": 0}))
        result = self.backend._launch(self.api, self.target, self.desktop)
        self.assertEqual(result["pid"], 18)
        self.assertFalse(result["attached"])
        self.api.create_process.assert_called_once()

    def test_fullscreen_input_indicators_are_excluded_but_game_dialogs_remain(self):
        from background_window import Win32Background
        api = Win32Background.__new__(Win32Background)
        classes = {22: "GameRenderer", 23: "#32770", 24: "UAC_InputIndicatorOverlayWnd", 25: "UAC Input Indicator"}
        def get_class(hwnd, output, size):
            output.value = classes[hwnd]
            return len(output.value)
        def enum_windows(desktop, callback, param):
            for hwnd in classes:
                self.assertTrue(callback(hwnd, param))
            return True
        api.enum_callback = lambda fn: fn
        api.desktop_handle = 10
        api.u = SimpleNamespace(IsWindowVisible=lambda hwnd: True, GetClassNameW=get_class,
                                EnumDesktopWindows=enum_windows)
        api.window = Mock(side_effect=lambda hwnd, root: {**self.api.row, "hwnd": hwnd})
        windows = api.windows(self.root)
        self.assertEqual([row["hwnd"] for row in windows], [22, 23])
        self.assertEqual([call.args[0] for call in api.window.call_args_list], [22, 23])
        with self.assertRaisesRegex(BackgroundWindowError, "唯一"):
            self.backend._locate(api, self.target)


if __name__ == "__main__":
    unittest.main()
