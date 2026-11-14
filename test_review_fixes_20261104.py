"""Verification regressions for all 10 fixes (F01 - F10) from the 20261104 Project Review.
Run from project root: python -m unittest test_review_fixes_20261104.py
"""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from config import AppConfig, BotBehaviorConfig, GroupCustomConfig, MediaConfig, OneBotAccountConfig
from conversation_runtime import RuntimeStore, TextOutbox
from media_jobs import MediaService
from media_models import MessageEnvelope
from onebot_client import OneBotClient
from persona_isolation import identity
from persona_manager import PersonaManager, PersonaPreset
from sandbox_runtime import SandboxRuntime
from skills.base import SkillContext, SkillResult
from skills.task_skills import RunTaskSkill


class TestReviewFixes20261104(unittest.IsolatedAsyncioTestCase):
    """Test suite directly validating F01 through F10 resolutions."""

    # F01: Mutable nickname never grants owner capability; group master & global master coexist
    def test_f01_nickname_never_grants_owner_capability(self):
        cfg = BotBehaviorConfig(master_id="111", group_settings={
            "123": GroupCustomConfig(group_id="123", master_name="KnownOwner", master_id="")
        })
        # Sender 999 with nickname KnownOwner should NOT be granted master
        granted = cfg.is_master("123", "999", "KnownOwner")
        self.assertFalse(granted)
        with self.assertRaises(PermissionError):
            SandboxRuntime.require_owner({"is_master": granted})

        # Global master 111 with any nickname should be granted master
        self.assertTrue(cfg.is_master("123", "111", "RandomNick"))

        # Group-specific master 888 and global master 111 must both be recognized
        cfg_with_grp_owner = BotBehaviorConfig(master_id="111", group_settings={
            "123": GroupCustomConfig(group_id="123", master_name="GroupOwner", master_id="888")
        })
        self.assertTrue(cfg_with_grp_owner.is_master("123", "888", "GroupOwner"))
        self.assertTrue(cfg_with_grp_owner.is_master("123", "111", "GlobalOwner"))
        self.assertFalse(cfg_with_grp_owner.is_master("123", "999", "GroupOwner"))

    # F02: Non-owner cannot clear group memory; owner can; private chats can clear own
    async def test_f02_non_owner_cannot_clear_group_memory(self):
        from test_startup_participation import StartupParticipationTests
        from test_ai_fixture import drain_runtime
        fixture = StartupParticipationTests()
        fixture.setUp()
        try:
            bot, _ = fixture.bot(stored_cid="existing-main")
            bot.app_config.bot.master_id = "111"
            bot._is_ws_connected = lambda: True
            with patch.object(bot.memory_mgr, "clear_memory", wraps=bot.memory_mgr.clear_memory) as clear, \
                 patch.object(bot, "_clear_image_context") as clear_images:
                result = await bot._handle_command("/clearmemory", group_id="123", user_id="999", is_group=True)
                await drain_runtime(bot)
                self.assertTrue(result)
                clear.assert_not_called()
                clear_images.assert_not_called()
                bot.send_group_message.assert_awaited_once()
                self.assertIn("普通成员无权清空", bot.send_group_message.await_args.args[1].body)
                await bot._handle_command("/clearmemory", group_id="123", user_id="111", is_group=True)
                await drain_runtime(bot)
                clear.assert_called_once_with("123")
                clear_images.assert_called_once_with("123")
            official, _ = fixture.bot(True, stored_cid="official-main")
            official.app_config.bot.master_id = ""
            reply = AsyncMock()
            with patch.object(official.memory_mgr, "clear_memory") as clear:
                await official._handle_message_core("group_123", "/clearmemory", reply, user_openid="")
                await drain_runtime(official)
                clear.assert_not_called()
                reply.assert_awaited_once()
                self.assertIn("普通成员无权清空", reply.await_args.args[0])
        finally:
            await fixture.asyncTearDown()

    # F03 & F04: Outbox restart guard & Gateway account binding
    async def test_f04_gateway_account_mismatch_rejected(self):
        bot = OneBotClient.__new__(OneBotClient)
        bot.app_config = AppConfig()
        bot.account_key = "account_1"
        bot.account_config = OneBotAccountConfig(account_id="111")
        bot.self_id = ""
        bot._identity_confirmed = False
        bot.outbox = Mock()
        bot.personas = SimpleNamespace(get_active_persona=lambda: SimpleNamespace(id="p1", name="Review"))
        bot.ai = SimpleNamespace(_sessions={}, bind_account=Mock())
        bot.send_action = AsyncMock(return_value={"status": "ok", "data": {"user_id": 222, "nickname": "Other"}})

        with patch("onebot_client.asyncio.sleep", new=AsyncMock()):
            ok = await bot._fetch_login_info()

        # Must reject login info if QQ differs
        self.assertFalse(ok)
        self.assertFalse(bot._identity_confirmed)
        self.assertEqual(bot.self_id, "")
        bot.outbox.wake.assert_not_called()

    async def test_f03_outbox_restart_cancels_superseded_or_switched_persona(self):
        bot = OneBotClient.__new__(OneBotClient)
        bot.self_id = "111"
        bot.personas = SimpleNamespace(get_active_persona=lambda: SimpleNamespace(id="p_fubuki", name="Fubuki"))
        mock_session = SimpleNamespace(generation=2)
        bot.ai = SimpleNamespace(get_or_create_session=Mock(return_value=mock_session))

        from persona_isolation import MARKER, identity
        preset = SimpleNamespace(id="p_fubuki", name="Fubuki")
        bot.personas = SimpleNamespace(
            get_active_persona=lambda: preset,
            get_persona=lambda name: preset,
            presets={"p_fubuki": preset}
        )
        bot.account_key = "review"
        bot.app_config = AppConfig()
        fubuki_identity = identity(bot, preset)
        session_key = f"onebot_group_123{MARKER}{fubuki_identity}"

        # 1. Stale persona id -> invalid
        stale_meta = {
            "account_id": "111",
            "session_key": session_key,
            "generation": 2,
            "persona_id": "p_maid",
            "superseded": False,
        }
        self.assertFalse(bot._validate_outbox_guard(stale_meta, {"scope": "g123"}, {}))

        # 2. Stale generation -> invalid
        stale_gen_meta = {
            "account_id": "111",
            "session_key": session_key,
            "generation": 1,
            "persona_id": fubuki_identity,
            "superseded": False,
        }
        self.assertFalse(bot._validate_outbox_guard(stale_gen_meta, {"scope": "g123"}, {}))

        # 3. Valid matching meta -> valid
        valid_meta = {
            "account_id": "111",
            "session_key": session_key,
            "generation": 2,
            "persona_id": fubuki_identity,
            "superseded": False,
        }
        self.assertTrue(bot._validate_outbox_guard(valid_meta, {"scope": "g123"}, {}))

        # 4. Account not yet confirmed / empty -> invalid
        bot.self_id = ""
        bot.account_config = None
        self.assertFalse(bot._validate_outbox_guard(valid_meta, {"scope": "g123"}, {}))

    # F05: Cache cleanup does NOT delete running native task intermediate files
    async def test_f05_cache_cleanup_protects_running_native_task_intermediate(self):
        with tempfile.TemporaryDirectory(prefix="qqbot_review_cache_fix_") as temporary:
            root = Path(temporary)
            service = MediaService(MediaConfig(
                database_path=str(root / "media.sqlite3"),
                cache_dir=str(root / "cache"),
                cache_max_mb=100
            ))
            ctx = MessageEnvelope(transport="onebot", account_id="review", conversation_id="123", sender_id="111",
                message_id="work", text="prepare large artifact", send_text=AsyncMock(), is_group=True)
            context = SkillContext(session_key="onebot_group_123", conversation_id="123", user_id="111",
                media_context=ctx, media_service=service, ai_client=SimpleNamespace(chat=AsyncMock()))
            skill = RunTaskSkill()

            intermediate_survived = False

            async def running(params, context, task, directory, job):
                nonlocal intermediate_survived
                intermediate = directory / "intermediate.bin"
                with intermediate.open("wb") as output:
                    output.truncate(101 * 1024 * 1024)

                self.assertIn(job["id"], service.tasks)
                # Verify lease path is included in active_paths
                protected = service.store.active_paths()
                self.assertTrue(any(str(directory) in p for p in protected))

                # Trigger cache cleanup
                await service._clean_cache_if_needed()
                intermediate_survived = intermediate.exists()
                return SkillResult(True, "done", data={"file": str(intermediate)})

            with patch.object(skill, "_run", new=running):
                await skill.execute({"task": "prepare large artifact"}, context)

            self.assertTrue(intermediate_survived, "Active task intermediate file should survive cache cleanup")

    # F06: NapCat multi-account config conforms to bundled schema
    def test_f06_napcat_config_matches_bundled_schema(self):
        from napcat_manager import NapCatManager
        with tempfile.TemporaryDirectory(prefix="qqbot_review_napcat_fix_") as temporary:
            manager = NapCatManager(temporary)
            self.assertTrue(manager.setup_multi_account_configs({"account_1": {
                "account_id": "111", "ws_url": "ws://127.0.0.1:3001", "access_token": "review-token"
            }}))
            cfg_path = Path(temporary) / "config/onebot11_111.json"
            self.assertTrue(cfg_path.exists())
            data = json.loads(cfg_path.read_text(encoding="utf-8"))

            # Check network.websocketServers format
            self.assertIn("network", data)
            self.assertIn("websocketServers", data["network"])
            servers = data["network"]["websocketServers"]
            self.assertTrue(isinstance(servers, list))
            self.assertTrue(any(s.get("port") == 3001 and s.get("token") == "review-token" for s in servers))

    # F07: Persona rename preserves persistent ID and identity
    def test_f07_persona_rename_preserves_persistent_identity(self):
        with tempfile.TemporaryDirectory(prefix="qqbot_review_rename_fix_") as temporary:
            manager = PersonaManager(str(Path(temporary) / "personas.yaml"))
            old = manager.get_active_persona()
            client = SimpleNamespace(account_key="review", send_action=None, personas=manager)
            before_id = identity(client, old)

            # Update name to Renamed Persona
            manager.update_preset(old.name, PersonaPreset(name="Renamed persona", prompt=old.prompt))
            renamed = manager.get_active_persona()
            after_id = identity(client, renamed)

            # Persistent identity must remain invariant
            self.assertEqual(before_id, after_id)
            self.assertEqual(old.id, renamed.id)

    def test_f07_persona_directory_migration_without_crash(self):
        import hashlib
        from persona_isolation import PersonaMemory
        with tempfile.TemporaryDirectory(prefix="qqbot_review_mig_fix_") as temporary:
            client = SimpleNamespace(account_key="test", send_action=lambda: None, personas=SimpleNamespace(presets={}))
            preset = PersonaPreset(name="TestPersona", id="pid123", prompt="test")
            old_key = hashlib.sha256(b"onebot:test\nTestPersona").hexdigest()[:24]
            old_dir = Path(temporary) / "profiles" / old_key
            old_dir.mkdir(parents=True, exist_ok=True)
            (old_dir / "old_memo.json").write_text("{}", encoding="utf-8")

            mem = PersonaMemory(client, SimpleNamespace(storage_dir=temporary, max_entries=10))
            mgr = mem.manager("123", persona=preset)
            self.assertTrue(Path(mgr.storage_dir).exists())
            self.assertTrue((Path(mgr.storage_dir) / "old_memo.json").exists())
            self.assertTrue((Path(mgr.storage_dir) / "profile.json").exists())

    # F08: GUI bot startup aborts if config validation fails
    def test_f08_gui_aborts_start_on_validation_failure(self):
        from ui.main_window import BotStudioGUI
        gui = SimpleNamespace(
            bot_process=None,
            companion_tab_widget=Mock(),
            _stdout_decoder=Mock(),
            _on_process_output=Mock(),
            _on_process_finished=Mock(),
            console_tab_widget=Mock(),
            lbl_status_indicator=Mock(),
            lbl_status=Mock(),
            btn_start_bot=Mock(),
            btn_stop_bot=Mock(),
            btn_restart_bot=Mock(),
            lbl_onebot_pid=Mock(),
            lbl_official_pid=Mock(),
        )
        gui.companion_tab_widget.collect.side_effect = ValueError("invalid config")
        gui._on_save_config = lambda **kwargs: BotStudioGUI._on_save_config(gui, **kwargs)
        process = Mock()

        with patch("ui.main_window.QMessageBox.warning") as warning, \
                patch("ui.main_window.QProcess", return_value=process):
            BotStudioGUI._on_start_bot(gui)

        warning.assert_called_once()
        process.start.assert_not_called()

    def test_f09_onebot_stop_threadsafe(self):
        bot = OneBotClient.__new__(OneBotClient)
        mock_loop = Mock()
        mock_loop.is_running.return_value = True
        mock_task = Mock()
        mock_task.get_loop.return_value = mock_loop
        mock_task.done.return_value = False
        bot._main_task = mock_task
        bot._stop_event = Mock()
        bot._pending_echoes = {}
        bot.stop()
        mock_loop.call_soon_threadsafe.assert_any_call(bot._stop_event.set)
        mock_loop.call_soon_threadsafe.assert_any_call(mock_task.cancel)

    # F10: QEMU pycdlib dependency declared and checked
    def test_f10_pycdlib_dependency(self):
        req_path = Path("requirements.txt")
        self.assertTrue(req_path.exists())
        req_content = req_path.read_text(encoding="utf-8")
        self.assertIn("pycdlib", req_content)

        # Verify qemu_vm error message on missing pycdlib
        with tempfile.TemporaryDirectory() as td:
            from qemu_vm import QemuVM, VMError
            vm = QemuVM(td)
            vm.binary = Path(td) / "qemu-system-x86_64.exe"
            vm.binary.touch()
            vm.disk = Path(td) / "disk.qcow2"
            vm.disk.touch()
            with patch.dict("sys.modules", {"pycdlib": None}):
                with self.assertRaises(VMError) as cm:
                    vm.prepare()
                self.assertIn("pip install pycdlib", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
