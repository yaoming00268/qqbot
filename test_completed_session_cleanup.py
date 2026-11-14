"""Offline regressions for terminal work/probe cleanup and startup recovery."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from antigravity_client import AntigravityClient


class CompletedSessionCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_completed_cleanup_")
        self.root = Path(self.tmp.name)
        self.ai = AntigravityClient(sessions_file=str(self.root / "sessions.json"))
        self.ai._brain_dir = str(self.root / "brain")
        self.ai._is_ready = True
        self.removed = []

        async def remove(key, archive, **options):
            self.assertTrue(json.loads(Path(archive).read_text(encoding="utf-8")))
            session = self.ai._sessions[key]
            self.assertEqual(session.conversation_id, options["expected_id"])
            self.removed.append(key)
            self.ai._invalidate(session)
            self.ai._save_sessions()
            return True
        self.ai._archive_session_once = AsyncMock(side_effect=remove)

    async def asyncTearDown(self):
        task = getattr(self.ai, "_archive_retry_task", None)
        if task:
            await task
        self.tmp.cleanup()

    def seed(self, key, cid="fixture-terminal-cid"):
        session = self.ai.get_or_create_session(key)
        session.conversation_id = cid
        self.ai._save_sessions()
        return session

    async def test_probe_success_removes_new_and_legacy_conversations(self):
        self.seed("__system_probe__", "fixture-legacy-probe")
        main = self.seed("onebot_group_fixture", "fixture-main-cid")
        async def chat(key, *args, **options):
            self.assertIsNone(self.ai.get_or_create_session(key).conversation_id)
            self.seed(key, "fixture-new-probe")
            return "OK"
        self.ai.chat = AsyncMock(side_effect=chat)
        self.assertEqual(await self.ai.probe(), (True, "OK"))
        self.assertEqual(len(self.removed), 2)
        self.assertFalse(any(s.conversation_id for k, s in self.ai._sessions.items() if k.startswith("__system_probe__")))
        self.assertEqual(main.conversation_id, "fixture-main-cid")
        self.assertFalse(self.ai._archive_queue())

    async def test_probe_failure_still_records_and_removes_conversation(self):
        async def chat(key, *args, **options):
            self.seed(key, "fixture-failed-probe")
            raise TimeoutError("fixture timeout")
        self.ai.chat = AsyncMock(side_effect=chat)
        ok, message = await self.ai.probe()
        self.assertFalse(ok)
        self.assertIn("fixture timeout", message)
        self.assertEqual(len(self.removed), 1)

    async def test_completed_work_is_queued_without_waiting_for_backend(self):
        key = "__work__:task:fixture"
        self.seed(key)
        owned = [key, "__vision__:" + key, "__vision__:" + key + ":view:leaf", "__native__:" + key]
        for i, child in enumerate(owned[1:]):
            self.seed(child, "fixture-child-cid-" + str(i))
        for child in ("onebot_group_fixture", "__work__:task:fixture-other", "__vision__:unrelated"):
            self.seed(child, "fixture-unrelated-" + child.replace(":", ""))
        archive = self.root / "result.json"
        archive.write_text('{"completed":true,"answer":"saved"}', encoding="utf-8")
        self.assertEqual(self.ai.queue_work_cleanup(key, str(archive), completed_at=time.time()), len(owned))
        self.ai._archive_session_once.assert_not_awaited()
        self.assertEqual({r["session_key"] for r in self.ai._archive_queue().values()}, set(owned))
        await self.ai.retry_archives()
        self.assertEqual(set(self.removed), set(owned))

    async def test_startup_evidence_never_deletes_resumed_or_locked_work(self):
        key = "__work__:task:fixture"
        session = self.seed(key)
        archive = self.root / "result.json"
        archive.write_text('{"completed":true}', encoding="utf-8")
        self.assertEqual(self.ai.queue_work_cleanup(key, str(archive), completed_at=session.last_active - 1), 0)
        async with session.lock:
            self.assertEqual(self.ai.queue_work_cleanup(key, str(archive), completed_at=time.time()), 0)
        self.assertFalse(self.ai._archive_queue())

    async def test_cancelled_probe_preserves_id_until_cleanup(self):
        self.ai._run_agentapi_cmd = AsyncMock(return_value={"response": {
            "newConversation": {"conversationId": "fixture-cancelled-probe"}}})
        self.ai._cancel_native_conversation = AsyncMock()
        entered = asyncio.Event()
        async def wait(*args, **options):
            entered.set()
            await asyncio.Event().wait()
        self.ai._wait_for_model_response = wait
        running = asyncio.create_task(self.ai.probe())
        await asyncio.wait_for(entered.wait(), 2)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.ai._cancel_native_conversation.assert_awaited_once_with("fixture-cancelled-probe")
        self.assertEqual(len(self.removed), 1)
        self.assertFalse(self.ai._archive_queue())

    async def test_failed_probe_cleanup_survives_account_switch_and_restart(self):
        self.seed("__system_probe__", "fixture-pending-probe")
        self.ai.chat = AsyncMock(return_value="OK")
        self.ai._archive_session_once = AsyncMock(return_value=False)
        self.assertEqual(await self.ai.probe(), (True, "OK"))
        self.assertTrue(self.ai._archive_queue())
        restarted = AntigravityClient(sessions_file=str(self.root / "sessions.json"))
        restarted._brain_dir = self.ai._brain_dir
        restarted._is_ready = True
        restarted.bind_account("fixture_account")
        main = restarted.get_or_create_session("onebot_group_fixture")
        main.conversation_id = "fixture-main-after-bind"
        restarted._save_sessions()
        previous = restarted._previous_cleanup_clients[0]
        async def remove(key, archive, **options):
            self.assertTrue(Path(archive).is_file())
            previous._invalidate(previous._sessions[key])
            previous._save_sessions()
            return True
        previous._archive_session_once = AsyncMock(side_effect=remove)
        with patch("antigravity_client.time.time", return_value=time.time() + 301):
            await restarted.retry_archives()
        previous._archive_session_once.assert_awaited_once()
        self.assertFalse(previous._archive_queue())
        self.assertFalse(restarted._previous_cleanup_clients)
        self.assertEqual(main.conversation_id, "fixture-main-after-bind")
        base = json.loads((self.root / "sessions.json").read_text(encoding="utf-8"))
        self.assertFalse(any(row["conversation_id"] for key, row in base.items() if key.startswith("__system_probe__")))

    async def test_repeated_cleanup_registration_preserves_retry_backoff(self):
        key = "__work__:task:backoff"
        self.seed(key)
        archive = self.root / "completed.json"
        archive.write_text('{"answer":"saved"}', encoding="utf-8")
        self.ai._archive_session_once = AsyncMock(return_value=False)
        await self.ai.archive_session(key, str(archive))
        before = self.ai._archive_queue()
        self.ai.queue_work_cleanup(key, str(archive))
        self.assertEqual(before, self.ai._archive_queue())

    async def test_switching_back_never_deletes_resumed_work_via_an_old_snapshot(self):
        self.ai.bind_account("first")
        key = "__work__:task:resumed-account"
        self.seed(key)
        archive = self.root / "completed.json"
        archive.write_text('{"answer":"saved"}', encoding="utf-8")
        self.ai.queue_work_cleanup(key, str(archive))
        self.ai.bind_account("second")
        self.assertEqual(len(self.ai._previous_cleanup_clients), 1)
        previous = self.ai._previous_cleanup_clients[0]
        previous._archive_session_once = AsyncMock(side_effect=AssertionError("stale cleanup owner"))
        self.ai.bind_account("first")
        session = self.ai._sessions[key]
        session.last_active += 1
        self.ai._save_sessions()
        await self.ai.retry_archives()
        previous._archive_session_once.assert_not_awaited()
        self.ai._archive_session_once.assert_not_awaited()
        self.assertEqual(session.conversation_id, "fixture-terminal-cid")
        self.assertFalse(self.ai._archive_queue())
        self.assertFalse(self.ai._previous_cleanup_clients)

    async def test_work_cleanup_evidence_survives_media_cache_expiry(self):
        key = "__work__:task:cache-expiry"
        session = self.seed(key)
        cid = session.conversation_id
        archive = self.root / "cache-result.json"
        archive.write_text('{"answer":"saved before delivery"}', encoding="utf-8")
        self.ai._archive_session_once = AsyncMock(return_value=False)
        self.ai.queue_work_cleanup(key, str(archive))
        await self.ai.retry_archives()
        queued = self.ai._archive_queue()[cid]
        self.assertNotEqual(Path(queued["archive"]), archive)
        archive.unlink()
        restarted = AntigravityClient(sessions_file=self.ai.sessions_file)
        async def remove(key, saved, **options):
            self.assertEqual(json.loads(Path(saved).read_text(encoding="utf-8"))["answer"], "saved before delivery")
            restarted._invalidate(restarted._sessions[key])
            restarted._save_sessions()
            return True
        restarted._archive_session_once = AsyncMock(side_effect=remove)
        with patch("antigravity_client.time.time", return_value=time.time() + 301):
            await restarted.retry_archives()
        self.assertFalse(restarted._archive_queue())
        self.assertIsNone(restarted._sessions[key].conversation_id)

    async def test_rpc_error_body_does_not_claim_backend_deletion(self):
        key = "__work__:task:rpc"
        session = self.seed(key)
        cid = session.conversation_id
        archive = self.root / "completed.json"
        archive.write_text('{"answer":"saved"}', encoding="utf-8")
        self.ai.ls_address, self.ai.csrf_token = "127.0.0.1:1", "fixture"
        self.ai._archive_session_once = AntigravityClient._archive_session_once.__get__(self.ai)
        response = MagicMock()
        response.read = AsyncMock(return_value=b'{"error":{"code":13,"message":"fixture delete failed"}}')
        request_context = MagicMock()
        request_context.__aenter__ = AsyncMock(return_value=response)
        request_context.__aexit__ = AsyncMock(return_value=False)
        http = MagicMock()
        http.post.return_value = request_context
        http_context = MagicMock()
        http_context.__aenter__ = AsyncMock(return_value=http)
        http_context.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=http_context):
            self.assertFalse(await self.ai.archive_session(key, str(archive)))
            self.assertEqual(session.conversation_id, cid)
            self.assertIn(cid, self.ai._archive_queue())
            self.assertFalse((archive.parent / "conversations" / (cid + ".json")).exists())
            response.read.return_value = b'{}'
            with patch("antigravity_client.time.time", return_value=time.time() + 301):
                await self.ai.retry_archives()
        self.assertIsNone(session.conversation_id)
        self.assertFalse(self.ai._archive_queue())


class CompletedWorkIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        for bot in self.fixture.bots:
            task = getattr(bot.ai, "_archive_retry_task", None)
            if task:
                await task
        await self.fixture.asyncTearDown()

    async def test_worker_cleanup_finishes_while_main_review_is_pending(self):
        bot, main_key = self.fixture.bot(stored_cid="fixture-persistent-main")
        unrelated_key = "__work__:task:unrelated-paused-work"
        unrelated = bot.ai.get_or_create_session(unrelated_key)
        unrelated.conversation_id = "fixture-unrelated-paused-cid"
        reviewed, allow_review, removed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def chat(key, prompt, **options):
            if key.startswith("__work__:task:"):
                session = bot.ai.get_or_create_session(key)
                session.conversation_id = "fixture-completed-worker"
                bot.ai._save_sessions()
                return json.dumps({"answer": "完整成果已保存", "files": [],
                                   "result": {"agent_session_key": unrelated_key}})
            if '"type": "task_result"' in prompt:
                reviewed.set()
                await allow_review.wait()
                return '{"should_reply":false}'
            return '{"should_reply":false,"tasks":[{"type":"task","task":"整理资料"}]}'
        async def remove(key, archive, **options):
            saved = json.loads(Path(archive).read_text(encoding="utf-8"))
            self.assertEqual(saved["answer"], "完整成果已保存")
            self.assertTrue(all(Path(row["local_path"]).is_file() for row in saved["files"]))
            bot.ai._invalidate(bot.ai._sessions[key])
            bot.ai._save_sessions()
            removed.set()
            return True
        bot.ai.chat.side_effect = chat
        bot.ai._archive_session_once = AsyncMock(side_effect=remove)
        try:
            await self.fixture.receive(bot, False, "cleanup-request", "整理资料")
            await asyncio.wait_for(asyncio.gather(reviewed.wait(), removed.wait()), 3)
            coordinator = bot._get_main_coordinator()
            with coordinator.broker.connect() as db:
                record = db.execute("SELECT state,result FROM requests WHERE kind='task'").fetchone()
            self.assertEqual(record["state"], "ready")
            result = json.loads(record["result"])["result"]
            self.assertTrue(Path(result["archive"]).is_file())
            self.assertIsNone(bot.ai._sessions[result["agent_session_key"]].conversation_id)
            self.assertNotEqual(result["agent_session_key"], unrelated_key)
            self.assertEqual(unrelated.conversation_id, "fixture-unrelated-paused-cid")
            self.assertEqual(bot.ai._sessions[main_key].conversation_id, "fixture-persistent-main")
            bot.send_action.assert_not_awaited()
        finally:
            allow_review.set()

    async def test_startup_recovers_only_completed_owned_branches(self):
        from console_tools.broker import ConsoleBroker
        bot, main_key = self.fixture.bot(stored_cid="fixture-main-survives-startup")
        coordinator = bot._get_main_coordinator()
        directory = coordinator.root / "cleanup-fixtures"
        directory.mkdir()
        context_file = coordinator.broker.issue_context({"namespace": coordinator.namespace,
            "scope": "fixture-scope", "source_message_id": "source", "session_key": main_key}, directory)
        _, token = ConsoleBroker.from_context(context_file)
        unrelated = bot.ai.get_or_create_session("__work__:task:unrelated-legacy")
        unrelated.conversation_id = "fixture-unrelated-legacy-cid"
        unrelated.last_active = time.time() - 30
        eligible = []
        for name, state, success in (("complete", "completed", True), ("ready", "ready", True),
                ("unknown", "unknown", True), ("failed", "completed", False), ("resumed", "ready", True),
                ("paused", "paused", None), ("sandbox", "completed", None)):
            receipt = coordinator.broker.submit(token, "task", name)
            key = "__work__:task:sandbox:" + receipt["id"] if success is None else "__work__:task:" + name
            session = bot.ai.get_or_create_session(key)
            session.conversation_id = "fixture-startup-" + name
            session.last_active = time.time() - 10
            archive = directory / (name + ".json")
            archive.write_text(json.dumps({"answer": name, "state": "paused" if name == "paused" else "ready"}), encoding="utf-8")
            if success is None:
                payload = {"agent_session_key": key, "full_result_file": str(archive)}
                with coordinator.broker.connect() as db:
                    db.execute("UPDATE requests SET kind='sandbox_play' WHERE id=?", (receipt["id"],))
            else:
                payload = {"success": success, "result": {"job_id": name,
                    "agent_session_key": "__work__:task:unrelated-legacy" if name == "ready" else key, "archive": str(archive)}}
            coordinator.broker.update(receipt["id"], state, payload)
            if name == "resumed":
                session.last_active = archive.stat().st_mtime + 10
            elif success or name == "sandbox":
                eligible.append(key)
        bot.ai._save_sessions()
        bot.ai.tick_archives = MagicMock()
        coordinator.prepare_startup()
        await asyncio.gather(*coordinator.startup_tasks.values())
        self.assertEqual({record["session_key"] for record in bot.ai._archive_queue().values()}, set(eligible))
        self.assertEqual(bot.ai._sessions[main_key].conversation_id, "fixture-main-survives-startup")
        self.assertEqual(unrelated.conversation_id, "fixture-unrelated-legacy-cid")
        bot.ai.chat.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
