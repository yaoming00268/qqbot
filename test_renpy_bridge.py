"""Exercise engine frame/event transport without host focus, network or game saves."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import ctypes
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from renpy_bridge import RenpyBridge, RenpyBridgeError, is_renpy, write_json
from renpy_bridge_agent import Adapter


class RenpyBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / "game folder" / "Game.exe"
        self.exe.parent.mkdir()
        self.exe.touch()
        self.target = {"id": "game", "executable": str(self.exe)}
        self.bridge = RenpyBridge(self.root / "control", self.target, "test-desktop")
        self.environment = self.bridge.prepare()
        self.config = self.bridge.config()
        self.window = {"pid": os.getpid(), "size": [320, 180]}
        picture = Image.new("RGB", (640, 360), "blue")
        picture.paste("orange", (320, 0, 640, 360))
        png = io.BytesIO()
        picture.save(png, "PNG")
        self.png = png.getvalue()
        events = []
        pygame = SimpleNamespace(event=SimpleNamespace(Event=lambda kind, **data: (kind, data), post=events.append),
            ACTIVEEVENT=1, KEYDOWN=2, KEYUP=3, MOUSEMOTION=4, MOUSEBUTTONDOWN=5, MOUSEBUTTONUP=6,
            K_LEFT=276, K_s=115)
        self.exports = SimpleNamespace(screenshot_to_bytes=Mock(return_value=self.png),
            get_physical_size=lambda: (320, 180),
            invoke_in_main_thread=Mock(side_effect=lambda fn, *args: fn(*args)))
        self.renpy = SimpleNamespace(game=SimpleNamespace(interface=SimpleNamespace(started=True)),
                                    exports=self.exports, pygame=pygame)
        self.events = events
        self.adapter = Adapter(self.renpy, self.config)
        write_json(Path(self.config["run"]) / "ready.json", {"pid": self.adapter.pid, "token": self.config["token"]})
        self.thread = threading.Thread(target=self.adapter.serve, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.adapter.stopped.set()
        self.thread.join(2)

    def test_overlay_uses_separate_search_path_and_leaves_game_untouched(self):
        self.assertFalse(is_renpy(self.exe))
        (self.exe.parent / "renpy").mkdir()
        (self.exe.parent / "renpy/__init__.py").touch()
        (self.exe.parent / "game").mkdir()
        self.assertTrue(is_renpy(self.exe))
        overlay = Path(self.environment["RENPY_SEARCHPATH"].split("::")[0])
        self.assertTrue(overlay.is_relative_to((self.root / "control").resolve()))
        self.assertTrue(Path(self.environment["QQBOT_RENPY_ADAPTER"]).is_file())
        self.assertFalse(list((self.exe.parent / "game").iterdir()))

    def test_engine_png_is_checked_scaled_and_main_thread_scheduled(self):
        picture = self.bridge.call(self.window, "capture")
        self.assertEqual(picture.size, (320, 180))
        self.assertEqual(picture.getpixel((10, 10)), (0, 0, 255))
        self.assertEqual(picture.getpixel((310, 10)), (255, 165, 0))
        self.exports.screenshot_to_bytes.assert_called_once_with(None)
        self.exports.invoke_in_main_thread.assert_called_once()
        self.assertFalse(list((Path(self.config["run"]) / "outbox").iterdir()))

    def test_key_and_click_are_only_engine_events_with_pixel_coordinates(self):
        self.bridge.call(self.window, "key", {"key": "left"})
        self.bridge.call(self.window, "click", {"x": .75, "y": .25, "button": "right"})
        self.assertEqual([kind for kind, _ in self.events], [1, 2, 3, 1, 4, 5, 6])
        self.assertEqual(self.events[1][1]["key"], 276)
        self.assertEqual(self.events[-2][1], {"pos": (240, 45), "button": 3})
        self.assertEqual(self.events[-1][1], {"pos": (240, 45), "button": 3})

    def test_keys_supply_repeat_flag_and_character_bindings_for_renpy(self):
        self.bridge.call(self.window, "key", {"key": "s"})
        down, up = self.events[-2:]
        self.assertEqual([event[1]["unicode"] for event in (down, up)], ["s", "s"])
        self.assertEqual([event[1]["repeat"] for event in (down, up)], [False, False])

    def test_wrong_process_cannot_capture_or_queue_input(self):
        with self.assertRaisesRegex(RenpyBridgeError, "未加载"):
            self.bridge.call({**self.window, "pid": self.window["pid"] + 1}, "key", {"key": "left"})
        self.assertFalse(self.events)

    def test_secret_expiry_paths_and_unlisted_actions_are_rejected(self):
        created = time.time()
        valid = {"id": "a" * 32, "pid": self.adapter.pid, "token": self.config["token"],
                 "operation": "capture", "params": {}, "created": created, "expires": created + 5}
        for changed in ({"id": "../escape"}, {"token": "wrong"}, {"created": 0}, {"operation": "shell"},
                        {"operation": "click", "params": {"x": float("nan"), "y": .5, "button": "left"}},
                        {"operation": "key", "params": {"key": "alt+f4"}},
                        {"params": ["invalid"]}, {"created": "not a time"}, {"expires": float("nan")}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.adapter.validate({**valid, **changed})

    def test_blank_or_wrong_aspect_frame_is_rejected(self):
        for size, color, code in (((640, 360), "black", "BACKGROUND_CAPTURE_BLANK"),
                                  ((640, 100), "blue", "RENPY_VIEWPORT_CHANGED")):
            picture = Image.new("RGB", size, color)
            png = io.BytesIO()
            picture.save(png, "PNG")
            self.exports.screenshot_to_bytes.return_value = png.getvalue()
            with self.subTest(code=code), self.assertRaises(RenpyBridgeError) as caught:
                self.bridge.call(self.window, "capture")
            self.assertEqual(caught.exception.code, code)

    def test_timeout_never_requeues_an_input(self):
        self.stop()
        with self.assertRaises(RenpyBridgeError) as caught:
            self.bridge.call(self.window, "key", {"key": "left"}, timeout=.05)
        self.assertEqual(caught.exception.code, "RENPY_BRIDGE_TIMEOUT")
        self.assertFalse(self.events)
        self.assertFalse(list((Path(self.config["run"]) / "inbox").iterdir()))

    def test_input_expiring_in_engine_queue_is_rejected_before_events(self):
        queued = []
        self.exports.invoke_in_main_thread.side_effect = lambda fn, *args: queued.append((fn, args))
        with self.assertRaises(RenpyBridgeError):
            self.bridge.call(self.window, "key", {"key": "left"}, timeout=.2)
        self.assertEqual(len(queued), 1)
        function, arguments = queued[0]
        function(*arguments)
        self.assertFalse(self.events)

    def test_malformed_mail_does_not_kill_adapter_or_submit_events(self):
        inbox = Path(self.config["run"]) / "inbox"
        for number, value in enumerate(([], {"id": "bad"}, None)):
            write_json(inbox / (str(number) + ".json"), value)
        self.bridge.call(self.window, "capture")
        self.assertTrue(self.thread.is_alive())
        self.assertFalse(self.events)

    def test_post_failure_still_releases_engine_key(self):
        post = self.renpy.pygame.event.post
        def fail_down(event):
            post(event)
            if event[0] == self.renpy.pygame.KEYDOWN:
                raise RuntimeError("input queue failed")
        self.renpy.pygame.event.post = fail_down
        with self.assertRaisesRegex(RenpyBridgeError, "input queue failed"):
            self.bridge.call(self.window, "key", {"key": "left"})
        self.assertEqual([event[0] for event in self.events], [1, 2, 3])

    def test_native_launch_passes_a_unicode_environment_block(self):
        from background_window import Win32Background, ProcessInfo
        api = Win32Background.__new__(Win32Background)
        def create(exe, command, psec, tsec, inherit, flags, env, cwd, startup, process):
            self.assertTrue(flags & 0x400)
            self.assertEqual(env.value, "QQBOT_RENPY_CONFIG=" + str(self.root / "游戏配置.json"))
            info = ctypes.cast(process, ctypes.POINTER(ProcessInfo)).contents
            info.process, info.thread, info.pid = 1, 2, 3
            return True
        api.k = SimpleNamespace(CreateProcessW=Mock(side_effect=create), CloseHandle=Mock())
        api.u = SimpleNamespace(WaitForInputIdle=Mock())
        self.assertEqual(api.create_process(self.exe, "test", {"QQBOT_RENPY_CONFIG": str(self.root / "游戏配置.json")}), 3)


if __name__ == "__main__":
    unittest.main()
