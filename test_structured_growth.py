"""Structured archives, continuity, autonomous reading and real CLI evidence."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from bot_identity import IdentityStore, bind
from companion_life import repair_pending_browse
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.files import ConsoleFileError
from exploration import configuration, ensure_exploration, reading_evidence
from memory_digest import normalize_digest, merge_sections, summarize_records
from test_ai_fixture import ScriptedConsoleAgentMock, prompt_object


class StructuredMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = IdentityStore(self.root / "identity.sqlite3")
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.context = self.broker.issue_context({"namespace": "a", "scope": "g", "session_key": "p",
            "main_agent": True, "identity_owner": "p", "identity_database": str(self.store.path),
            "exploration_config": {"roots": {"documents": str(self.root / "documents")}},
            "output_dir": str(self.root / "output")}, self.root / "contexts")
        (self.root / "documents").mkdir()
        self.payload = {"summary": "阿林研究配乐。", "session_start": "2026-11-10 12:00:00", "session_end": "2026-11-10 13:00:00",
            "participants": {"123": {"nickname": "阿林", "role": "user"}, "bot": {"nickname": "机器人", "role": "assistant"}},
            "events": [{"id": "music", "title": "讨论配乐", "summary": "研究弦乐", "people": ["123"], "tags": ["配乐"], "status": "open", "evidence": ["m1"]}],
            "people": [{"user_id": "123", "facts": ["学习配乐"], "preferences": ["喜欢弦乐"], "tags": ["音乐"], "evidence": ["m1"]}],
            "growth": [{"observation": "没有先读来源", "lesson": "先查依据", "proposal": "回答前核对来源", "evidence": ["m1"]}]}

    def archive(self, **changes):
        return self.store.enqueue_archive("p", "g", "session", {**self.payload, **changes})

    def test_projection_sections_queries_restart_and_idempotence(self):
        first = self.archive()
        self.assertEqual(first, self.archive())
        store = IdentityStore(self.store.path)
        person = store.get("p", "g", "123")
        self.assertEqual(person["facts"], ["学习配乐"])
        self.assertEqual(person["preferences"], ["喜欢弦乐"])
        self.assertEqual(len(person["timeline"]), 1)
        events = store.events("p", "g", user_id="123", tag="配乐", since="2026-11-10", until="2026-11-10")["events"]
        self.assertEqual([row["id"] for row in events], ["music"])
        self.assertEqual(events[0]["revision"], 1)
        self.assertFalse(store.events("p", "other")["events"])
        self.assertFalse(store.contacts("other", "g")["contacts"])
        self.assertEqual(store.events("p", "g", since="2026-11-11")["events"], [])
        self.assertEqual(store.get("p", "g", "bot")["state"], "missing")

    def test_archive_corrections_deletes_and_tombstones(self):
        self.archive()
        self.archive(summary="偏好更正", people=[{"user_id": "123", "remove_preferences": ["喜欢弦乐"], "preferences": ["喜欢钢琴"]}])
        person = self.store.get("p", "g", "123")
        self.assertEqual(person["preferences"], ["喜欢钢琴"])
        self.archive(summary="忘记", people=[{"user_id": "123", "operation": "delete"}], events=[{"id": "music", "operation": "delete"}])
        self.archive(summary="不能复活")
        self.assertTrue(self.store.get("p", "g", "123")["deleted"])
        self.assertEqual(self.store.events("p", "g")["events"], [])
        self.assertTrue(self.store.event("p", "g", "music")["deleted"])
        self.assertGreater(len(self.store.event_history("p", "g", "music")["history"]), 1)

    def test_projection_rolls_back_people_events_and_archive_on_failure(self):
        original = self.store.edit_contact
        with patch.object(self.store, "edit_contact", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.archive()
        self.assertEqual(self.store.events("p", "g")["events"], [])
        self.assertEqual(self.store.get("p", "g", "123")["state"], "missing")
        self.assertEqual(self.store.pending("p", "g"), [])
        self.archive()
        self.assertEqual(len(self.store.get("p", "g", "123")["timeline"]), 1)

    def test_legacy_archives_backfilled_without_recreating_deleted_people(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO identity_archives VALUES(?,?,?,?,?,1,?)", ("legacy", "p", "g", "session", json.dumps(self.payload), 1))
        restored = IdentityStore(self.store.path)
        self.assertEqual(restored.event("p", "g", "music")["title"], "讨论配乐")
        self.assertEqual(restored.pending("p", "g"), [])
        self.assertEqual(len(IdentityStore(self.store.path).event_history("p", "g", "music")["history"]), 1)

    def test_growth_review_needs_reason_and_effective_preset_change(self):
        archive = self.archive()
        for decision, reason in (("keep", " "), ("pretend", "理由"), ("update", "尚未修改")):
            with self.assertRaises(ValueError):
                self.store.review_archive("p", "g", archive, decision, 0, reason)
        with self.assertRaises(PermissionError):
            self.store.review_archive("other", "g", archive, "keep", 0, "无权")
        self.store.edit_preset("p", "先核对来源", 0, "真实复盘", "m1")
        self.store.review_archive("p", "g", archive, "update", 1, "已根据纠正落实来源核对习惯")
        self.assertEqual(self.store.reviewed("p", "g"), {archive})
        self.assertEqual(self.store.edit_preset("p", "先核对来源", 1, "同内容", "m1")["revision"], 1)

    def test_events_cli_revision_conflict_dates_and_finish_guards(self):
        params = {"event_id": "e", "revision": 0, "reason": "消息 m1", "title": "研究", "started_at": "2026-11-10T12:00:00+08:00", "tags": ["研究"]}
        result = execute("events", self.context, operation="upsert", params=params)
        self.assertEqual(result["revision"], 1)
        self.assertEqual(execute("events", self.context, operation="list", params={"since": "2026-11-10T03:00:00Z", "until": "2026-11-10T05:00:00Z"})["events"][0]["id"], "e")
        with self.assertRaises(ValueError):
            execute("events", self.context, operation="list", params={"since": "yesterday"})
        execute("finish", self.context)
        with self.assertRaises(ConsoleFileError):
            execute("events", self.context, operation="delete", params={"event_id": "e", "revision": 1, "reason": "已结束"})

    def test_explore_paging_source_links_json_and_path_escape(self):
        docs = self.root / "documents"
        (docs / "note.md").write_text("abc" * 1000, encoding="utf-8")
        (docs / "page.html").write_text('<script>hidden</script><h1>标题</h1><p>正文</p><a href="https://example.com/a">来源</a>', encoding="utf-8")
        (docs / "data.json").write_text('{"topics":[{"title":"配乐"}]}', encoding="utf-8")
        read = lambda operation, **params: execute("explore", self.context, operation=operation, params={"root_id": "documents", **params})
        page = read("read", path="note.md", max_chars=256)
        self.assertEqual(page["next_offset"], 256)
        self.assertEqual(len(page["text"]), 256)
        text = read("parse", path="page.html")["text"]
        self.assertIn("正文", text)
        self.assertNotIn("hidden", text)
        self.assertIn("https://example.com/a", read("parse", path="page.html", format="links")["text"])
        self.assertIn("配乐", read("parse", path="data.json", format="json", query="topics.0.title")["text"])
        with self.assertRaises(PermissionError):
            read("read", path="../identity.sqlite3")
        with self.assertRaises(PermissionError):
            read("list", root_id="not-granted")

    def test_fetch_keeps_source_and_decodes_chinese_without_screenshot(self):
        page = '<meta charset="gbk"><article>中文正文</article>'.encode("gbk")
        with patch("exploration.fetch_page", return_value=(page, "text/html", "https://example.com/music")):
            receipt = execute("explore", self.context, operation="fetch", params={"url": "https://example.com/music"})
        self.assertIn("中文正文", receipt["text"])
        self.assertIn("<article>", Path(receipt["source_file"]).read_text(encoding="utf-8"))

    def test_reading_evidence_requires_successful_tool_result(self):
        def trace(operation="read", ok=True, text="真实读取", status="DONE"):
            return [{"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "qqbot_cli", "id": "1", "args": {"action": '"explore"', "operation": json.dumps(operation)}}]},
                {"type": "GENERIC", "tool_call_id": "1", "status": status, "content": "Tool output: " + json.dumps({"ok": ok, "text": text})}]
        self.assertEqual(reading_evidence(trace()), {"explore.read"})
        encoded = trace()
        encoded[0]["tool_calls"][0]["args"] = json.dumps(encoded[0]["tool_calls"][0]["args"])
        self.assertEqual(reading_evidence(encoded), {"explore.read"})
        wrong_call = trace()
        wrong_call[1]["tool_call_id"] = "other"
        self.assertEqual(reading_evidence(wrong_call), set())
        for rows in (trace("list"), trace(ok=False), trace(text=""), trace(status="FAILED"), trace()[:1]):
            self.assertEqual(reading_evidence(rows), set())

    def test_merge_retains_omitted_sections_and_applies_removals(self):
        previous = normalize_digest(self.payload)
        current = normalize_digest({"summary": "下一块", "people": [{"user_id": "123", "remove_preferences": ["喜欢弦乐"], "preferences": ["喜欢钢琴"]}]})
        merged = merge_sections(previous, current)
        self.assertEqual(merged["events"][0]["id"], "music")
        self.assertEqual(merged["people"][0]["preferences"], ["喜欢钢琴"])
        self.assertIn("喜欢弦乐", merged["people"][0]["remove_preferences"])
        self.assertEqual(merged["growth"][0]["proposal"], "回答前核对来源")

    def test_ambiguous_or_missing_ack_parent_fails_without_waiting_forever(self):
        parent = {"namespace": "legacy", "scope": "g", "session_key": "s", "generation": 0,
            "main_agent": True, "turn_managed": True, "received_at": 1}
        for count in (0, 2):
            scope = "g" + str(count)
            for _ in range(count):
                self.broker.issue_context({**parent, "scope": scope}, self.root / "contexts")
            child = {**parent, "scope": scope, "turn_managed": False, "self_directed": True, "ack_before_work": True}
            path = self.broker.issue_context(child, self.root / "contexts")
            _, token = ConsoleBroker.from_context(path)
            receipt = self.broker._enqueue(token, child, "sandbox_play", "旧阅读", {}, "legacy")
            repair_pending_browse(self.broker, "legacy")
            record = self.broker.record(receipt["id"])
            self.assertEqual(record["state"], "failed")
            self.assertEqual(json.loads(record["result"])["code"], "ACK_CONTEXT_MISSING")


class RuntimeGrowthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def prepare(self):
        bot, key = self.fixture.bot(stored_cid="continuous-cid")
        coordinator = bot._get_main_coordinator()
        self.start = patch.object(coordinator, "start")
        self.start.start()
        self.addCleanup(self.start.stop)
        bot.ai.chat = ScriptedConsoleAgentMock(return_value="")
        await self.fixture.receive(bot, False, "m1", "先去忙了")
        return bot, key, coordinator

    async def test_same_cid_restart_protocol_and_growth_are_incremental(self):
        bot, key, coordinator = await self.prepare()
        first = bot.ai.chat.await_args.args[1]
        self.assertIn('"type": "continuity_resume"', first)
        self.assertNotIn("@rules:", first)
        base = bind(bot, key)["identity_base_prompt"]
        self.assertNotIn(base, first)
        bot.ai._save_sessions()
        from antigravity_client import AntigravityClient
        restored = AntigravityClient(sessions_file=bot.ai._base_sessions_file, account_key=bot.account_key)
        self.assertEqual(restored.get_or_create_session(key).conversation_id, "continuous-cid")
        await self.fixture.receive(bot, False, "m2", "继续", elapsed=1)
        self.assertNotIn('"type": "protocol_update"', bot.ai.chat.await_args.args[1])
        binding = bind(bot, key)
        coordinator.identity.edit_preset(binding["identity_owner"], "回答前先读实际来源", 0, "主人纠正", "m2")
        await self.fixture.receive(bot, False, "m3", "还有呢", elapsed=2)
        prompt = bot.ai.chat.await_args.args[1]
        self.assertIn('"type": "growth_update"', prompt)
        self.assertIn("回答前先读实际来源", prompt)
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "continuous-cid")

    async def test_finish_alone_does_not_acknowledge_growth_review(self):
        bot, key, coordinator = await self.prepare()
        ctx = coordinator.contexts[key]
        owner = bind(bot, key)["identity_owner"]
        archive = coordinator.identity.enqueue_archive(owner, ctx.scope, key, {"summary": "复盘来源读取习惯"})
        async def finish_only(key, prompt, **options):
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            execute("finish", context)
            return ""
        bot.ai.chat = AsyncMock(side_effect=finish_only)
        await coordinator.chat(key, '{"type":"memory_maintenance"}', idle_event=True, activity_scope=ctx.scope, memory_maintenance=True)
        self.assertEqual(coordinator.identity.pending(owner, ctx.scope)[0]["archive_id"], archive)
        bot.send_group_message.assert_not_awaited()

    async def test_format_changes_do_not_replay_the_persona(self):
        bot, key, coordinator = await self.prepare()
        bot.app_config.bot.enable_action_descriptions = False
        await self.fixture.receive(bot, False, "format", "继续说", elapsed=1)
        prompt = bot.ai.chat.await_args.args[1]
        self.assertIn('"type": "behavior_update"', prompt)
        self.assertIn("纯对话台词模式", prompt)
        self.assertNotIn('"type": "persona_update"', prompt)
        self.assertNotIn(bind(bot, key)["identity_base_prompt"], prompt)
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "continuous-cid")

    async def test_idle_wait_starts_one_reading_job_and_records_real_experience(self):
        bot, key, coordinator = await self.prepare()
        cfg = bot.app_config.bot
        cfg.self_activity_enabled = True
        cfg.self_activity_interval_minutes = 1
        workspace = Path(configuration(coordinator, key)["workspace"])
        workspace.mkdir(parents=True)
        (workspace / "music.md").write_text("弦乐通过不同奏法改变音色。", encoding="utf-8")
        entered, release = asyncio.Event(), asyncio.Event()
        async def model(session_key, prompt, **options):
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            if session_key == key:
                execute("wait", context)
                return ""
            entered.set()
            await release.wait()
            receipt = execute("explore", context, operation="read", params={"root_id": "workspace", "path": "music.md"})
            options["response_trace"].extend([
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "qqbot_cli", "id": "read", "args": {"action": "explore", "operation": "read"}}]},
                {"type": "GENERIC", "tool_call_id": "read", "status": "DONE", "content": json.dumps(receipt)}])
            execute("agents", context, operation="report", params={"text": "读到了弦乐奏法与音色的关系，下一步比较拨奏与拉奏。来源：music.md", "finish": True})
            return ""
        bot.ai.chat = AsyncMock(side_effect=model)
        with patch("time.time", return_value=self.fixture.baseline + 61):
            await coordinator.schedule_self_activity()
            await asyncio.gather(*coordinator.self_activity_tasks.values())
            await asyncio.wait_for(entered.wait(), 3)
            again = ensure_exploration(coordinator, key, force=True)
            self.assertTrue(again["existing"])
            release.set()
            tasks = list(coordinator.agents.tasks.values())
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 5)
        self.assertEqual(len(coordinator.agents.mailbox.all(coordinator.namespace)), 1)
        state = bot.ai.get_or_create_session(key).chat_state
        self.assertIn("下一步", state["last_discovery"])
        owner = bind(bot, key)["identity_owner"]
        pending = coordinator.identity.pending(owner, coordinator.contexts[key].scope, detailed=True)
        self.assertTrue(any(row.get("source") == "autonomous_reading" for row in pending))
        self.assertEqual(coordinator.identity.events(owner, coordinator.contexts[key].scope, tag="自主阅读")["events"][0]["title"], "自主阅读的新发现")
        bot.send_group_message.assert_not_awaited()

    async def test_authorized_roots_include_only_current_scope_owner_grants(self):
        bot, key, coordinator = await self.prepare()
        cfg = bot.app_config
        # The production master registry supports group-specific IDs.
        from sandbox_runtime import SandboxRuntime
        runtime = SandboxRuntime(cfg.sandbox.directory)
        with runtime.connect() as db:
            scope = coordinator.contexts[key].scope
            for identity, namespace, group, owner in (("yes", coordinator.namespace, scope, "456"), ("scope", coordinator.namespace, "other", "456"), ("owner", coordinator.namespace, scope, "789"), ("account", "other", scope, "456")):
                db.execute("INSERT INTO grants VALUES(?,?,?,?,?,0)", (identity, namespace, group, owner, str(Path("documents-" + identity).resolve())))
        with patch.object(type(cfg.bot), "is_master", side_effect=lambda group, user: user == "456"):
            roots = configuration(coordinator, key)["roots"]
        self.assertIn("grant-yes", roots)
        self.assertFalse(any(name in roots for name in ("grant-scope", "grant-owner", "grant-account")))

    async def test_browse_ack_link_and_legacy_pending_repair(self):
        bot, key, coordinator = await self.prepare()
        bot.app_config.bot.self_activity_enabled = True
        coordinator.contexts[key].received_at += 1
        context, token, payload = coordinator.issue_chat_context(coordinator.contexts[key], key, ack_before_work=True)
        executable = Path("chrome.exe").resolve()
        executable.write_bytes(b"fixture")
        with patch("browser_control.installed_browser", return_value=executable):
            receipt = execute("companion", context, operation="browse", params={"url": "https://example.com", "text": "读音乐"})
        row = coordinator.broker.record(receipt["id"])
        child = coordinator.broker.context(row["token"])
        self.assertEqual(child["ack_token"], token)
        self.assertFalse(coordinator.work_ready(row["token"]))
        execute("reply", context, text="去看一会儿资料。")
        for message in coordinator.broker.committed_messages(token):
            coordinator.broker.message_state(message["command_id"], "accepted", {"message_id": "fixture-receipt"})
        self.assertTrue(coordinator.work_ready(row["token"]))
        child.pop("ack_token")
        with coordinator.broker.connect() as db:
            db.execute("UPDATE contexts SET payload=? WHERE token=?", (json.dumps(child), row["token"]))
        repair_pending_browse(coordinator.broker, coordinator.namespace)
        self.assertEqual(coordinator.broker.context(row["token"])["ack_token"], token)

