"""Offline checks for reusable verification and durable, owned branch cleanup."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from antigravity_client import AntigravityClient
from media_models import MediaAsset
from visual_verification import VerificationCache, inspect_image


class VerificationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_verification_lifecycle_")
        self.root = Path(self.tmp.name)
        self.client = AntigravityClient(sessions_file=str(self.root / "sessions.json"))
        self.client._brain_dir = str(self.root / "brain")

    def tearDown(self):
        self.tmp.cleanup()

    def archive_target(self):
        key = "__vision__:__work__:task:fixture:view:owned"
        session = self.client.get_or_create_session(key)
        session.conversation_id = "fixture-owned-cid"
        path = self.root / "archive.json"
        path.write_text(json.dumps({"completed": False, "session_key": key, "response": ""}), encoding="utf-8")
        return key, session, path

    def transcript(self, rows):
        path = Path(self.client._brain_dir) / "fixture/.system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    def test_persisted_decision_requires_same_request_bytes_path_and_provenance(self):
        asset = MediaAsset(kind="image", title="chart", local_path=str(self.root / "chart.png"),
                           source_url="https://example.com/chart", sha256="hash-one")
        result = {"matched": True, "description": "an orange chart", "view_evidence": {
            "path": asset.local_path, "sha256": asset.sha256}}
        cache = VerificationCache(self.root)
        cache.put("find chart", asset, result)
        cache = VerificationCache(self.root)
        self.assertEqual(cache.get("find chart", asset), result)
        self.assertIsNone(cache.get("find photo", asset))
        for field, value in (("sha256", "hash-two"), ("source_url", "https://example.org/other"),
                             ("title", "other subject"), ("local_path", str(self.root / "other.png"))):
            changed = asset.model_copy(update={field: value})
            self.assertIsNone(cache.get("find chart", changed), field)
        cache.put("find chart", asset, {**result, "matched": False})
        self.assertEqual(cache.get("find chart", asset), result)

    async def test_failed_backend_cleanup_is_persisted_and_retried_after_restart(self):
        key, session, path = self.archive_target()
        self.client._save_sessions()
        self.client._archive_session_once = AsyncMock(return_value=False)
        self.assertFalse(await self.client.archive_session(key, str(path)))
        queue = self.client._archive_queue()
        self.assertEqual(queue[session.conversation_id]["attempts"], 1)
        restarted = AntigravityClient(sessions_file=str(self.root / "sessions.json"))
        async def remove(*args, **kwargs):
            self.assertEqual(kwargs["expected_id"], session.conversation_id)
            self.assertTrue(path.is_file())
            return True
        restarted._archive_session_once = AsyncMock(side_effect=remove)
        with patch("antigravity_client.time.time", return_value=time.time() + 301):
            await restarted.retry_archives()
        restarted._archive_session_once.assert_awaited_once()
        self.assertFalse(restarted._archive_queue())

    async def test_retry_drops_reused_conversation_instead_of_deleting_new_work(self):
        key, session, path = self.archive_target()
        self.client._archive_session_once = AsyncMock(return_value=False)
        await self.client.archive_session(key, str(path))
        self.client._archive_session_once.reset_mock()
        session.last_active += 1
        with patch("antigravity_client.time.time", return_value=time.time() + 301):
            await self.client.retry_archives()
        self.client._archive_session_once.assert_not_awaited()
        self.assertFalse(self.client._archive_queue())

    async def test_main_and_active_sessions_are_never_queued(self):
        key, session, path = self.archive_target()
        self.client._archive_session_once = AsyncMock(return_value=True)
        main = self.client.get_or_create_session("onebot_group_fixture")
        main.conversation_id = "main-chat-cid"
        self.assertFalse(await self.client.archive_session(main.session_key, str(path)))
        async with session.lock:
            self.assertFalse(await self.client.archive_session(key, str(path)))
        self.assertFalse(self.client._archive_queue())
        self.client._archive_session_once.assert_not_awaited()

    async def test_empty_done_waits_for_final_of_same_invocation(self):
        path = self.transcript([{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
                                 "status": "DONE", "content": ""}])
        async def complete():
            await asyncio.sleep(0.02)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE",
                    "status": "DONE", "content": "verified final"}) + "\n")
        completing = asyncio.create_task(complete())
        response = await self.client._wait_for_model_response("fixture", expected_step_min=1, timeout=1)
        await completing
        self.assertEqual(response, "verified final")

    async def test_cancelled_inspection_preserves_mapping_until_diagnostics_cleanup(self):
        picture = self.root / "fixture.png"
        Image.new("RGB", (24, 24), "orange").save(picture)
        self.client._is_ready = True
        self.client._run_agentapi_cmd = AsyncMock(return_value={"response": {
            "newConversation": {"conversationId": "cancelled-view-cid"}}})
        self.client._cancel_native_conversation = AsyncMock()
        entered = asyncio.Event()
        async def waiting(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.client._wait_for_model_response = waiting
        async def cleanup(key, archive, **kwargs):
            self.assertEqual(self.client._sessions[key].conversation_id, "cancelled-view-cid")
            payload = json.loads(Path(archive).read_text(encoding="utf-8"))
            self.assertFalse(payload["completed"])
            return True
        self.client._archive_session_once = AsyncMock(side_effect=cleanup)
        running = asyncio.create_task(inspect_image(self.client, "__vision__:task", picture, "describe"))
        await entered.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.client._cancel_native_conversation.assert_awaited_once_with("cancelled-view-cid")
        self.client._archive_session_once.assert_awaited_once()
        self.assertFalse(self.client._archive_queue())

    async def test_system_error_with_blank_done_is_reported_without_recovery_turn(self):
        self.transcript([
            {"step_index": 1, "type": "SYSTEM_ERROR", "status": "DONE"},
            {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": ""}])
        with self.assertRaisesRegex(RuntimeError, "服务终止"):
            await self.client._wait_for_model_response("fixture", expected_step_min=1, timeout=1)

    async def test_unchanging_empty_completion_reports_error_instead_of_placeholder(self):
        self.transcript([{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
                          "status": "DONE", "content": ""}])
        clock = time.monotonic()
        async def observe(*args, **kwargs):
            nonlocal clock
            clock += 6
        with patch("antigravity_client.time.monotonic", side_effect=lambda: clock), patch(
                "antigravity_client.asyncio.sleep", new=observe):
            with self.assertRaisesRegex(RuntimeError, "空回复"):
                await self.client._wait_for_model_response("fixture", expected_step_min=1, timeout=0)

    async def test_cleanup_preserves_full_trace_before_backend_removal(self):
        key, session, path = self.archive_target()
        self.client._is_ready = True
        self.client.ls_address, self.client.csrf_token = "127.0.0.1:1", "fixture"
        source = Path(self.client._brain_dir) / session.conversation_id / ".system_generated/logs/transcript.jsonl"
        source.parent.mkdir(parents=True)
        source.write_text('{"type":"SYSTEM_ERROR"}\n', encoding="utf-8")
        cid = session.conversation_id
        response = MagicMock()
        response.read = AsyncMock(return_value=b"")
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        http = MagicMock()
        def post(*args, **kwargs):
            self.assertTrue((path.parent / "conversations" / (cid + ".jsonl.gz")).is_file())
            self.assertEqual(kwargs["json"], {"cascadeId": cid})
            return context
        http.post.side_effect = post
        http_context = MagicMock()
        http_context.__aenter__ = AsyncMock(return_value=http)
        http_context.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=http_context):
            self.assertTrue(await self.client.archive_session(key, str(path)))
        self.assertIsNone(session.conversation_id)
        self.assertFalse(self.client._archive_queue())


if __name__ == "__main__":
    unittest.main()
