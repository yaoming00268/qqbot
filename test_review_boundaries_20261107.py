"""Behavioral regressions for the November 7 review boundaries."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from console_tools.cli import execute
from skills.base import SkillResult
import test_startup_participation as fixtures
import test_desktop_control as desktop_fixtures
from console_tools.files import ConsoleFileError


class WorkAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def prepare(self, access="master"):
        bot, key = self.fixture.bot()
        bot.app_config.bot.work_permissions.access = access
        coordinator = bot._get_main_coordinator()
        ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
            message_id="revoked-owner", text="offline fixture", is_group=True)
        return bot, key, coordinator, ctx

    async def test_revoked_owner_admin_and_allowlist_are_rechecked_before_queueing(self):
        for access in ("master", "admin", "allowlist"):
            with self.subTest(access=access):
                bot, key, main, ctx = self.prepare(access)
                cfg = bot.app_config.bot
                if access == "master": cfg.master_id = "456"
                if access == "admin": cfg.admin_ids = ["456"]
                if access == "allowlist": cfg.work_permissions.allowed_user_ids = ["456"]
                path, _, _ = main.issue_chat_context(ctx, key)
                receipt = execute("submit", path, task_type="code", task="never execute host tools")
                cfg.master_id, cfg.admin_ids, cfg.work_permissions.allowed_user_ids = "new-owner", [], []
                with patch.object(main, "_queue_result"), patch.object(main, "start"), \
                     patch.object(bot._get_runtime(), "submit_work") as submit:
                    await main.poll()
                submit.assert_not_called()
                result = main.broker.record(receipt["id"])
                self.assertEqual(result["state"], "ready")
                self.assertFalse(json.loads(result["result"])["success"])

    async def test_permission_is_refreshed_again_at_execution_and_in_skill_context(self):
        bot, key, main, ctx = self.prepare("all")
        bot.app_config.bot.master_id = "456"
        path, _, metadata = main.issue_chat_context(ctx, key)
        receipt = execute("submit", path, task_type="code", task="offline no-op")
        bot.app_config.bot.master_id = "new-owner"
        calls = []
        async def run(_skill, params, context):
            calls.append(context.is_master)
            return SkillResult(False, "fixture complete", reply_handled=True)
        with patch("skills.task_skills.RunTaskSkill.execute", new=run), patch.object(main, "start"):
            await main._execute(main.broker.record(receipt["id"]), ctx, metadata)
        self.assertEqual(calls, [False])
        # Execution must reject the stale master snapshot even without poll().
        bot.app_config.bot.master_id = "456"
        bot.app_config.bot.work_permissions.access = "master"
        other, _, saved = main.issue_chat_context(ctx, key)
        receipt = execute("submit", other, task_type="code", task="denied no-op")
        bot.app_config.bot.master_id = "new-owner"
        with patch("skills.task_skills.RunTaskSkill.execute", new=AsyncMock()) as worker, patch.object(main, "start"):
            await main._execute(main.broker.record(receipt["id"]), ctx, saved)
        worker.assert_not_awaited()
        self.assertFalse(json.loads(main.broker.record(receipt["id"])["result"])["success"])

    async def test_running_work_is_cancelled_when_owner_grant_is_revoked(self):
        bot, key, main, ctx = self.prepare()
        bot.app_config.bot.master_id = "456"
        path, _, _ = main.issue_chat_context(ctx, key)
        receipt = execute("submit", path, task_type="code", task="blocked fixture")
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def run(*args, **kwargs):
            entered.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        with patch("skills.task_skills.RunTaskSkill.execute", new=run), \
             patch.object(main, "start"), patch.object(main, "_queue_result"):
            await main.poll()
            await asyncio.wait_for(entered.wait(), 2)
            bot.app_config.bot.master_id = "new-owner"
            await main.poll()
            await asyncio.wait_for(cancelled.wait(), 2)
        self.assertEqual(main.broker.record(receipt["id"])["state"], "cancelled")


class DesktopTurnTests(unittest.TestCase):
    def setUp(self):
        self.fixture = desktop_fixtures.DesktopControlTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_finished_main_cannot_input_or_write_but_bound_worker_continues(self):
        f = self.fixture
        frame = execute("desktop", f.context, operation="screenshot", params={"session_id": f.sid})
        execute("finish", f.context)
        for action, operation, params in (
            ("desktop", "key", {"session_id": f.sid, "observation_id": frame["observation_id"], "key": "enter"}),
            ("desktop", "click", {"session_id": f.sid, "observation_id": frame["observation_id"], "x": 1, "y": 1}),
            ("sandbox", "key", {"session_id": f.sid, "observation_id": frame["observation_id"], "key": "enter"}),
            ("sandbox", "note", {"session_id": f.sid, "text": "late fixture", "evidence": [frame["observation_id"]]}),
            ("sandbox", "write", {"session_id": f.sid, "path": "fixture.txt", "content": "late"}),
        ):
            with self.subTest(action=action, operation=operation), self.assertRaises(ConsoleFileError) as raised:
                execute(action, f.context, operation=operation, params=params)
            self.assertEqual(raised.exception.code, "TURN_CLOSED")
        self.assertFalse(f.backend.inputs)

        self.assertTrue(execute("desktop", f.context, operation="targets")["targets"])
        result = execute("desktop", f.work, operation="key",
            params={"observation_id": frame["observation_id"], "key": "enter", "wait_ms": 0})
        self.assertTrue(result["input_posted"])
        self.assertEqual(len(f.backend.inputs), 1)

    def test_finish_during_backend_preflight_rejects_input(self):
        f = self.fixture
        frame = execute("desktop", f.context, operation="screenshot", params={"session_id": f.sid})
        original = f.backend.act
        def finish_then_act(*args, **kwargs):
            execute("finish", f.context)
            return original(*args, **kwargs)
        with patch.object(f.backend, "act", side_effect=finish_then_act):
            result = execute("desktop", f.context, operation="key",
                params={"session_id": f.sid, "observation_id": frame["observation_id"], "key": "enter", "wait_ms": 0})
        self.assertFalse(result["input_posted"])
        self.assertEqual(result["code"], "TURN_CLOSED")
        self.assertFalse(f.backend.inputs)


class PersonaMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_persona_switch_cannot_reuse_candidates_or_last_reply_and_switch_back_restores_them(self):
        from console_media import prepare_media_command
        from media_models import MediaAsset, MediaIntent
        from media_downloader import MediaError
        from skills.base import SkillContext
        bot, key = self.fixture.bot()
        def context(session, message):
            return bot._make_media_context(session_key=session, user_id="456", conversation_id="12345678",
                message_id=message, text="/pick 1", is_group=True)
        a = context(key, "persona-a")
        original_owner = a.media_owner
        a.ai_session_key = "__work__:task:fixture"
        self.assertEqual(a.media_owner, original_owner)
        service = bot._get_media_service()
        source = Path(service.config.cache_dir).resolve() / "persona-a.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("A fixture", encoding="utf-8")
        asset = MediaAsset(kind="file", title="A-only.txt", local_path=str(source))
        service.store.put_asset(a.media_scope, asset)
        intent = MediaIntent(operation="library", kind="file", query="A")
        service.store.put_results(a.media_owner, intent.model_dump_json(), "file", 0, [asset], 300)
        bot._remember_chat_reply(a, "persona A reply")
        first = bot.personas.active_persona_name
        bot.personas.set_active(next(name for name in bot.personas.presets if name != first))
        bot.sync_system_prompt()
        b = context(bot._chat_session_key("onebot_group_12345678"), "persona-b")
        self.assertEqual(a.scope, b.scope)  # Transport grouping remains shared.
        self.assertNotEqual(a.media_owner, b.media_owner)
        self.assertEqual(service.store.last_reply(b.media_owner), "")
        request = {"task": "/pick 1", "result": json.dumps({"command": "pick", "argument": "1"})}
        skill_context = SkillContext(b.ai_session_key, b.conversation_id, b.sender_id,
            is_group=True, media_context=b, media_service=service, app_config=bot.app_config)
        with self.assertRaises(MediaError):
            await prepare_media_command(request, skill_context)
        bot._cancel_media(b)
        bot.personas.set_active(first)
        bot.sync_system_prompt()
        restored = context(bot._chat_session_key("onebot_group_12345678"), "restored-a")
        self.assertEqual(bot._last_chat_replies[restored.media_owner], "persona A reply")
        self.assertEqual(service.store.last_reply(restored.media_owner), "persona A reply")
        skill_context.media_context, skill_context.session_key = restored, restored.ai_session_key
        async def prepare(selected, **kwargs): return selected
        with patch.object(service.processor, "prepare", new=AsyncMock(side_effect=prepare)), \
             patch("skills.task_skills.RunTaskSkill._deliver_result", new=AsyncMock(return_value=SkillResult(True, "fixture"))) as deliver:
            result = await prepare_media_command(request, skill_context)
        self.assertTrue(result.success)
        deliver.assert_awaited_once()

    def test_legacy_asset_schema_is_preserved_and_reused_ids_do_not_overwrite_other_personas(self):
        import sqlite3
        from media_store import MediaStore
        from media_models import MediaAsset
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media.sqlite3"
            asset = MediaAsset(kind="file", title="legacy.txt")
            from contextlib import closing
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE assets(id TEXT PRIMARY KEY,scope TEXT NOT NULL,data TEXT NOT NULL)")
                db.execute("INSERT INTO assets VALUES(?,?,?)", (asset.asset_id, "legacy", asset.model_dump_json()))
                db.commit()
            store = MediaStore(path)
            self.assertEqual(store.asset("legacy", asset.asset_id).title, "legacy.txt")
            store.put_asset("scope::persona:a", asset.model_copy(update={"title": "A"}))
            store.put_asset("scope::persona:b", asset.model_copy(update={"title": "B"}))
            self.assertEqual(store.asset("scope::persona:a", asset.asset_id).title, "A")
            self.assertEqual(store.asset("scope::persona:b", asset.asset_id).title, "B")
            self.assertEqual(MediaStore(path).asset("legacy", asset.asset_id).title, "legacy.txt")


class PersonaIdentityTests(unittest.TestCase):
    def test_duplicate_ids_are_rejected_before_api_changes_or_disk_writes(self):
        from persona_manager import PersonaManager, PersonaPreset
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "personas.yaml"
            manager = PersonaManager(str(path))
            manager.add_preset(PersonaPreset(name="A", id="fixture-a", prompt="A"))
            before = path.read_bytes()
            for operation in (lambda: manager.add_preset(PersonaPreset(name="B", id="fixture-a", prompt="B")),
                              lambda: manager.update_preset("missing", PersonaPreset(name="B", id="fixture-a", prompt="B"))):
                with self.assertRaisesRegex(ValueError, "ID 重复"):
                    operation()
                self.assertNotIn("B", manager.presets)
                self.assertEqual(path.read_bytes(), before)
            manager.presets["B"] = PersonaPreset(name="B", id="fixture-a", prompt="B")
            with self.assertRaisesRegex(ValueError, "ID 重复"):
                manager.save()
            self.assertEqual(path.read_bytes(), before)

    def test_invalid_reload_preserves_live_identity_and_rename_preserves_memory_identity(self):
        import yaml
        from persona_manager import PersonaManager, PersonaPreset
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "personas.yaml"
            manager = PersonaManager(str(path))
            manager.add_preset(PersonaPreset(name="A", prompt="A"))
            original = manager.get_preset("A").id
            saved = path.read_bytes()
            data = yaml.safe_load(saved)
            data["presets"]["B"] = {**data["presets"]["A"], "name": "B"}
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ID 重复"):
                manager.load()
            self.assertNotIn("B", manager.presets)
            self.assertEqual(manager.get_preset("A").id, original)
            with self.assertRaisesRegex(ValueError, "ID 重复"):
                PersonaManager(str(path))
            path.write_bytes(saved)
            manager.update_preset("A", PersonaPreset(name="Renamed", prompt="edited A"))
            manager.add_preset(PersonaPreset(name="Copy", prompt="edited A"))
            self.assertEqual(manager.get_preset("Renamed").id, original)
            self.assertNotEqual(manager.get_preset("Copy").id, original)
            self.assertEqual(PersonaManager(str(path)).get_preset("Renamed").id, original)


class PersonaArchiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from config import AppConfig
        from types import SimpleNamespace
        from group_memory import GroupMemoryManager
        from persona_isolation import PersonaMemory
        from test_memory_efficiency import summarizer
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = GroupMemoryManager(self.tmp.name)
        self.a = SimpleNamespace(name="A", id="fixture-a")
        self.b = SimpleNamespace(name="B", id="fixture-b")
        self.client = SimpleNamespace(account_key="archive-fixture", send_action=None,
            personas=SimpleNamespace(presets={"A": self.a, "B": self.b}), app_config=AppConfig())
        self.client.app_config.bot.runtime_database_path = str(Path(self.tmp.name) / "runtime.sqlite3")
        self.router = PersonaMemory(self.client, self.root)
        self.ai, self.prompts = summarizer()

    async def test_unloaded_profile_is_recovered_without_other_account_legacy_or_live_messages(self):
        from types import SimpleNamespace
        from persona_isolation import PersonaMemory, identity
        old = self.router.manager(persona=self.b)
        old.record_session_message("123", "u", "B", "旧人设 B 的待归档消息")
        other = PersonaMemory(SimpleNamespace(account_key="other-account", send_action=None,
            personas=self.client.personas), self.root)
        other_manager = other.manager(persona=self.b)
        other_manager.record_session_message("123", "u", "other", "其他账号的消息")
        self.root.record_session_message("123", "u", "legacy", "混合旧目录消息")
        current = PersonaMemory(self.client, self.root)
        self.assertFalse(current.managers)
        self.assertTrue(current.has_pending())
        self.assertEqual(set(current.managers), {identity(self.client, self.b)})
        live = current.manager(persona=self.a)
        live.record_session_message("123", "u", "A", "当前 live 消息")
        saved = await current.summarize_and_save_session(self.ai, recover_only=True)
        self.assertEqual(len(saved), 1)
        record = json.loads(Path(next(iter(saved.values()))).read_text(encoding="utf-8"))
        self.assertTrue(record["summary_complete"])
        self.assertIn("旧人设 B", record["summary"])
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(live.run_memory.pending_groups(), ["123"])
        self.assertTrue(other_manager.has_pending())
        self.assertTrue(self.root.has_pending())
        self.assertFalse(current.managers[identity(self.client, self.b)].has_pending())

    async def test_unloaded_failed_profile_requires_explicit_retry_after_restart(self):
        from persona_isolation import PersonaMemory
        old = self.router.manager(persona=self.b)
        old.record_session_message("123", "u", "B", "失败后必须显式重试")
        self.ai.chat.side_effect = RuntimeError("offline fixture")
        await self.router.summarize_and_save_session(self.ai)
        current = PersonaMemory(self.client, self.root)
        self.ai.chat.reset_mock()
        await current.summarize_and_save_session(self.ai, recover_only=True)
        self.ai.chat.assert_not_awaited()
        from test_memory_efficiency import summarizer
        ai, prompts = summarizer()
        await current.summarize_and_save_session(ai, recover_only=True, retry=True)
        self.assertEqual(len(prompts), 1)
        self.assertFalse(current.has_pending())


class CommandReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def command(self, text, *, is_group=True):
        from console_tools.broker import ConsoleBroker
        from console_tools.chat_commands import execute_control
        bot, key = self.fixture.bot()
        main = bot._get_main_coordinator()
        ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
            message_id="review-command", text=text, is_group=is_group)
        file = main.issue_chat_context(ctx, key)[0]
        _, token = ConsoleBroker.from_context(file)
        receipt = execute("command", file, text=text)
        return bot, main, ctx, file, lambda: execute_control(main, main.broker.record(receipt["id"]), ctx, main.broker.context(token))

    def test_private_name_fails_explicitly_and_group_name_is_durable(self):
        from console_tools.chat_commands import format_help
        bot, _, _, _, run = self.command("/name FixtureName", is_group=False)
        with patch.object(bot.memory_mgr, "set_participant_nickname") as save, \
             patch.object(bot, "_cache_member_name") as cache, self.assertRaises(ConsoleFileError) as raised:
            run()
        self.assertEqual(raised.exception.code, "GROUP_ONLY")
        save.assert_not_called()
        cache.assert_not_called()
        self.assertIn("仅群聊", format_help())
        bot, _, ctx, _, run = self.command("/name GroupFixtureName")
        self.assertEqual(run(), "已记录称呼：GroupFixtureName")
        from group_memory import GroupMemoryManager
        restored = GroupMemoryManager(bot.memory_mgr.manager(ctx.conversation_id).storage_dir)
        self.assertEqual(restored.get_group_memory(ctx.conversation_id).participants[ctx.sender_id].nickname, "GroupFixtureName")

    def test_skills_examples_queue_current_work_and_respect_current_permissions(self):
        bot, main, _, context, run = self.command("/skills")
        with patch("skills.manager.default_skill_manager.format_prompt_skills", side_effect=AssertionError("legacy protocol")):
            help_text = run()
        self.assertNotIn('"action": "skill"', help_text)
        self.assertNotIn("send_image", help_text)
        for word in ("deliver", "finish", "context", "平台回执", "agents"):
            self.assertIn(word, help_text)
        examples = [line.split("：", 1)[1] for line in help_text.splitlines() if line.startswith("• ")]
        for example in examples:
            receipt = execute("command", context, text=example)
            self.assertIn(main.broker.record(receipt["id"])["kind"], ("search", "image", "video", "file",
                "generate_image", "generate_video", "code", "process", "task", "command"))
        bot.app_config.bot.work_permissions.access = "disabled"
        restricted = run()
        self.assertIn("没有后台工作权限", restricted)
        self.assertFalse(any(line.startswith("• ") for line in restricted.splitlines()))


class MaintenanceEntryTests(unittest.TestCase):
    def test_importing_historical_entries_has_no_writes_or_subprocesses(self):
        import importlib
        from unittest.mock import mock_open
        for name in ("submit_notes", "submit_wiki_notes", "submit_vote_notes", "submit_sakura_notes"):
            with self.subTest(name=name), patch("builtins.open", mock_open()) as opened, \
                 patch("subprocess.run", side_effect=AssertionError("unexpected process")), \
                 patch("console_tools.cli.main", side_effect=AssertionError("unexpected submission")):
                module = importlib.import_module(name)
                importlib.reload(module)
                opened.assert_not_called()

    def test_parameterized_note_requires_context_and_rejects_a_finished_turn(self):
        from maintenance.submit_note import main
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main([])
        self.assertEqual(raised.exception.code, 2)
        fixture = desktop_fixtures.DesktopControlTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        note = fixture.root / "explicit-note.txt"
        note.write_text("显式提交的测试笔记", encoding="utf-8")
        frame = execute("desktop", fixture.context, operation="screenshot", params={"session_id": fixture.sid})
        args = ["--context-file", fixture.context, "--text-file", str(note), "--kind", "reflection",
            "--evidence", frame["observation_id"], "--session-id", fixture.sid]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(args), 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        execute("finish", fixture.context)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(args), 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "TURN_CLOSED")
        self.assertEqual(note.read_text(encoding="utf-8"), "显式提交的测试笔记")
