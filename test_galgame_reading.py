"""Reading must save image turns without losing dialogue or replaying inputs."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw, ImageFont

from config import AppConfig, GalgameReadingConfig
from console_tools.cli import execute, main
from galgame_reading import local_ocr, read_frame
from renpy_bridge_agent import Adapter
import test_desktop_control as desktop_fixture


class StoryBackend:
    def __init__(self):
        self.inputs, self.captures, self.page = [], 0, 0
        self.pages = [{"text": f"这是第 {n} 句真实对白，仍然要逐句阅读。"} for n in range(30)]
        self.on_input, self.fail_input, self.fail_capture = None, False, False
        self.native, self.on_capture = True, None

    def observe(self, target, path):
        from background_window import BackgroundWindowError
        self.captures += 1
        if self.fail_capture:
            raise BackgroundWindowError("BACKGROUND_CAPTURE_BLANK", "取图失败")
        page = dict(self.pages[self.page])
        if self.on_capture:
            page.update(self.on_capture(self.captures))
        picture = Image.new("RGB", (400, 200), page.get("color", "navy"))
        draw = ImageDraw.Draw(picture)
        draw.rectangle((0, 125, 399, 199), fill="#222222")
        draw.text((20, 145), str(self.page), fill="white")
        picture.save(path)
        result = {"pid": 77, "title": "Story fixture", "size": [400, 200], "hwnd": 11, "rect": [0, 0, 400, 200]}
        if self.native:
            result["game_reading"] = {"source": "renpy", "kind": page.get("kind", "dialogue"), "text": page["text"]}
        return result

    def act(self, target, observation, action, **params):
        from background_window import BackgroundWindowError
        params.pop("guard")()
        self.inputs.append((action, params))
        if self.fail_input:
            raise BackgroundWindowError("INPUT_UNCERTAIN", "未确认按键")
        self.page = min(self.page + 1, len(self.pages) - 1)
        if self.on_input:
            self.on_input()
        return {"posted": True, "verified": False, "host_input": False}


class ReadingTests(unittest.TestCase):
    def setUp(self):
        desktop_fixture.DesktopControlTests.setUp(self)
        self.device_patch.stop()
        self.backend = StoryBackend()
        self.device_patch = patch("sandbox_runtime.SandboxRuntime.device", return_value=self.backend)
        self.device_patch.start()
        self.addCleanup(self.device_patch.stop)
        self.sleep_patch = patch("galgame_reading.time.sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    mcp = desktop_fixture.DesktopControlTests.mcp
    screenshot = desktop_fixture.DesktopControlTests.screenshot

    def configure(self, **reading):
        self.metadata["sandbox_config"]["galgame_reading"] = reading
        self.work = self.broker.issue_context({**self.metadata, "sandbox_session_id": self.sid}, self.root / "read-context")

    def test_eight_steps_return_all_nine_pages_with_no_image_blocks(self):
        first = self.screenshot()
        result, response = self.mcp("read", observation_id=first["observation_id"])
        self.assertFalse(response["isError"], result)
        self.assertEqual(result["advanced"], 8)
        self.assertEqual(result["stop_reason"], "batch_complete")
        self.assertEqual([item["text"] for item in result["dialogue"]], [p["text"] for p in self.backend.pages[:9]])
        self.assertFalse(result["image_required"])
        self.assertEqual(len(response["content"]), 1)
        self.assertEqual(len(self.backend.inputs), 8)
        for entry in result["dialogue"]:
            self.runtime.note(self.metadata, self.sid, entry["text"], [entry["observation_id"]], "plot")
        self.assertEqual(self.runtime.evidence(self.metadata, self.sid)["notes"], 9)

    def test_next_batch_reuses_latest_observation_and_deduplicates_text(self):
        first = self.screenshot()
        one, _ = self.mcp("read", observation_id=first["observation_id"], max_steps=2)
        two, _ = self.mcp("read", observation_id=one["observation_id"], max_steps=2)
        self.assertEqual(len(self.backend.inputs), 4)
        self.assertEqual([p["text"] for p in two["dialogue"]], [p["text"] for p in self.backend.pages[3:5]])

    def test_duplicate_batch_never_replays_or_captures(self):
        first = self.screenshot()
        params = {"observation_id": first["observation_id"], "max_steps": 2}
        one, _ = self.mcp("read", **params)
        counts = (len(self.backend.inputs), self.backend.captures)
        two, _ = self.mcp("read", **{**params, "max_steps": 9})
        self.assertTrue(two["cached"])
        self.assertEqual(one["observation_id"], two["observation_id"])
        self.assertEqual(counts, (len(self.backend.inputs), self.backend.captures))

    def test_inspect_zero_never_inputs_and_result_can_be_used_to_continue(self):
        first = self.screenshot()
        read, response = self.mcp("read", observation_id=first["observation_id"], max_steps=0)
        self.assertEqual(read["advanced"], 0)
        self.assertEqual(read["stop_reason"], "inspect_complete")
        self.assertEqual(len(response["content"]), 1)
        self.assertFalse(self.backend.inputs)
        result, _ = self.mcp("read", observation_id=read["observation_id"], max_steps=2)
        self.assertEqual(result["advanced"], 2)

    def test_choice_stops_before_selection_and_attaches_real_image(self):
        self.backend.pages[3] = {"text": "去公园\n回家", "kind": "choice"}
        result, response = self.mcp("read", observation_id=self.screenshot()["observation_id"])
        self.assertEqual(result["advanced"], 3)
        self.assertEqual(result["stop_reason"], "choice")
        self.assertEqual(len(self.backend.inputs), 3)
        self.assertEqual(response["content"][1]["mimeType"], "image/png")
        with self.runtime.connect() as db:
            raw = json.loads(db.execute("SELECT data FROM observations WHERE id=?", (result["observation_id"],)).fetchone()[0])
        self.assertEqual(base64.b64decode(response["content"][1]["data"]), Path(raw["image_path"]).read_bytes())

    def test_menu_is_never_advanced(self):
        self.backend.pages[0]["kind"] = "menu"
        result, _ = self.mcp("read", observation_id=self.screenshot()["observation_id"])
        self.assertEqual(result["stop_reason"], "menu")
        self.assertFalse(self.backend.inputs)

    def test_scene_change_returns_new_cg_before_more_input(self):
        self.backend.pages[1]["color"] = "orange"
        result, _ = self.mcp("read", observation_id=self.screenshot()["observation_id"])
        self.assertEqual(result["stop_reason"], "scene_changed")
        self.assertEqual(len(self.backend.inputs), 1)
        self.assertTrue(result["image_required"])

    def test_same_text_stops_after_one_input(self):
        self.backend.pages[1]["text"] = self.backend.pages[0]["text"]
        result, _ = self.mcp("read", observation_id=self.screenshot()["observation_id"])
        self.assertEqual(result["stop_reason"], "stalled")
        self.assertEqual(len(self.backend.inputs), 1)

    def test_typewriter_waits_for_stable_text_without_extra_keys(self):
        self.backend.on_capture = lambda n: {"text": "打字中" if n == 1 else "这句完整对白终于显示出来。"}
        result, _ = self.mcp("read", observation_id=self.screenshot()["observation_id"], max_steps=0)
        self.assertEqual(result["dialogue"][0]["text"], "这句完整对白终于显示出来。")
        self.assertGreaterEqual(self.backend.captures, 3)
        self.assertFalse(self.backend.inputs)

    def test_text_never_stabilizes_returns_image_without_pressing(self):
        self.backend.on_capture = lambda n: {"text": "文字持续变化" * n}
        result, _ = self.mcp("read", observation_id=self.screenshot()["observation_id"])
        self.assertEqual(result["stop_reason"], "text_unstable")
        self.assertFalse(self.backend.inputs)

    def test_unknown_input_is_not_replayed_by_same_batch(self):
        self.backend.fail_input = True
        first = self.screenshot()
        params = {"observation_id": first["observation_id"]}
        result, response = self.mcp("read", **params)
        self.assertTrue(response["isError"])
        self.assertEqual(result["last_action"]["state"], "unknown")
        self.mcp("read", **params)
        self.assertEqual(len(self.backend.inputs), 1)

    def test_capture_failure_after_input_is_not_replayed(self):
        self.backend.on_input = lambda: setattr(self.backend, "fail_capture", True)
        first = self.screenshot()
        result, _ = self.mcp("read", observation_id=first["observation_id"])
        self.assertEqual(result["advanced"], 1)
        self.assertTrue(result["last_action"]["input_posted"])
        self.assertFalse(result["image_required"])
        self.mcp("read", observation_id=first["observation_id"])
        self.assertEqual(len(self.backend.inputs), 1)

    def test_stop_between_steps_prevents_more_input(self):
        self.backend.on_input = lambda: self.broker.update(self.request_id, "cancel_requested", {})
        result, response = self.mcp("read", observation_id=self.screenshot()["observation_id"])
        self.assertEqual(len(self.backend.inputs), 1)
        self.assertTrue(response["isError"])

    def test_direction_change_between_steps_prevents_more_input(self):
        self.backend.on_input = lambda: self.runtime.steer(self.metadata, self.sid, "停止旧目标，先观察")
        result, _ = self.mcp("read", observation_id=self.screenshot()["observation_id"])
        self.assertEqual(len(self.backend.inputs), 1)
        self.assertFalse(result["ok"])

    def test_action_limit_applies_inside_batch(self):
        self.metadata["sandbox_config"]["max_actions"] = 3
        self.configure()
        result, _ = self.mcp("read", observation_id=self.screenshot()["observation_id"])
        self.assertEqual(result["stop_reason"], "action_limit")
        self.assertEqual(len(self.backend.inputs), 3)

    def test_visual_checkpoint_is_shared_across_batches(self):
        self.configure(visual_interval=3)
        one, _ = self.mcp("read", observation_id=self.screenshot()["observation_id"], max_steps=2)
        self.assertFalse(one["image_required"])
        two, _ = self.mcp("read", observation_id=one["observation_id"], max_steps=2)
        self.assertEqual(two["stop_reason"], "visual_checkpoint")
        self.assertEqual(len(self.backend.inputs), 3)
        self.assertTrue(two["image_required"])

    def test_invalid_or_stale_observation_never_inputs(self):
        first = self.screenshot()
        self.screenshot()
        result, response = self.mcp("read", observation_id=first["observation_id"])
        self.assertTrue(response["isError"])
        self.assertFalse(self.backend.inputs)

    def test_tampered_frame_is_rejected(self):
        first = self.screenshot()
        with self.runtime.connect() as db:
            frame = json.loads(db.execute("SELECT data FROM observations WHERE id=?", (first["observation_id"],)).fetchone()[0])
        Path(frame["image_path"]).write_bytes(b"changed")
        result, response = self.mcp("read", observation_id=first["observation_id"])
        self.assertTrue(response["isError"])
        self.assertFalse(self.backend.inputs)

    def test_pid_change_is_rejected_before_input(self):
        result, response = self.mcp("read", observation_id=self.screenshot()["observation_id"], pid=78)
        self.assertTrue(response["isError"])
        self.assertFalse(self.backend.inputs)

    def test_disabled_mode_falls_back_without_input(self):
        self.configure(enabled=False)
        result, response = self.mcp("read", observation_id=self.screenshot()["observation_id"])
        self.assertTrue(response["isError"])
        self.assertFalse(self.backend.inputs)

    def test_software_does_not_use_dialogue_keys(self):
        with self.runtime.connect() as db:
            db.execute("UPDATE games SET kind='software' WHERE id=?", (self.target["id"],))
        result, response = self.mcp("read", observation_id=self.screenshot()["observation_id"])
        self.assertTrue(response["isError"])
        self.assertFalse(self.backend.inputs)

    def test_reserved_interrupted_batch_cannot_start_again(self):
        first = self.screenshot()
        with patch("galgame_reading.DialogueReader._read", side_effect=RuntimeError("interrupted")) as read:
            self.mcp("read", observation_id=first["observation_id"])
            self.mcp("read", observation_id=first["observation_id"])
        self.assertEqual(read.call_count, 1)
        self.assertFalse(self.backend.inputs)

    def test_cli_exposes_same_read_contract(self):
        first = self.screenshot()
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["desktop", "read", "--context-file", self.work,
                           "--observation-id", first["observation_id"], "--max-steps", "0"])
        self.assertIsNone(status)
        result = json.loads(output.getvalue())
        self.assertEqual(result["stop_reason"], "inspect_complete")
        self.assertTrue(Path(result["image_path"]).is_file())

    def test_read_only_uses_enter_or_space(self):
        for key in ("tab", "escape", "s"):
            result, response = self.mcp("read", observation_id=self.screenshot()["observation_id"], key=key)
            self.assertTrue(response["isError"])
        self.assertFalse(self.backend.inputs)

    def test_ocr_uncertainty_and_choices_stop_without_input(self):
        self.backend.native = False
        cases = [
            ([{"text": "这是一行置信度很低的对白。", "confidence": .6, "box": [.1, .7, .8, .78]}], "low_confidence"),
            ([{"text": "去公园还是回家", "confidence": .99, "box": [.2, .3, .8, .4]}], "choice_or_overlay"),
            ([{"text": "1. 去公园散步", "confidence": .99, "box": [.2, .7, .8, .75]}], "choice_or_overlay"),
            ([{"text": "去公园散步", "confidence": .99, "box": [.2, .65, .8, .7]},
              {"text": "回家等候朋友", "confidence": .99, "box": [.2, .82, .8, .87]}], "choice_or_overlay"),
            ([], "no_dialogue"),
        ]
        for lines, reason in cases:
            with self.subTest(reason=reason), patch("galgame_reading.local_ocr", return_value=lines):
                result, response = self.mcp("read", observation_id=self.screenshot()["observation_id"])
                self.assertEqual(result["stop_reason"], reason)
                self.assertTrue(result["image_required"])
                self.assertFalse(self.backend.inputs)

    def test_ocr_dependency_failure_returns_image_without_input(self):
        self.backend.native = False
        with patch("galgame_reading.local_ocr", side_effect=ImportError("not installed")):
            result, _ = self.mcp("read", observation_id=self.screenshot()["observation_id"])
        self.assertEqual(result["stop_reason"], "ocr_unavailable")
        self.assertFalse(self.backend.inputs)

    def test_local_text_evidence_survives_later_failure(self):
        self.backend.fail_input = True
        result, _ = self.mcp("read", observation_id=self.screenshot()["observation_id"])
        with self.runtime.connect() as db:
            rows = db.execute("SELECT data FROM observations WHERE session=?", (self.sid,)).fetchall()
        evidence = [json.loads(row["data"]).get("local_reading", {}).get("text") for row in rows]
        self.assertIn(self.backend.pages[0]["text"], evidence)

    def test_long_dialogue_is_not_truncated_and_then_skipped(self):
        self.backend.pages[0]["text"] = "非常长的对白。" * 1200
        result, _ = self.mcp("read", observation_id=self.screenshot()["observation_id"])
        self.assertEqual(result["stop_reason"], "text_too_long")
        self.assertTrue(result["image_required"])
        self.assertFalse(self.backend.inputs)


class NativeReadingTests(unittest.TestCase):
    def adapter(self, screens):
        return Adapter(SimpleNamespace(exports=SimpleNamespace(get_screen=screens.get)), {"run": "."})

    def test_current_screen_only_and_markup_removed(self):
        adapter = self.adapter({"say": SimpleNamespace(scope={"who": "小光", "what": "{b}今天很开心。{/b}"})})
        self.assertEqual(adapter.dialogue_snapshot(), {"source": "renpy", "kind": "dialogue", "text": "小光：今天很开心。"})

    def test_choice_and_overlay_take_priority_over_underlying_dialogue(self):
        say = SimpleNamespace(scope={"what": "背景中的旧对白"})
        choice = SimpleNamespace(scope={"items": [SimpleNamespace(caption="去公园"), SimpleNamespace(caption="回家")]})
        reading = self.adapter({"say": say, "choice": choice}).dialogue_snapshot()
        self.assertEqual(reading["kind"], "choice")
        self.assertEqual(reading["text"], "去公园\n回家")
        self.assertEqual(self.adapter({"say": say, "save": object()}).dialogue_snapshot()["kind"], "menu")

    def test_custom_screen_missing_text_never_invents_dialogue(self):
        self.assertEqual(self.adapter({}).dialogue_snapshot()["kind"], "unknown")

    def test_current_screen_survives_authenticated_frame_transport(self):
        import test_renpy_bridge as fixture_module
        fixture = fixture_module.RenpyBridgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        screens = {"say": SimpleNamespace(scope={"who": "小光", "what": "此刻画面中的对白。"})}
        fixture.exports.get_screen = screens.get
        picture = fixture.bridge.call(fixture.window, "capture")
        self.assertEqual(picture.info["game_reading"]["text"], "小光：此刻画面中的对白。")
        screens["choice"] = SimpleNamespace(scope={"items": [SimpleNamespace(caption="回家")]})
        self.assertEqual(fixture.bridge.call(fixture.window, "capture").info["game_reading"]["kind"], "choice")


class ReadingConfigurationTests(unittest.TestCase):
    def test_old_configuration_gets_reading_defaults(self):
        config = AppConfig.model_validate({"sandbox": {"enabled": True}})
        self.assertEqual(config.sandbox.galgame_reading.batch_lines, 8)
        self.assertEqual(config.sandbox.galgame_reading.visual_interval, 24)

    def test_gui_roundtrip_preserves_advanced_reading_settings(self):
        from PySide6.QtWidgets import QApplication
        from ui.components.companion_tab import CompanionTabWidget
        app = QApplication.instance() or QApplication([])
        config = AppConfig()
        config.stickers.directory = "does-not-exist-test-stickers"
        config.sandbox.galgame_reading = GalgameReadingConfig(visual_interval=42, scene_threshold=.23)
        widget = CompanionTabWidget(config)
        self.addCleanup(widget.close)
        widget.reading_batch.setValue(4)
        widget.dialogue_top.setValue(70)
        _, sandbox = widget.collect()
        self.assertEqual(sandbox.galgame_reading.batch_lines, 4)
        self.assertEqual(sandbox.galgame_reading.dialogue_top, .70)
        self.assertEqual(sandbox.galgame_reading.visual_interval, 42)
        self.assertEqual(sandbox.galgame_reading.scene_threshold, .23)
        config.sandbox = sandbox
        widget.load_config(config)
        self.assertEqual(widget.reading_batch.value(), 4)


if __name__ == "__main__":
    unittest.main()
