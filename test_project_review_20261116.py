"""Regression checks for the six project review findings; all services are fixtures."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer
from config import AppConfig, GroupCustomConfig, config_revision
from console_tools.control_commands import _save
from ui.studio_api import StudioAPI
from vtuber_config import VTuberConfig
from vtuber_service import VTuberRuntime, create_app


class ConfigurationUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / "config.yaml"
        self.config = AppConfig()
        self.config.media.delivery_max_file_mb = 20
        self.config.save(str(self.path))
        self.client = SimpleNamespace(app_config=AppConfig.load(str(self.path)), config_path=str(self.path))

    def test_studio_and_chat_saves_preserve_each_others_fields(self):
        api = StudioAPI(self.home)
        response = api.save_config({"media": {"delivery_max_file_mb": 77}}, config_revision(self.path))
        self.assertTrue(response["ok"], response)
        _save(self.client, self.client.app_config.bot, "reply_char_limit", 333)
        self.assertEqual(AppConfig.load(str(self.path)).media.delivery_max_file_mb, 77)
        self.assertEqual(self.client.app_config.media.delivery_max_file_mb, 77)
        stale = response["data"]["revision"]
        self.assertFalse(api.save_config({"voice": {"enabled": True}}, stale)["ok"])
        response = api.save_config({"voice": {"enabled": True}}, config_revision(self.path))
        self.assertTrue(response["ok"], response)
        saved = AppConfig.load(str(self.path))
        self.assertEqual(saved.bot.reply_char_limit, 333)
        self.assertTrue(saved.voice.enabled)

    def test_parallel_partial_updates_do_not_lose_unrelated_fields(self):
        barrier = threading.Barrier(2)
        def save(changes):
            barrier.wait(timeout=5)
            AppConfig.update_fields(self.path, changes)
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(save, changes) for changes in (
                {"bot": {"reply_char_limit": 450}}, {"media": {"delivery_max_file_mb": 81}})]
            for task in tasks:
                task.result(timeout=15)
        saved = AppConfig.load(str(self.path))
        self.assertEqual(saved.bot.reply_char_limit, 450)
        self.assertEqual(saved.media.delivery_max_file_mb, 81)

    def test_group_setting_keeps_newer_settings_of_other_groups(self):
        group = GroupCustomConfig(group_id="first")
        self.client.app_config.bot.group_settings["first"] = group
        self.client.app_config.save(str(self.path))
        latest = AppConfig.load(str(self.path))
        latest.bot.group_settings["other"] = GroupCustomConfig(group_id="other", persona="newer")
        latest.save(str(self.path))
        _save(self.client, group, "persona", "selected")
        saved = AppConfig.load(str(self.path))
        self.assertEqual(saved.bot.get_group_config("first").persona, "selected")
        self.assertEqual(saved.bot.get_group_config("other").persona, "newer")

    def test_context_read_of_missing_config_has_no_write_or_permission_side_effect(self):
        from console_tools.broker import ConsoleBroker
        from voice_live import current_voice
        missing = self.home / "missing.yaml"
        client = SimpleNamespace(app_config=AppConfig(), config_path=str(missing))
        client.app_config.bot.master_id = "owner"
        client.app_config.bot.self_activity_enabled = True
        current_voice(client)
        broker = ConsoleBroker(self.home / "broker.sqlite3")
        context = broker.issue_context({"namespace": "fixture", "scope": "fixture",
            "voice_config_path": str(missing), "voice": {"enabled": False}}, self.home / "contexts")
        _, token = ConsoleBroker.from_context(context)
        broker.context(token)
        current_voice(client)
        self.assertFalse(missing.exists())
        self.assertEqual(client.app_config.bot.master_id, "owner")
        self.assertTrue(client.app_config.bot.self_activity_enabled)

    def test_failed_save_does_not_mutate_live_configuration(self):
        with patch.object(AppConfig, "save", side_effect=OSError("fixture disk failure")):
            with self.assertRaises(OSError):
                _save(self.client, self.client.app_config.bot, "reply_char_limit", 333)
        self.assertEqual(self.client.app_config.bot.reply_char_limit, 0)
        self.assertEqual(AppConfig.load(str(self.path)).bot.reply_char_limit, 0)


class ModelServiceReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("first", "second"):
            folder = self.root / name
            folder.mkdir()
            (folder / (name + ".model3.json")).write_text(json.dumps({"FileReferences": {
                "Moc": "model.moc3", "Textures": ["texture.png"],
                "Expressions": [{"Name": "happy", "File": "happy.exp3.json"}],
                "Motions": {"Idle": [{"File": "idle.motion3.json", "Sound": "sound.wav"}]}}}), encoding="utf-8")
            for asset in ("model.moc3", "texture.png", "happy.exp3.json", "idle.motion3.json", "sound.wav"):
                (folder / asset).write_bytes(b"fixture")
        self.config = VTuberConfig(model_path=str(self.root / "first/first.model3.json"),
            credentials_file=str(self.root / "first/credentials.json"),
            vts_token_file=str(self.root / "first/vts-token.json"),
            api_token_env="QQBOT_REVIEW_FIXTURE_TOKEN", obs_password_env="QQBOT_REVIEW_FIXTURE_OBS")
        self.runtime = VTuberRuntime(self.config)
        self.server = TestServer(create_app(self.runtime))
        await self.server.start_server()
        self.config.port = self.server.port
        self.session = ClientSession(trust_env=False)

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()
        self.temp.cleanup()

    async def test_model_resources_are_public_but_unreferenced_json_is_not(self):
        (self.root / "first/unrelated.json").write_text('{"secret":"fixture"}', encoding="utf-8")
        for name in ("first.model3.json", "model.moc3", "texture.png", "happy.exp3.json", "idle.motion3.json", "sound.wav"):
            async with self.session.get(self.server.make_url("/model/" + name)) as response:
                self.assertEqual(response.status, 200, name)
        for name in ("credentials.json", "unrelated.json", "../second/second.model3.json"):
            async with self.session.get(self.server.make_url("/model/" + name)) as response:
                self.assertEqual(response.status, 404, name)
        async with self.session.get(self.server.make_url("/api/status")) as response:
            self.assertEqual(response.status, 401)

    async def test_private_credentials_cannot_be_granted_by_a_model_reference(self):
        model = Path(self.config.model_path)
        data = json.loads(model.read_text(encoding="utf-8"))
        data["FileReferences"]["UserData"] = "credentials.json"
        model.write_text(json.dumps(data), encoding="utf-8")
        async with self.session.get(self.server.make_url("/model/credentials.json")) as response:
            self.assertEqual(response.status, 404)

    async def test_reusing_service_applies_model_and_obs_changes_once(self):
        from vtuber_host import ensure_server
        changed = self.config.model_copy(update={"model_path": str(self.root / "second/second.model3.json"),
                                                 "obs_scene": "New fixture scene"})
        with patch.object(self.runtime, "configure", wraps=self.runtime.configure) as configure:
            result = await asyncio.to_thread(ensure_server, changed)
            self.assertFalse(result["started"])
            self.assertEqual(result["status"]["model"]["name"], "second")
            self.assertEqual(result["status"]["obs"]["scene"], "New fixture scene")
            await asyncio.to_thread(ensure_server, changed)
            self.assertEqual(configure.call_count, 1)

    async def test_reuse_reports_restart_for_listener_policy_change(self):
        from vtuber_host import ensure_server
        changed = self.config.model_copy(update={"allow_remote": True})
        with self.assertRaisesRegex(RuntimeError, "Restart.*allow_remote"):
            await asyncio.to_thread(ensure_server, changed)
        self.assertFalse(self.runtime.config.allow_remote)

    async def test_bert_and_avatar_can_listen_at_the_same_time_with_defaults(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent / "emotion_studio"))
        from emotionlab.processes import ProcessManager
        from emotionlab.inference.server import make_server
        self.assertEqual(ProcessManager().analysis_port, 0)
        with make_server("unused-fixture", analyzer=SimpleNamespace()) as analysis:
            self.assertNotEqual(analysis.server_port, self.server.port)
            self.assertNotEqual(analysis.server_port, 0)


class ActorVoiceReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_actor_uses_its_independent_enabled_emotion_catalog(self):
        from agent_core.runtime import ActorRuntime
        from console_tools.broker import ConsoleBroker
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            catalog = home / "catalog.json"
            catalog.write_text(json.dumps({"version": 1, "characters": {"live": {
                "emotions": {"live-only": {"description": "fixture"}}}}}), encoding="utf-8")
            config = AppConfig.model_validate({"agent": {"actors": {"host": {"name": "Fixture"}}},
                "bilibili": {"enabled": True, "auto_reply": True, "actor_id": "host", "room_id": 101},
                "voice": {"enabled": False}, "broadcast_voice": {"enabled": True, "character": "live", "catalog_path": str(catalog)}})
            path = home / "config.yaml"
            config.save(str(path))
            captured = {}
            async def chat(key, prompt, **options):
                broker, token = ConsoleBroker.from_context(json.loads(prompt)["console_context_file"])
                captured.update(broker.context(token))
                return ""
            runtime = ActorRuntime(config.agent, database_path=home / "actors.sqlite3", ai=SimpleNamespace(chat=chat))
            runtime.broadcast_config_path = str(path)
            try:
                runtime.store.ingest("host", "fixture", "broadcast.audience_batch", {"events": []}, source_id="fixture")
                await runtime.tick()
                await asyncio.gather(*(task for _, task in runtime.model_tasks.values()))
                self.assertTrue(captured["voice"]["enabled"])
                self.assertEqual(captured["voice"]["emotions"], ["live-only"])
                self.assertEqual(captured["voice"]["pipeline"], "broadcast")
            finally:
                await runtime.close()
