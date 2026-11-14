"""Offline lifecycle coverage: recurring archives must not create recurring agents."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from activity_memory import summarize_activity
from antigravity_client import AntigravityClient
from group_memory import GroupMemoryManager
from idle_memory import IdleMemory
from memory_digest import SUMMARY_RULES, summarize_records
from sandbox_runtime import SandboxRuntime
from test_desktop_control import FrameBackend


class OrganizerBackend:
    """Model-free transport recording real new/send calls and invocation policy."""

    def __init__(self):
        self.calls = []
        self.payloads = {}
        self.created = []

    def attach(self, ai, root):
        ai._is_ready = True
        ai._brain_dir = str(root / "brain")
        ai._last_transcript_step = MagicMock(return_value=0)
        ai._read_response_trace = MagicMock(return_value=[])
        ai._cancel_native_conversation = AsyncMock()
        ai.archive_session = AsyncMock(side_effect=AssertionError("organizers must be reused"))
        ai._run_agentapi_cmd = AsyncMock(side_effect=self.command)
        ai._wait_for_model_response = AsyncMock(side_effect=self.respond)
        return ai

    async def command(self, args, **options):
        self.calls.append((args, options))
        if args[0] == "new-conversation":
            cid = "organizer-fixture-" + str(len(self.created) + 1)
            self.created.append(cid)
            if options.get("on_created"):
                options["on_created"](cid)
            result = {"response": {"newConversation": {"conversationId": cid}}}
        else:
            assert args[0] == "send-message", args[0]
            cid, result = args[1], {}
        self.payloads[cid] = json.loads(args[-1].splitlines()[-1])
        return result

    async def respond(self, conversation_id, **options):
        data = self.payloads[conversation_id]
        content = [row.get("content", row.get("text", ""))[:100] for row in data["records"]]
        text = " ".join([data["previous"].get("summary", ""), *content]).strip()
        return json.dumps({"summary": text or "已记录进度"}, ensure_ascii=False)


class MemoryOrganizerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot-organizer-lifecycle-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backend = OrganizerBackend()
        self.path = self.root / "sessions.json"
        self.ai = self.client()

    def client(self, account=""):
        ai = AntigravityClient(sessions_file=str(self.path), account_key=account,
                               session_timeout_minutes=1)
        return self.backend.attach(ai, self.root)

    def persisted(self, ai=None):
        return json.loads(Path((ai or self.ai).sessions_file).read_text(encoding="utf-8"))

    async def test_periodic_group_archive_reuses_one_id_after_idle_and_process_restart(self):
        manager = GroupMemoryManager(str(self.root / "memory"))
        manager.session_namespace = "::persona:one"
        key = "group_summary_123::persona:one"
        for number in range(6):
            if number == 3:
                self.ai = self.client()
            manager.record_session_message("123", "a", "甲", "归档话题" + str(number))
            saved = await manager.summarize_and_save_session(self.ai)
            value = json.loads(Path(saved["123"]).read_text(encoding="utf-8"))
            self.assertTrue(value["summary_complete"])
            self.assertIn("归档话题0", value["summary"])
            self.assertIn("归档话题" + str(number), value["summary"])
            session = self.ai.get_or_create_session(key)
            self.assertEqual(session.conversation_id, "organizer-fixture-1")
            session.last_active = 1
            self.ai._save_sessions()
            self.ai.archive_session.assert_not_awaited()
        self.assertEqual(len(self.backend.created), 1)
        self.assertEqual([call[0][0] for call in self.backend.calls], ["new-conversation"] + ["send-message"] * 5)
        self.assertFalse(self.ai._archive_queue())

    async def test_long_archive_uses_one_organizer_and_sends_rules_only_once(self):
        records = [{"content": str(n) + "长" * 8000} for n in range(3)]
        result = await summarize_records(self.ai, "group_summary_many", records,
                                          checkpoint_path=self.root / "batches.work.json")
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["chunks"]), 3)
        self.assertEqual(len(self.backend.created), 1)
        self.assertIn(SUMMARY_RULES, self.backend.calls[0][0][-1])
        for args, options in self.backend.calls:
            planner = options["cascade_config"]["plannerConfig"]
            self.assertTrue(planner["toolConfig"]["forceDisableToolCalling"])
            self.assertFalse(planner["toolConfig"]["invokeSubagent"]["enabled"])
            self.assertEqual(planner["customizationConfig"]["mcpServers"], [])
            self.assertTrue(all(not value["inheritUser"] for value in
                planner["customizationDiscoveryConfig"].values() if isinstance(value, dict) and "inheritUser" in value))
            if args[0] == "send-message":
                self.assertNotIn(SUMMARY_RULES, args[-1])

    async def test_expired_organizer_ids_and_rules_state_survive_restart(self):
        keys = ["group_summary_123", "group_summary_123::persona:one",
                "group_summary_idle_abc", "activity_summary_target"]
        rules_hash = hashlib.sha256(SUMMARY_RULES.encode()).hexdigest()
        self.path.write_text(json.dumps({key: {"conversation_id": "owned-summary-" + str(n),
            "updated_at": 1, "generation": 4, "chat_state": {"summary_rules_hash": rules_hash}}
            for n, key in enumerate(keys)}), encoding="utf-8")
        self.ai = self.client()
        for n, key in enumerate(keys):
            session = self.ai.get_or_create_session(key)
            self.assertEqual(session.conversation_id, "owned-summary-" + str(n))
            self.assertEqual(session.generation, 4)
            self.assertEqual(session.chat_state["summary_rules_hash"], rules_hash)
            result = await summarize_records(self.ai, key, [{"content": "新事实"}])
            self.assertTrue(result["complete"])
        self.assertEqual(self.backend.created, [])
        self.assertTrue(all(args[0] == "send-message" for args, _ in self.backend.calls))

    async def test_cancelled_batch_keeps_id_and_retries_only_when_explicitly_requested(self):
        key = "group_summary_idle_cancelled"
        checkpoint = self.root / "cancelled.work.json"
        await summarize_records(self.ai, key, [{"content": "已有事实"}])
        entered = asyncio.Event()

        async def stalled(**options):
            entered.set()
            await asyncio.Event().wait()

        self.ai._wait_for_model_response.side_effect = stalled
        records = [{"content": "取消期间保留的资料"}]
        task = asyncio.create_task(summarize_records(self.ai, key, records, checkpoint_path=checkpoint))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.persisted()[key]["conversation_id"], "organizer-fixture-1")
        self.assertEqual(self.ai.get_or_create_session(key).generation, 0)
        journal = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(next(iter(journal["requests"].values()))["status"], "interrupted")
        count = len(self.backend.calls)
        self.ai = self.client()
        self.assertFalse((await summarize_records(self.ai, key, records, checkpoint_path=checkpoint))["complete"])
        self.assertEqual(len(self.backend.calls), count)
        self.assertTrue((await summarize_records(self.ai, key, records,
            checkpoint_path=checkpoint, retry=True))["complete"])
        self.assertEqual(len(self.backend.created), 1)

    async def test_outer_timeout_preserves_id_and_does_not_repeat_failed_batch(self):
        key = "activity_summary_timeout"
        checkpoint = self.root / "timeout.work.json"
        await summarize_records(self.ai, key, [{"text": "之前的观察"}])

        async def stalled(**options):
            await asyncio.Event().wait()

        self.ai._wait_for_model_response.side_effect = stalled
        records = [{"text": "超时前观察"}]
        self.assertFalse((await summarize_records(self.ai, key, records,
            timeout=0.1, checkpoint_path=checkpoint))["complete"])
        self.assertEqual(self.persisted()[key]["conversation_id"], "organizer-fixture-1")
        self.ai = self.client()
        self.assertFalse((await summarize_records(self.ai, key, records, checkpoint_path=checkpoint))["complete"])
        self.assertTrue((await summarize_records(self.ai, key, records,
            checkpoint_path=checkpoint, retry=True))["complete"])
        self.assertEqual(len(self.backend.created), 1)

    async def test_failed_json_keeps_organizer_and_new_records_can_continue(self):
        key = "group_summary_bad_json"
        checkpoint = self.root / "invalid.work.json"
        self.ai._wait_for_model_response.side_effect = None
        self.ai._wait_for_model_response.return_value = "invalid summary"
        records = [{"content": "旧资料"}]
        self.assertFalse((await summarize_records(self.ai, key, records, checkpoint_path=checkpoint))["complete"])
        count = len(self.backend.calls)
        self.assertFalse((await summarize_records(self.ai, key, records, checkpoint_path=checkpoint))["complete"])
        self.assertEqual(len(self.backend.calls), count)
        self.ai._wait_for_model_response.side_effect = self.backend.respond
        self.assertTrue((await summarize_records(self.ai, key, [{"content": "新资料"}],
            checkpoint_path=checkpoint))["complete"])
        self.assertEqual(len(self.backend.created), 1)

    async def test_upgrade_retires_pending_summary_deletion_without_backend_or_data_loss(self):
        key, cid = "group_summary_123", "owned-old-summary"
        session = self.ai.get_or_create_session(key)
        session.conversation_id, session.last_active = cid, 1
        self.ai._save_sessions()
        source = self.root / "saved-summary.json"
        source.write_text('{"summary":"本地归档必须保留"}', encoding="utf-8")
        record = {"session_key": key, "conversation_id": cid, "last_active": 1,
                  "archive": str(source), "attempts": 3, "next_attempt": 0}
        self.ai._save_archive_queue({cid: record})
        self.ai = self.client()
        self.ai._is_ready = False
        self.ai.initialize = MagicMock(side_effect=AssertionError("no backend access"))
        self.ai._archive_session_once = AsyncMock(side_effect=AssertionError("do not delete organizer"))
        await self.ai.retry_archives()
        self.assertFalse(self.ai._archive_queue())
        self.ai._archive_session_once.assert_not_awaited()
        self.ai.initialize.assert_not_called()
        self.assertEqual(self.persisted()[key]["conversation_id"], cid)
        self.assertIn("本地归档必须保留", source.read_text(encoding="utf-8"))

    async def test_account_persona_and_source_ids_remain_independent(self):
        keys = ["group_summary_123::persona:one", "group_summary_123::persona:two",
                "group_summary_456::persona:one", "group_summary_idle_one", "activity_summary_target"]
        owners = []
        for account in ("first", "second"):
            ai = self.client(account)
            for key in keys:
                for n in range(2):
                    result = await summarize_records(ai, key, [{"content": account + key + str(n)}])
                    self.assertTrue(result["complete"])
                owners.append(ai.get_or_create_session(key).conversation_id)
            self.assertEqual(len(self.persisted(ai)), len(keys))
        self.assertEqual(len(set(owners)), len(keys) * 2)
        self.assertEqual(len(self.backend.created), len(keys) * 2)
        for data in self.backend.payloads.values():
            self.assertEqual(data["previous"], {})

    async def test_concurrent_batches_share_lock_and_create_only_one_organizer(self):
        key = "group_summary_concurrent"
        active, maximum = 0, 0

        async def delayed(**options):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            result = await self.backend.respond(**options)
            active -= 1
            return result

        self.ai._wait_for_model_response.side_effect = delayed
        results = await asyncio.gather(*(summarize_records(self.ai, key,
            [{"content": "并发资料" + str(n)}], checkpoint_path=self.root / (str(n) + ".work.json"))
            for n in range(4)))
        self.assertTrue(all(result["complete"] for result in results))
        self.assertEqual(maximum, 1)
        self.assertEqual(len(self.backend.created), 1)
        self.assertEqual(len(self.backend.calls), 4)

    async def test_activity_batches_and_new_play_sessions_reuse_target_organizer(self):
        executable = self.root / "story.exe"
        executable.touch()
        context = {"namespace": "account", "scope": "group", "sender_id": "owner", "is_master": True,
                   "session_key": "onebot_group_123::persona:one", "source_text": str(executable)}
        runtime = SandboxRuntime(self.root / "sandbox", backend=FrameBackend())
        game = runtime.register(context, str(executable))
        for n in range(4):
            sid, _ = runtime.begin(context, game["id"], "request-" + str(n))
            observation = runtime.observe(context, sid)
            runtime.note(context, sid, "游戏剧情" + str(n), [observation["id"]], "plot")
            self.assertTrue(await summarize_activity(runtime, self.ai, context, sid, force=True))
            runtime.finish(context, sid, "paused", "稍后继续")
            self.ai.archive_session.assert_not_awaited()
            self.ai = self.client()
            runtime = SandboxRuntime(self.root / "sandbox", backend=FrameBackend())
        value = json.loads((runtime.root / "memories" / (game["id"] + ".json")).read_text(encoding="utf-8"))
        self.assertIn("游戏剧情0", value["summary"])
        self.assertIn("游戏剧情3", value["summary"])
        self.assertEqual(len(self.backend.created), 1)


class IdleOrganizerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import test_startup_participation as fixtures
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_repeated_idle_archives_keep_one_organizer_and_main_cid(self):
        bot, key = self.fixture.bot(stored_cid="main-fixture-id")
        backend = OrganizerBackend()
        backend.attach(bot.ai, Path.cwd())
        bot.ai.chat = AntigravityClient.chat.__get__(bot.ai)
        coordinator = bot._get_main_coordinator()
        generation = bot.ai.get_or_create_session(key).generation
        memory = IdleMemory(coordinator.directory(key), generation)
        for n in range(4):
            memory.append([{"id": str(n), "author_id": "a", "author_name": "甲", "content": "空闲话题" + str(n)}])
            await coordinator._recall(key, generation)
            self.assertFalse(memory.pending())
            self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "main-fixture-id")
        self.assertEqual(len(backend.created), 1)
        bot.ai.archive_session.assert_not_awaited()
        value = json.loads(memory.path.read_text(encoding="utf-8"))
        self.assertIn("空闲话题0", value["summary"])
        self.assertIn("空闲话题3", value["summary"])


class OrganizerCreationHandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot-organizer-handoff-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "sessions.json"
        self.ai = AntigravityClient(sessions_file=str(self.path))
        self.ai._is_ready = True
        self.ai.ls_address, self.ai.csrf_token = "127.0.0.1:1", "fixture"
        self.ai._brain_dir = str(self.root / "brain")
        self.ai._cancel_native_conversation = AsyncMock()
        self.ai._refresh_connection = AsyncMock(return_value=True)
        self.key, self.cid = "group_summary_handoff", "owned-started-organizer"
        self.methods = []
        self.entered = asyncio.Event()

    def http(self, mode):
        @asynccontextmanager
        async def post(url, **options):
            method = url.rsplit("/", 1)[-1]
            self.methods.append(method)
            status, data = 200, {}
            if method == "GetCascadeModelConfigData":
                data = {"clientModelConfigs": [{"label": "Gemini Flash", "modelId": "gemini-flash",
                    "modelOrAlias": {"model": "FIXTURE_MODEL"}}]}
            elif method == "StartCascade":
                data = {"cascadeId": self.cid}
            elif method == "SendUserCascadeMessage":
                # Ownership must already be durable before the first send.
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.assertEqual(saved[self.key]["conversation_id"], self.cid)
                self.entered.set()
                if mode == "cancel":
                    await asyncio.Event().wait()
                if mode == "rejected":
                    status, data = 400, {"message": "fixture policy rejected"}
                if mode == "refused":
                    connection = SimpleNamespace(host="127.0.0.1", port=1, ssl=False)
                    raise aiohttp.ClientConnectorError(connection, ConnectionRefusedError(10061, "fixture refused"))
            else:
                raise AssertionError(method)
            yield SimpleNamespace(status=status, text=AsyncMock(return_value=json.dumps(data)))

        @asynccontextmanager
        async def session(*args, **options):
            yield SimpleNamespace(post=post)

        return session

    async def check_failed_first_send(self, mode):
        records = [{"content": "第一轮待归档资料"}]
        checkpoint = self.root / "handoff.work.json"
        with patch("agent_transport.aiohttp.ClientSession", side_effect=self.http(mode)):
            task = asyncio.create_task(summarize_records(self.ai, self.key, records, checkpoint_path=checkpoint))
            if mode == "cancel":
                await asyncio.wait_for(self.entered.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                self.assertFalse((await task)["complete"])
        self.ai._refresh_connection.assert_not_awaited()
        self.assertEqual(self.methods.count("StartCascade"), 1)
        restarted = AntigravityClient(sessions_file=str(self.path))
        self.assertEqual(restarted.get_or_create_session(self.key).conversation_id, self.cid)
        backend = OrganizerBackend()
        backend.attach(restarted, self.root)
        self.assertTrue((await summarize_records(restarted, self.key, records,
            checkpoint_path=checkpoint, retry=True))["complete"])
        self.assertEqual(backend.created, [])
        self.assertEqual(backend.calls[0][0][:2], ["send-message", self.cid])

    async def test_rejected_first_send_does_not_lose_started_organizer(self):
        await self.check_failed_first_send("rejected")

    async def test_cancelled_first_send_does_not_lose_started_organizer(self):
        await self.check_failed_first_send("cancel")

    async def test_refused_first_send_does_not_create_another_organizer(self):
        await self.check_failed_first_send("refused")

