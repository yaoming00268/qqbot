"""Desktop bridge regressions: persistent edits, process boundaries and real training."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import io
import json
import sys
import types
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from config import AppConfig
from ui.studio_api import StudioAPI, validated_preferences


class StudioBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.config_path = self.home / "config.yaml"
        self.api = StudioAPI(self.home)
        self.initial = self.ok(self.api.bootstrap())

    def ok(self, result):
        self.assertTrue(result["ok"], result)
        return result["data"]

    def test_browser_transport_uses_dedicated_profile_and_persists(self):
        from config import GeminiWebConfig
        from pydantic import ValidationError

        self.assertEqual(GeminiWebConfig().transport, "cookie")
        with self.assertRaises(ValidationError):
            GeminiWebConfig(page_profile_dir="   ")
        changes = {"media": {"gemini_web": {"transport": "browser", "page_profile_dir": "profiles/gemini"}}}
        self.ok(self.api.save_config(changes, self.initial["revision"]))
        self.assertEqual(AppConfig.load(str(self.config_path)).media.gemini_web.transport, "browser")
        account = {"source": "browser", "status": "ok", "models": [{"name": "pro", "available": True}]}
        page = types.ModuleType("gemini_page_browser")
        page.launch_page_login = Mock()
        page.inspect_page_account = AsyncMock(return_value=account)
        with patch.dict(sys.modules, {"gemini_page_browser": page}):
            self.ok(self.api.browser_tools("connect"))
            self.assertEqual(Path(page.launch_page_login.call_args.args[0].page_profile_dir), self.home / "profiles/gemini")
            self.ok(self.api.browser_tools("inspect"))
            for _ in range(400):
                result = self.ok(self.api.poll())
                if result["status"]["task"]["state"] != "running":
                    break
                time.sleep(.01)
            self.assertEqual(result["status"]["task"]["state"], "completed", result)
            self.assertEqual(result["status"]["task"]["result"], account)
            self.assertEqual(Path(page.inspect_page_account.call_args.args[0].page_profile_dir), self.home / "profiles/gemini")

    def test_partial_save_preserves_credentials_and_unedited_config(self):
        config = AppConfig.load(str(self.config_path))
        config.qq.secret = "test-secret-not-for-logs"
        config.media.local_library_roots = ["retained-library"]
        config.bot.mention_probability = .37
        config.save(str(self.config_path))
        initial = self.ok(self.api.bootstrap())
        result = self.ok(self.api.save_config({"bot": {"backlog_interrupt_messages": 5}}, initial["revision"]))
        restored = AppConfig.load(str(self.config_path))
        self.assertEqual(restored.bot.backlog_interrupt_messages, 5)
        self.assertEqual(restored.qq.secret, config.qq.secret)
        self.assertEqual(restored.media.local_library_roots, ["retained-library"])
        self.assertEqual(restored.bot.mention_probability, .37)
        self.assertNotEqual(initial["revision"], result["revision"])
        self.assertEqual(self.ok(self.api.poll())["events"], [])

    def test_stale_invalid_or_unknown_changes_never_overwrite_disk(self):
        initial = self.initial
        self.ok(self.api.save_config({"bot": {"backlog_interrupt_messages": 7}}, initial["revision"]))
        saved = self.config_path.read_bytes()
        stale = self.api.save_config({"bot": {"master_id": "other"}}, initial["revision"])
        self.assertFalse(stale["ok"])
        fresh = self.ok(self.api.bootstrap())["revision"]
        self.assertFalse(self.api.save_config({"bot": {"backlog_interrupt_messages": -1}}, fresh)["ok"])
        self.assertFalse(self.api.save_config({"bot": {"typo_field": True}}, fresh)["ok"])
        self.assertEqual(self.config_path.read_bytes(), saved)

    def test_appearance_is_validated_and_saved_separately_from_bot_config(self):
        original = self.config_path.read_bytes()
        self.ok(self.api.preferences({"theme": "dark", "surface_opacity": 52, "window_opacity": 87, "blur": 35}))
        restored = self.ok(StudioAPI(self.home).bootstrap())["preferences"]
        self.assertEqual(restored["window_opacity"], 87)
        self.assertEqual(restored["surface_opacity"], 52)
        self.assertEqual(restored["theme"], "dark")
        self.assertEqual(self.config_path.read_bytes(), original)
        for value in (float("nan"), 64, 101):
            self.assertFalse(self.api.preferences({"window_opacity": value})["ok"])

    def test_persona_rename_keeps_identity_and_group_binding(self):
        initial = self.initial["personas"]
        persona = dict(initial["items"][0])
        old_name, old_id = persona["name"], persona["id"]
        cfg = AppConfig.load(str(self.config_path))
        from config import GroupCustomConfig
        cfg.bot.group_settings["123"] = GroupCustomConfig(group_id="123", persona=old_name)
        cfg.save(str(self.config_path))
        persona["name"] = "改名后的角色"
        result = self.ok(self.api.save_persona(old_name, persona, initial["revision"]))
        self.assertEqual(next(p for p in result["items"] if p["name"] == persona["name"])["id"], old_id)
        self.assertEqual(AppConfig.load(str(self.config_path)).bot.get_persona_name("123"), persona["name"])
        self.assertEqual(result["active"], persona["name"])

    def test_feed_preserves_human_text_but_cleans_decorative_system_emoji(self):
        self.api._consume_line('[FEED_MSG] ' + json.dumps({"content": '<script>alert(1)</script> 😊', "user_id": "123"}))
        self.api._consume_line("INFO 🤖 核心运行中")
        data = self.ok(self.api.poll())
        self.assertIn("😊", data["events"][0]["data"]["content"])
        self.assertNotIn("🤖", data["events"][1]["data"]["text"])
        self.assertEqual(self.ok(self.api.poll(data["cursor"]))["events"], [])

    def test_native_controls_request_graceful_stop_and_restart_only_after_exit(self):
        gate = threading.Event()
        process = Mock(pid=4321)
        process.poll.return_value = None
        class Output:
            def __iter__(self):
                gate.wait(2)
                process.poll.return_value = 0
                return iter(["INFO 正常退出\n"])
            def close(self):
                pass
        process.stdout = Output()
        process.wait.return_value = 0
        with patch("ui.studio_api.subprocess.Popen", return_value=process) as popen:
            self.ok(self.api.control("start"))
            self.ok(self.api.control("start"))
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(popen.call_args.kwargs["cwd"], self.home)
            self.ok(self.api.control("stop"))
            request = json.loads((self.home / "data/runtime_control.json").read_text(encoding="utf-8"))
            self.assertEqual(request["action"], "stop")
            process.kill.assert_not_called()
            process.terminate.assert_not_called()
            gate.set()
            for _ in range(100):
                if self.ok(self.api.poll())["status"]["state"] == "stopped":
                    break
                time.sleep(.01)
            self.assertEqual(self.ok(self.api.poll())["status"]["state"], "stopped")

    def test_memory_reads_existing_structured_store_without_creating_missing_databases(self):
        result = self.ok(self.api.records("memory"))
        self.assertFalse((self.home / "data/main_chat/identity.sqlite3").exists())
        from bot_identity import IdentityStore
        store = IdentityStore(self.home / "data/main_chat/identity.sqlite3")
        store.observe("owner", "group:test", "123", "测试用户")
        result = self.ok(self.api.records("memory", "测试用户"))
        self.assertEqual(result["contacts"][0]["user_id"], "123")
        self.assertFalse(result["errors"])

    def test_real_training_and_classifier_analysis_use_worker_events(self):
        params = {"labels": ["喜悦", "悲伤"], "epochs": 3, "feature_dim": 128,
                  "classifier_path": "models/test_classifier.npz", "validation_fraction": 0}
        rows = [{"text": text, "label": label} for label, texts in
                (("喜悦", ["今天真开心", "我很高兴", "这太好了"]), ("悲伤", ["今天很难过", "我好伤心", "真的很遗憾"])) for text in texts]
        self.ok(self.api.emotion("train", {"config": params, "rows": rows}))
        def wait():
            for _ in range(400):
                result = self.ok(self.api.poll())
                if result["status"]["task"]["state"] != "running":
                    return result
                time.sleep(.01)
            self.fail("Model worker did not complete")
        result = wait()
        self.assertEqual(result["status"]["task"]["state"], "completed", result)
        self.assertTrue((self.home / "models/test_classifier.npz").is_file())
        self.assertEqual(len(result["status"]["task"]["result"]["history"]), 3)
        self.ok(self.api.emotion("analyze", {"config": {**params, "provider": "local_classifier"}, "text": "今天真开心。"}))
        result = wait()
        self.assertEqual(result["status"]["task"]["state"], "completed", result)
        self.assertIn(result["status"]["task"]["result"][0]["label"], params["labels"])
        self.assertTrue(any(event["kind"] == "model_result" for event in result["events"]))

    def test_gui_entry_does_not_require_importing_qt(self):
        import ast
        for path in (Path("gui.py"), Path("ui/webview_app.py"), Path("ui/studio_api.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
            self.assertFalse(any("PySide6" in ast.unparse(node) for node in imports))

    def test_glossary_template_validation_and_revision_preserve_bot_updates(self):
        initial = self.ok(self.api.glossary())
        path = self.home / "data/slang_glossary.md"
        self.assertFalse(path.exists())
        self.assertIn("<!-- entries:start -->", initial["text"])
        edited = initial["text"].replace("<!-- entries:end -->", "| | 验收 | | 测试 | #本地 | |\n<!-- entries:end -->")
        saved = self.ok(self.api.glossary("save", edited, initial["revision"]))
        from slang_glossary import SlangGlossary
        self.assertEqual(SlangGlossary(path).entries()[0].term, "验收")
        self.assertFalse(self.api.glossary("save", "格式无效", saved["revision"])["ok"])
        path.write_text(edited + "\n机器人并发修改\n", encoding="utf-8")
        self.assertFalse(self.api.glossary("save", edited, saved["revision"])["ok"])
        self.assertIn("机器人并发修改", path.read_text(encoding="utf-8"))

    def test_autonomy_view_is_read_only_and_does_not_invent_recoverable_tasks(self):
        data = self.ok(self.api.records("autonomy"))
        self.assertEqual(data["sessions"], [])
        self.assertFalse((self.home / "data/sandbox/sandbox.sqlite3").exists())
        self.assertFalse(self.api.recover_activity("missing-task", "restart")["ok"])


if __name__ == "__main__":
    unittest.main()
