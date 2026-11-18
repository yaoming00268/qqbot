"""Offline Bilibili service lifecycle, grants and receipt regression fixtures."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from broadcast_service import BroadcastService
from broadcast_commands import context_binding, operate, tick_client
from config import AppConfig


class FixtureAdapter:
    def __init__(self):
        self.connections = []
        self.disconnects = 0
        self.current = None

    def status(self):
        return {"sdk_ready": True, "credentials_present": True, "provider": "fixture"}

    def credential(self): return object()

    async def listen(self, room_id, on_event):
        signal = asyncio.Event()
        self.current = signal
        self.connections.append((room_id, on_event, signal))
        await signal.wait()

    async def disconnect(self):
        self.disconnects += 1
        if self.current: self.current.set()


def packet(text="fixture", timestamp=12345):
    return {"type": "DANMU_MSG", "data": {"cmd": "DANMU_MSG", "info": [
        [0, 1, 25, 16777215, timestamp], text, [42, "fixture-viewer"]]}}


class BroadcastLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-broadcast-fixture-")
        self.path = Path(self.temp.name) / "config.yaml"
        self.config = AppConfig.model_validate({"bilibili": {"enabled": True, "room_id": 101,
            "area_id": 2, "auto_listen": True, "batch_seconds": 1, "actor_id": "host", "auto_reply": True},
            "agent": {"enabled": True, "actors": {"host": {"name": "Fixture host"}}}})
        self.config.save(str(self.path))
        self.adapter = FixtureAdapter()
        self.service = BroadcastService(self.path, adapter=self.adapter)
        self.before_tasks = set(asyncio.all_tasks())

    async def asyncTearDown(self):
        await self.service.close()
        leaked = [task for task in asyncio.all_tasks() - self.before_tasks if task is not asyncio.current_task() and not task.done()]
        for task in leaked: task.cancel()
        await asyncio.gather(*leaked, return_exceptions=True)
        self.temp.cleanup()

    async def start(self):
        await self.service.start_listener()
        await asyncio.sleep(0)

    async def test_room_change_never_relabels_old_room_event_as_new_room(self):
        await self.start()
        old_callback = self.adapter.connections[0][1]
        self.config.bilibili.room_id = 202
        self.config.save(str(self.path))
        await self.service.execute("status")
        await old_callback(packet())
        events = self.service.store.events()
        self.assertFalse(any(row["body"]["room_id"] == 202 for row in events),
            "An event received by the room 101 listener was mislabeled as room 202")

    async def test_disable_eventually_disconnects_the_live_listener(self):
        await self.start()
        self.config.bilibili.enabled = False
        self.config.save(str(self.path))
        await self.service.execute("status")
        await asyncio.sleep(1.15)
        self.assertTrue(self.service.listener_task.done(), "Disabling Bilibili left its listener connected")

    async def test_restarting_a_finished_listener_does_not_orphan_flush_loop(self):
        await self.start()
        previous_flush = self.service.flush_task
        self.adapter.current.set()
        await self.service.listener_task
        await self.service.start_listener()
        await asyncio.sleep(0)
        self.assertTrue(previous_flush is self.service.flush_task or previous_flush.done(),
                        "Restart created an additional untracked flush loop")

    async def test_tick_recovers_a_stopped_listener_without_config_changes(self):
        client = SimpleNamespace(config_path=str(self.path), app_config=self.config)
        calls = []
        async def fake_thread(function, *args, **kwargs):
            return function(*args, **kwargs)
        def ensure(_path): return {"running": True, "started": False, "status": {"listener": "stopped"}}
        def execute(_path, operation, params):
            calls.append((operation, params))
            return {"listener": "connecting"}
        with patch("broadcast_host.ensure_server", ensure), patch("broadcast_host.execute", execute), patch("asyncio.to_thread", fake_thread):
            await tick_client(client)
            client._broadcast_check_after = 0
            await tick_client(client)
        self.assertEqual(len(calls), 2, "The second health tick did not reconnect the stopped listener")

    async def test_constructing_bind_candidate_does_not_recover_active_owner_receipt(self):
        draft = self.service.store.save_draft("dynamic", {"text": "Fixture draft"})
        self.service.store.claim("publish", {"draft_id": draft["id"]}, "inflight-fixture",
            draft_id=draft["id"], revision=draft["revision"])
        candidate = BroadcastService(self.path, adapter=FixtureAdapter())
        try:
            self.assertEqual(self.service.store.receipt("inflight-fixture")["state"], "running",
                "A non-owning candidate marked an active owner's receipt unknown before binding its port")
        finally: await candidate.close()

    async def test_overlay_expression_maps_the_public_expression_argument(self):
        from vtuber_service import VTuberRuntime
        from vtuber_config import VTuberConfig
        avatar = VTuberRuntime(VTuberConfig(credentials_file=str(Path(self.temp.name) / "avatar.json")))
        async def forward(_function, action, body, _config): return await avatar.execute(action, body)
        try:
            with patch("asyncio.to_thread", forward):
                result = await self.service.avatar({"cue": "expression", "expression": "smile"})
            self.assertIn("event_id", result)
        finally: await avatar.close()

    async def test_disabling_actor_engine_revokes_broadcast_actor_binding(self):
        self.config.agent.enabled = False
        self.config.save(str(self.path))
        self.assertEqual(context_binding(self.path, actor_id="host"), {},
                         "An actor remained authorized after agent.enabled was revoked")

    async def test_draft_version_claim_is_durable_and_does_not_replay(self):
        draft = self.service.store.save_draft("dynamic", {"text": "Fixture draft"})
        body = {"draft_id": draft["id"], "revision": draft["revision"]}
        self.assertTrue(self.service.store.claim("publish", body, "fixture-publish", draft_id=draft["id"], revision=draft["revision"]))
        self.assertFalse(self.service.store.claim("publish", body, "fixture-publish", draft_id=draft["id"], revision=draft["revision"]))
        with self.assertRaises(ValueError):
            self.service.store.claim("publish", body, "different-key", draft_id=draft["id"], revision=draft["revision"])

    async def test_actor_status_does_not_expose_owner_unpublished_drafts(self):
        self.service.store.save_draft("dynamic", {"text": "PRIVATE_OWNER_DRAFT"})
        context = {"main_agent": True, "broadcast_config_path": str(self.path), "broadcast_actor": "host",
            "actor_event": True, "actor_database": str(Path(self.temp.name) / "actor.sqlite3"),
            "actor_lease_owner": "fixture-lease", "is_master": False}
        broker = SimpleNamespace(context=lambda token: context)
        with patch("broadcast_host.execute", return_value=self.service.status()), patch("agent_core.store.ActorStore.owns_model_lease", return_value=True):
            result = operate(broker, "fixture-token", "status")
        self.assertNotIn("PRIVATE_OWNER_DRAFT", json.dumps(result), "Audience-triggered actor read an unpublished owner draft via status")

    async def test_actor_and_child_workers_cannot_publish_or_control_obs(self):
        from console_tools.contracts import capabilities
        context = {"main_agent": True, "broadcast_config_path": str(self.path), "broadcast_actor": "host",
            "actor_event": True, "actor_database": str(Path(self.temp.name) / "actor.sqlite3"),
            "actor_lease_owner": "fixture-lease", "is_master": False}
        broker = SimpleNamespace(context=lambda token: context)
        self.assertNotIn("publish", capabilities(context)["broadcast"])
        with self.assertRaises(PermissionError): operate(broker, "fixture-token", "publish", draft_id="arbitrary", revision=1)
        with patch("agent_core.store.ActorStore.owns_model_lease", return_value=True):
            with self.assertRaises(PermissionError): operate(broker, "fixture-token", "avatar", cue="obs.start_stream")
        for flag in ("agent_worker_id", "sandbox_session_id", "parent_request_id", "emotion_analysis"):
            self.assertNotIn("broadcast", capabilities({**context, flag: "fixture"}))

    async def test_unknown_publish_is_sanitized_and_not_replayed(self):
        self.config.bilibili.allow_publish = True
        self.config.save(str(self.path))
        self.adapter.publish_dynamic = AsyncMock(side_effect=RuntimeError("SESSDATA=PRIVATE_COOKIE"))
        draft = await self.service.execute("draft", {"broadcast_kind": "dynamic", "text": "Fixture publish"})
        params = {"draft_id": draft["id"], "revision": draft["revision"], "request_key": "fixture-network-uncertain"}
        result = await self.service.execute("publish", params)
        again = await self.service.execute("publish", params)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(again["state"], "unknown")
        self.assertEqual(self.adapter.publish_dynamic.await_count, 1)
        self.assertNotIn("PRIVATE_COOKIE", json.dumps(result))


if __name__ == "__main__": unittest.main()
