"""Behavioral regressions for token-efficient, durable, semantic memories."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from activity_memory import activity_memories, summarize_activity, flush_activity_memories
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute, main
from console_tools.compact import compact_receipt
from console_tools.server import handle
from group_memory import GroupMemoryManager
from memory_digest import read_memory_page, record_chunks
from reply_policy import reply_style
from sandbox_runtime import SandboxRuntime
from test_desktop_control import FrameBackend


def summarizer():
    prompts = []
    async def chat(key, prompt, **kwargs):
        data = json.loads(prompt.split("\n", 1)[1])
        prompts.append(data)
        before = data["previous"].get("summary", "")
        text = " ".join(row.get("content", row.get("text", ""))[:180] for row in data["records"])
        return json.dumps({"summary": (before + " " + text).strip(), "conclusions": ["讨论形成了结论"],
            "open_questions": ["仍需核对第二步"], "reflection": "配乐给人的感受与剧情分开"}, ensure_ascii=False)
    return SimpleNamespace(chat=AsyncMock(side_effect=chat), archive_session=AsyncMock(return_value=True)), prompts


class RunArchiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = GroupMemoryManager(str(self.root))
        self.ai, self.prompts = summarizer()

    async def test_periodic_archive_retains_early_topics_and_full_run_evidence(self):
        self.manager.record_session_message("123", "a", "小甲", "早上的部署决定")
        await self.manager.summarize_and_save_session(self.ai)
        self.manager.record_session_message("123", "b", "小乙", "晚上的故障修复")
        saved = await self.manager.summarize_and_save_session(self.ai)
        record = json.loads(Path(saved["123"]).read_text(encoding="utf-8"))
        self.assertIn("早上的部署决定", record["summary"])
        self.assertIn("晚上的故障修复", record["summary"])
        self.assertEqual(record["run_message_count"], 2)
        self.assertEqual(len(self.prompts[-1]["records"]), 1)
        raw = self.manager.run_memory.reference("123").with_suffix(".messages.jsonl")
        self.assertEqual(len(raw.read_text(encoding="utf-8").splitlines()), 2)
        manifest = self.manager.get_memory_file_manifest("123")
        self.assertIn(raw.resolve().as_posix(), [row["path"] for row in manifest["files"]])
        self.assertEqual(len([row for row in manifest["files"] if row["role"] == "archive"]), 1)
        self.assertEqual(len(self.manager.load_recent_memories("123", force_reload=True)), 1)
        calls = self.ai.chat.await_count
        await self.manager.summarize_and_save_session(self.ai)
        self.assertEqual(self.ai.chat.await_count, calls)

    async def test_failed_summary_requires_explicit_retry_after_restart_without_mixing_run_ids(self):
        old = self.manager.run_memory.id
        self.manager.record_session_message("123", "a", "小甲", "未完成归档的群友消息")
        self.ai.chat.side_effect = RuntimeError("暂时离线")
        saved = await self.manager.summarize_and_save_session(self.ai)
        self.assertFalse(json.loads(Path(saved["123"]).read_text(encoding="utf-8"))["summary_complete"])
        current = GroupMemoryManager(str(self.root))
        current.record_session_message("123", "b", "小乙", "重启后的新话题")
        ai, prompts = summarizer()
        await current.summarize_and_save_session(ai)
        self.assertEqual(len(prompts), 1)
        self.assertIn("重启后", prompts[0]["records"][0]["content"])
        self.assertEqual(prompts[0]["previous"], {})
        await current.summarize_and_save_session(ai, retry=True)
        self.assertEqual(len(prompts), 2)
        self.assertIn("未完成归档", prompts[1]["records"][0]["content"])
        self.assertFalse(current.run_memory.pending())
        archives = current.load_recent_memories("123", force_reload=True)
        self.assertEqual({row["run_id"] for row in archives}, {old, current.run_memory.id})

    async def test_startup_recovery_does_not_summarize_current_live_messages(self):
        self.manager.record_session_message("123", "a", "A", "旧运行未总结的内容")
        current = GroupMemoryManager(str(self.root))
        current.record_session_message("123", "b", "B", "本次刚收到的内容")
        await current.summarize_and_save_session(self.ai, recover_only=True)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("旧运行", self.prompts[0]["records"][0]["content"])
        self.assertEqual(len(current._session_messages["123"]), 1)
        self.assertEqual(current.run_memory.pending_groups(), ["123"])

    async def test_new_messages_during_summary_are_not_credited_to_old_snapshot(self):
        self.manager.record_session_message("123", "a", "A", "第一条")
        original = self.ai.chat.side_effect
        async def inject(*args, **kwargs):
            self.manager.record_session_message("123", "b", "B", "总结过程中收到的第二条")
            self.ai.chat.side_effect = original
            return await original(*args, **kwargs)
        self.ai.chat.side_effect = inject
        await self.manager.summarize_and_save_session(self.ai)
        self.assertEqual([row["content"] for row in self.manager.run_memory.snapshot("123")["records"]], ["总结过程中收到的第二条"])
        await self.manager.summarize_and_save_session(self.ai)
        self.assertIn("第二条", self.prompts[-1]["records"][0]["content"])

    async def test_clear_during_summary_cannot_resurrect_cleared_memory(self):
        self.manager.record_session_message("123", "a", "A", "将被清空的内容")
        original = self.ai.chat.side_effect
        async def clear(*args, **kwargs):
            self.manager.clear_group_memory("123")
            return await original(*args, **kwargs)
        self.ai.chat.side_effect = clear
        self.assertEqual(await self.manager.summarize_and_save_session(self.ai), {})
        self.assertEqual(self.manager.get_memory_file_manifest("123")["files"], [])
        self.assertEqual(self.manager.run_memory.pending(), [])

    def test_chunking_keeps_every_character_in_very_long_messages(self):
        text = "开头" + "长消息" * 12000 + "末尾"
        chunks = list(record_chunks([{"author_id": "a", "content": text}], budget=2000))
        self.assertGreater(len(chunks), 10)
        self.assertEqual("".join(row["content"] for chunk in chunks for row in chunk), text)
        self.assertTrue(all(len(json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))) < 2200 for chunk in chunks))


class PagedMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "memory.json"
        self.value = {"summary": "部署使用灰度发布。游戏剧情另行讨论。", "conclusions": ["部署需要回滚预案", "游戏第一章结束"],
            "recent_interactions": [{"user_msg": "RAW_SENTINEL" * 4000}],
            "participants": {str(i): {"nickname": "成员" + str(i)} for i in range(100)}}
        self.path.write_text(json.dumps(self.value, ensure_ascii=False), encoding="utf-8")
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.context = self.broker.issue_context({"namespace": "account", "scope": "group", "sender_id": "99", "source_message_id": "m",
            "session_key": "group_123", "memory_files": [str(self.path)], "is_master": True}, self.root / "contexts")

    def test_default_summary_excludes_raw_interactions_and_prioritizes_current_user(self):
        result = execute("read-memory", self.context, path=str(self.path))
        data = json.loads(result["text"])
        self.assertNotIn("RAW_SENTINEL", result["text"])
        self.assertIn("99", data["participants"])
        self.assertEqual(len(data["participants"]), 4)
        self.assertLess(len(result["text"]), self.path.stat().st_size / 20)

    def test_query_returns_relevant_fields_without_dumping_unrelated_topics(self):
        result = read_memory_page(self.path, query="部署")
        self.assertIn("回滚预案", result["text"])
        self.assertNotIn("游戏第一章结束", result["text"])
        self.assertNotIn("RAW_SENTINEL", result["text"])

    def test_raw_pages_reconstruct_the_exact_utf8_document(self):
        offset, pages = 0, []
        while True:
            page = read_memory_page(self.path, view="raw", offset=offset, max_chars=317)
            self.assertLessEqual(len(page["text"]), 317)
            pages.append(page["text"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual("".join(pages), self.path.read_text(encoding="utf-8"))

    def test_jsonl_is_not_silently_returned_as_a_summary(self):
        path = self.root / "messages.jsonl"
        path.write_text('{"content":"RAW_SENTINEL"}\n' * 30, encoding="utf-8")
        self.assertNotIn("RAW_SENTINEL", read_memory_page(path)["text"])
        self.assertIn("RAW_SENTINEL", read_memory_page(path, view="raw")["text"])

    def test_mcp_and_cli_share_validation_and_read_permissions(self):
        message = {"id": 1, "method": "tools/call", "params": {"name": "qqbot_read_memory", "arguments": {
            "context_file": self.context, "path": str(self.path), "max_chars": 256}}}
        result = handle(message)["result"]
        self.assertFalse(result["isError"])
        self.assertLessEqual(len(json.loads(result["content"][0]["text"])["text"]), 256)
        message["params"]["arguments"]["max_chars"] = True
        self.assertTrue(handle(message)["result"]["isError"])
        with self.assertRaises(PermissionError):
            execute("read-memory", self.context, path=str(self.root / "other.json"))

    def test_compact_cli_discovery_reduces_output_without_losing_capabilities(self):
        full = execute("context", self.context)
        result = compact_receipt("context", full)
        self.assertEqual(result["capabilities"], full["capabilities"])
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), len(json.dumps(full, ensure_ascii=False)) * .4)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            main(["context", "--context-file", self.context, "--compact"])
        self.assertNotIn("examples", json.loads(stream.getvalue()))


class ActivityMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.exe = self.root / "story.exe"
        self.exe.touch()
        self.context = {"namespace": "account", "scope": "group", "sender_id": "owner", "is_master": True,
                        "session_key": "onebot_group_123::persona:one", "source_text": str(self.exe)}
        self.runtime = SandboxRuntime(self.root / "sandbox", backend=FrameBackend())
        self.game = self.runtime.register(self.context, str(self.exe))
        self.sid, _ = self.runtime.begin(self.context, self.game["id"], "request")
        self.ai, self.prompts = summarizer()

    def note(self, text, kind="plot"):
        observation = self.runtime.observe(self.context, self.sid)
        self.runtime.note(self.context, self.sid, text, [observation["id"]], kind)

    async def test_cumulative_experiences_survive_new_sessions_and_preserve_sources(self):
        self.note("第一章：角色在森林相遇 https://example.com/chapter1")
        self.assertTrue(await summarize_activity(self.runtime, self.ai, self.context, self.sid, force=True))
        self.runtime.finish(self.context, self.sid, "paused", "停在第二章入口")
        self.sid, _ = self.runtime.begin(self.context, self.game["id"], "request2")
        self.note("第二章：角色找到了地图")
        self.assertTrue(await summarize_activity(self.runtime, self.ai, self.context, self.sid, force=True))
        memories = activity_memories(SandboxRuntime(self.root / "sandbox"), self.context)
        self.assertIn("第一章", memories[0]["summary"])
        self.assertIn("第二章", memories[0]["summary"])
        self.assertEqual(memories[0]["source"], "fictional_game")
        self.assertIn("https://example.com/chapter1", memories[0]["sources"])
        self.assertFalse(memories[0]["pending"])
        self.ai.archive_session.assert_not_awaited()
        self.assertEqual(len([row for row in self.prompts[-1]["records"] if row.get("text")]), 1)
        self.assertEqual(activity_memories(self.runtime, {**self.context, "scope": "other"}), [])
        self.assertEqual(activity_memories(self.runtime, {**self.context, "session_key": "onebot_group_123::persona:two"}), [])

    async def test_summary_failure_preserves_pending_notes_for_retry(self):
        self.note("不能丢弃的游戏剧情")
        self.ai.chat.side_effect = RuntimeError("offline")
        self.assertFalse(await summarize_activity(self.runtime, self.ai, self.context, self.sid, force=True))
        item = activity_memories(self.runtime, self.context)[0]
        self.assertTrue(item["pending"])
        self.assertFalse(item["summary_complete"])
        ai, _ = summarizer()
        self.assertFalse(await summarize_activity(self.runtime, ai, self.context, self.sid, force=True))
        ai.chat.assert_not_awaited()
        self.assertTrue(await summarize_activity(self.runtime, ai, self.context, self.sid, force=True, retry=True))
        self.assertFalse(activity_memories(self.runtime, self.context)[0]["pending"])

    async def test_periodic_summary_waits_for_batch_and_skips_unchanged_memory(self):
        self.note("第一条记录")
        self.assertFalse(await summarize_activity(self.runtime, self.ai, self.context, self.sid))
        self.ai.chat.assert_not_awaited()
        for number in range(7):
            self.note("后续记录" + str(number))
        self.assertTrue(await summarize_activity(self.runtime, self.ai, self.context, self.sid))
        count = self.ai.chat.await_count
        await summarize_activity(self.runtime, self.ai, self.context, self.sid, force=True)
        self.assertEqual(self.ai.chat.await_count, count)

    async def test_terminal_state_without_new_notes_does_not_resummarize_content(self):
        self.note("已经总结过的剧情")
        await summarize_activity(self.runtime, self.ai, self.context, self.sid, force=True)
        count = self.ai.chat.await_count
        self.runtime.finish(self.context, self.sid, "cancelled", "主人停止，留在第一章")
        await summarize_activity(self.runtime, self.ai, self.context, self.sid, force=True)
        self.assertEqual(self.ai.chat.await_count, count)
        self.assertEqual(activity_memories(self.runtime, self.context)[0]["state"], "cancelled")

    async def test_partial_failure_advances_only_completed_notes_and_restart_does_not_repeat(self):
        for number in range(9):
            self.note(str(number) + "观察" * 1390)
        original = self.ai.chat.side_effect
        attempts = 0
        async def fails_second(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise RuntimeError("offline")
            return await original(*args, **kwargs)
        self.ai.chat.side_effect = fails_second
        self.assertFalse(await summarize_activity(self.runtime, self.ai, self.context, self.sid, force=True))
        item = activity_memories(self.runtime, self.context)[0]
        self.assertTrue(Path(item["memory_file"]).is_file())
        self.assertGreater(item["pending_notes"], 0)
        self.assertLess(item["pending_notes"], 9)
        self.assertFalse(item["summary_complete"])
        self.ai.archive_session.assert_not_awaited()
        runtime = SandboxRuntime(self.root / "sandbox")
        ai, prompts = summarizer()
        for _ in range(3):
            self.assertFalse(await summarize_activity(runtime, ai, self.context, self.sid, force=True))
        ai.chat.assert_not_awaited()
        self.assertTrue(await summarize_activity(runtime, ai, self.context, self.sid, force=True, retry=True))
        self.assertFalse(activity_memories(runtime, self.context)[0]["pending"])
        self.assertEqual(sum(1 for payload in prompts for row in payload["records"] if row.get("text")), item["pending_notes"])

    async def test_missing_summary_file_is_recovered_from_database_without_model_call(self):
        self.note("已经概括并保存在数据库中的剧情")
        await summarize_activity(self.runtime, self.ai, self.context, self.sid, force=True)
        path = Path(activity_memories(self.runtime, self.context)[0]["memory_file"])
        expected = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
        ai, _ = summarizer()
        self.assertTrue(await summarize_activity(SandboxRuntime(self.root / "sandbox"), ai, self.context, self.sid, force=True))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)
        ai.chat.assert_not_awaited()

    async def test_recovery_excludes_previous_digest_and_its_source_observations(self):
        import time
        self.note("旧方向下不再可用的剧情")
        await summarize_activity(self.runtime, self.ai, self.context, self.sid, force=True)
        with self.runtime.connect() as db:
            db.execute("INSERT INTO session_recoveries VALUES(?,?,?,?,?,?,?,?,?,?)", (
                self.sid, 1, 0, "skip", "", "BLOCKED", '["https://example.com/blocked"]', 0, "requested", time.time() + 1))
        item = activity_memories(self.runtime, self.context)[0]
        self.assertFalse(item["summary_complete"])
        self.assertNotIn("旧方向", json.dumps(item, ensure_ascii=False))

    async def test_software_notes_keep_actual_source_urls(self):
        with self.runtime.connect() as db:
            db.execute("UPDATE games SET kind='software' WHERE id=?", (self.game["id"],))
        self.note("页面讨论了数据库回滚策略 https://example.com/database", kind="observation")
        await summarize_activity(self.runtime, self.ai, self.context, self.sid, force=True)
        item = activity_memories(self.runtime, self.context, "回滚")[0]
        self.assertEqual(item["source"], "software_observation")
        self.assertIn("https://example.com/database", item["sources"])

    async def test_startup_recovers_pending_experiences_from_durable_context(self):
        self.note("重启前实际看过的剧情")
        broker = ConsoleBroker(self.root / "console.sqlite3")
        context_file = broker.issue_context(self.context, self.root / "contexts")
        _, token = ConsoleBroker.from_context(context_file)
        with self.runtime.connect() as db:
            db.execute("UPDATE games SET source_token=? WHERE id=?", (token, self.game["id"]))
        console = SimpleNamespace(client=SimpleNamespace(ai=self.ai, app_config=SimpleNamespace(
            sandbox=SimpleNamespace(directory=str(self.runtime.root)))), broker=broker, namespace="account")
        await flush_activity_memories(console)
        self.assertIn("重启前", activity_memories(self.runtime, self.context)[0]["summary"])


class ReplyDepthTests(unittest.TestCase):
    def test_banter_is_short_but_explanations_and_support_can_expand(self):
        self.assertEqual(reply_style("哈哈，确实"), "casual")
        for text in ("这是为什么", "怎么设置", "我现在很焦虑", "请详细解释这个过程"):
            self.assertEqual(reply_style(text), "expanded")
        self.assertEqual(reply_style("一句话说明原因"), "concise")
        self.assertEqual(reply_style("请详细解释", [{"text": "简短点"}]), "expanded")
