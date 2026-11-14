"""Deep verification test for fixes from PROJECT_REVIEW_20261030.md."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from config import AppConfig, GroupCustomConfig
from persona_manager import PersonaManager, PersonaPreset
from console_tools.broker import ConsoleBroker


class ReviewFixesEdgeCasesTest(unittest.TestCase):
    def test_config_atomic_save_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.yaml"
            cfg = AppConfig()
            cfg.bot.master_id = "initial_master"
            cfg.save(str(cfg_path))
            self.assertTrue(cfg_path.exists())
            
            # Second save should create .backup
            cfg.bot.master_id = "updated_master"
            cfg.save(str(cfg_path))
            backup_path = cfg_path.with_name("config.yaml.backup")
            self.assertTrue(backup_path.exists())
            backup_content = backup_path.read_text(encoding="utf-8")
            self.assertIn("initial_master", backup_content)
            
            # Empty file load raises ValueError with backup hint
            cfg_path.write_text("   ", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                AppConfig.load(str(cfg_path))
            self.assertIn("配置文件为空或已损坏", str(ctx.exception))
            self.assertIn("config.yaml.backup", str(ctx.exception))

    def test_persona_manager_backup_and_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            p_path = Path(tmp) / "personas.yaml"
            pm = PersonaManager(str(p_path))
            self.assertTrue(p_path.exists())
            
            # Second save creates backup
            pm.save()
            backup_path = p_path.with_name("personas.yaml.backup")
            self.assertTrue(backup_path.exists())
            
            # Rename to existing name raises ValueError
            presets = list(pm.presets.keys())
            self.assertGreaterEqual(len(presets), 2)
            first, second = presets[0], presets[1]
            new_preset = PersonaPreset(name=second, prompt="test prompt")
            with self.assertRaises(ValueError) as ctx:
                pm.update_preset(first, new_preset)
            self.assertIn("已存在", str(ctx.exception))
            
            # Empty persona file raises ValueError with backup hint
            p_path.write_text("", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                PersonaManager(str(p_path))
            self.assertIn("人设配置文件为空或已损坏", str(ctx.exception))
            self.assertIn("personas.yaml.backup", str(ctx.exception))

    def test_gui_group_settings_master_name_not_defaulted_to_title(self):
        from PySide6.QtWidgets import QApplication
        from ui.components.config_tab import ConfigTabWidget
        app = QApplication.instance() or QApplication([])
        cfg = AppConfig()
        tab = ConfigTabWidget(cfg)
        tab._on_add_group_setting()
        tab.save_group_settings()
        saved_grp = cfg.bot.group_settings.get("10001")
        self.assertIsNotNone(saved_grp)
        # master_name must be empty, NOT defaulted to "主人"
        self.assertEqual(saved_grp.master_name, "")
        tab.close()

    def test_broker_initiator_isolation_with_empty_caller(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "console.db"
            broker = ConsoleBroker(str(db_path))
            ctx_dir = Path(tmp) / "contexts"
            
            token_a_file = broker.issue_context({"namespace": "ns", "scope": "grp_1", "sender_id": "user_a"}, str(ctx_dir))
            token_b_file = broker.issue_context({"namespace": "ns", "scope": "grp_1", "sender_id": ""}, str(ctx_dir))
            
            _, token_a = ConsoleBroker.from_context(token_a_file)
            _, token_b = ConsoleBroker.from_context(token_b_file)
            
            res = broker.submit(token_a, "task", "secret work")
            req_id = res["id"]
            
            # Caller B with empty sender attempting to access A's task must be rejected
            with self.assertRaises(PermissionError):
                broker.status(token_b, req_id)

    def test_console_delivery_safe_media_error(self):
        from console_delivery import deliver_console_result
        from media_models import MessageEnvelope
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "console.db"
            broker = ConsoleBroker(str(db_path))
            ctx_dir = Path(tmp) / "contexts"
            token_file = broker.issue_context({"namespace": "ns", "scope": "grp_1", "sender_id": "u1", "session_key": "s1", "generation": 0}, str(ctx_dir))
            _, token = ConsoleBroker.from_context(token_file)
            
            req_res = broker.submit(token, "task", "work", request_key="k1")
            req_id = req_res["id"]
            broker.update(req_id, "ready", {"job_id": "non_existent_job_id", "result": {}})
            
            client = MagicMock()
            client._get_main_coordinator.return_value = MagicMock(broker=broker, namespace="ns")
            client.ai = MagicMock()
            client._get_media_service.return_value = MagicMock()
            client._get_media_service().store.job.side_effect = Exception("Job not found")
            
            envelope = MessageEnvelope("onebot", "acc", "grp_1", "u1", "m1", "text", MagicMock())
            # deliver_console_result should not crash on non-existent job
            asyncio.run(deliver_console_result(client, req_id, envelope, broker.context(token)))


if __name__ == "__main__":
    unittest.main()
