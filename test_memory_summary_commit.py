"""Regression coverage for truncated responses, runaway summaries and lost commits."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from antigravity_client import AntigravityClient
from group_memory import GroupMemoryManager
from idle_memory import IdleMemory
from memory_digest import summarize_records
from test_agent_reply_latency import inbound, final
from test_memory_efficiency import summarizer


class SummaryCommitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkpoint = self.root / "summary.work.json"
        self.ai, _ = summarizer()

    async def fold(self, records, **options):
        return await summarize_records(self.ai, "group_summary_fixture", records,
            checkpoint_path=self.checkpoint, **options)

    async def test_completed_batch_is_loaded_from_fixed_file_after_restart_without_calling_model(self):
        records = [{"content": "群友讨论了部署计划"}]
        first = await self.fold(records)
        self.ai.chat = AsyncMock(side_effect=AssertionError("must use saved summary"))
        second = await self.fold(records)
        self.assertTrue(first["complete"] and second["complete"])
        self.assertEqual(first["digest"], second["digest"])
        self.ai.chat.assert_not_awaited()
        self.assertEqual(len(list(self.root.glob("*.work.json"))), 1)

    async def test_partial_success_is_committed_and_only_unfinished_batch_can_be_retried(self):
        records = [{"content": str(number) + "长" * 8000} for number in range(3)]
        original = self.ai.chat.side_effect
        calls = 0
        async def fails_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("offline")
            return await original(*args, **kwargs)
        self.ai.chat.side_effect = fails_second
        result = await self.fold(records)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["chunks"]), 1)
        self.ai.chat.reset_mock()
        for _ in range(4):
            self.assertFalse((await self.fold(records))["complete"])
        self.ai.chat.assert_not_awaited()
        self.ai.chat.side_effect = original
        result = await self.fold(records, retry=True)
        self.assertTrue(result["complete"])
        self.assertEqual(self.ai.chat.await_count, 2)

    async def test_running_checkpoint_is_durable_before_generation_and_cancel_does_not_loop(self):
        entered = asyncio.Event()
        async def stalled(*args, **kwargs):
            state = json.loads(self.checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(next(iter(state["requests"].values()))["status"], "running")
            entered.set()
            await asyncio.Event().wait()
        self.ai.chat.side_effect = stalled
        task = asyncio.create_task(self.fold([{"content": "cannot lose this"}]))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.ai.chat.reset_mock()
        result = await self.fold([{"content": "cannot lose this"}])
        self.assertFalse(result["complete"])
        self.ai.chat.assert_not_awaited()

    async def test_cannot_write_checkpoint_means_no_model_call(self):
        with patch("memory_digest.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.fold([{"content": "source"}])
        self.ai.chat.assert_not_awaited()
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    async def test_corrupt_checkpoint_does_not_silently_repeat_generation(self):
        self.checkpoint.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            await self.fold([{"content": "source"}])
        self.ai.chat.assert_not_awaited()

    async def test_run_summary_updates_fixed_paths_and_clear_removes_its_checkpoints(self):
        manager = GroupMemoryManager(str(self.root / "groups"))
        manager.record_session_message("123", "a", "A", "第一条")
        first = await manager.summarize_and_save_session(self.ai)
        manager.record_session_message("123", "b", "B", "第二条")
        second = await manager.summarize_and_save_session(self.ai)
        self.assertEqual(first["123"], second["123"])
        summary = Path(manager.storage_dir) / "group_123.summary.json"
        self.assertIn("第一条", summary.read_text(encoding="utf-8"))
        self.assertIn("第二条", summary.read_text(encoding="utf-8"))
        checkpoint = manager.run_memory.reference("123").with_suffix(".work.json")
        self.assertTrue(checkpoint.is_file())
        manager.clear_group_memory("123")
        self.assertFalse(summary.exists())
        self.assertFalse(checkpoint.exists())


class SummaryInvocationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ai = AntigravityClient(sessions_file=str(root / "sessions.json"))
        self.ai._is_ready = True
        self.ai._brain_dir = str(root / "brain")
        self.ai._cancel_native_conversation = AsyncMock()
        self.key = "activity_summary_fixture"
        self.session = self.ai.get_or_create_session(self.key)
        self.session.conversation_id = "fixture"
        self.path = root / "brain/fixture/.system_generated/logs/transcript.jsonl"
        self.path.parent.mkdir(parents=True)

    async def test_first_full_json_is_returned_and_generation_stops_before_autonomous_continuations(self):
        expected = json.dumps({"summary": "完整的长摘要" * 500}, ensure_ascii=False)
        async def command(args, **options):
            marker = args[-1].splitlines()[0]
            clipped = final(2, expected[:80] + "[truncated]")
            clipped["truncated_fields"] = ["content"]
            self.path.write_text("\n".join(json.dumps(row) for row in [inbound(1, marker), clipped]), encoding="utf-8")
            full = [inbound(1, marker), final(2, expected), final(3, "不应读取的自我续写")]
            self.path.with_name("transcript_full.jsonl").write_text("\n".join(json.dumps(row) for row in full), encoding="utf-8")
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        result = await self.ai.chat(self.key, "summary", single_response=True, bind_response=True, timeout=1, raise_on_error=True)
        self.assertEqual(result, expected)
        self.ai._run_agentapi_cmd.assert_awaited_once()
        self.ai._cancel_native_conversation.assert_awaited_once_with("fixture")

    async def test_truncated_only_response_fails_and_cancels_without_asking_model_to_rewrite(self):
        async def command(args, **options):
            clipped = final(2, '{"summary":"truncated')
            clipped["truncated_fields"] = ["content"]
            self.path.write_text("\n".join(json.dumps(row) for row in [inbound(1, args[-1].splitlines()[0]), clipped]), encoding="utf-8")
            return {}
        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        with self.assertRaisesRegex(RuntimeError, "截断"):
            await self.ai.chat(self.key, "summary", single_response=True, bind_response=True, timeout=1, raise_on_error=True)
        self.ai._run_agentapi_cmd.assert_awaited_once()
        self.ai._cancel_native_conversation.assert_awaited_once_with("fixture")


class IdleMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.memory = IdleMemory(self.root, 0)

    def append(self, text):
        with self.memory.journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": text, "generation": 0}) + "\n")

    def test_empty_idle_has_no_work_and_completed_cursor_survives_restart(self):
        self.assertFalse(self.memory.pending())
        self.append("one")
        snapshot, _, records = self.memory.begin()
        self.memory.finish(snapshot, {"summary": "first"}, "main-cid")
        self.assertFalse(IdleMemory(self.root, 0).pending())
        self.append("two")
        snapshot, previous, records = IdleMemory(self.root, 0).begin()
        self.assertEqual([row["content"] for row in records], ["two"])
        self.assertEqual(previous["summary"], "first")
        self.memory.finish(snapshot, {"summary": "both"}, "main-cid")
        self.assertEqual(self.memory.path, (self.root / "memory.json").resolve())
        self.assertEqual(len(list(self.root.glob("memory.json"))), 1)

    def test_failed_and_interrupted_snapshot_are_not_retried_by_idle_timer_or_restart(self):
        self.append("one")
        snapshot, _, _ = self.memory.begin()
        self.memory.fail(snapshot)
        self.assertIsNone(IdleMemory(self.root, 0).begin())
        self.append("two")
        self.assertTrue(IdleMemory(self.root, 0).pending())

    def test_new_event_during_summary_remains_pending_after_snapshot_commits(self):
        self.append("one")
        snapshot, _, _ = self.memory.begin()
        self.append("two")
        self.memory.finish(snapshot, {"summary": "first"}, "main-cid")
        self.assertTrue(self.memory.pending())
        self.assertEqual(self.memory.begin()[2][0]["content"], "two")

    def test_rewritten_log_does_not_reuse_wrong_source_cursor(self):
        self.append("one")
        snapshot, _, _ = self.memory.begin()
        self.memory.finish(snapshot, {"summary": "old"}, "main-cid")
        self.memory.journal.write_text('{"event":"new","generation":0}\n', encoding="utf-8")
        _, previous, records = self.memory.begin()
        self.assertEqual(previous, {})
        self.assertEqual(records[0]["content"], "new")
