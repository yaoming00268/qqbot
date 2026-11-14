"""Actor identity, durable work, private memory and the public integration contract."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from agent_core.configuration import AgentConfig, origin
from agent_core.runtime import ActorRuntime
from agent_core.store import ActorStore
from agent_core.web import ActionGateway
from companion_life import state_for
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.contracts import capabilities


def configuration(**changes):
    return AgentConfig.model_validate({"actors": {"alice": {"name": "Alice", "prompt": "好奇且诚实的角色"}},
        "accounts": {"site": {"actor_id": "alice", "home_url": "https://example.com/", "actions": ["read_content", "create_draft"]}},
        "autonomous_model_enabled": False, **changes})


class ActorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot_actor_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = ActorRuntime(configuration(), database_path=self.root / "actors.sqlite3")
        self.store = self.runtime.store
        self.broker = ConsoleBroker(self.root / "console.sqlite3")

    def context(self, **changes):
        value = {"namespace": "actor:alice", "scope": "actor:alice", "session_key": "actor:alice",
            "main_agent": True, "actor_event": True, "actor_id": "alice", "actor_database": str(self.store.path),
            "sender_id": "", "source_message_id": "event-1", "is_master": False, "generation": 0,
            "reply_sources": [], **changes}
        return self.broker.issue_context(value, self.root / "contexts")

    def test_legacy_identities_are_separate_until_explicitly_bound(self):
        first = self.runtime.bind_legacy("onebot:a", "persona")
        self.assertNotEqual(first, self.runtime.bind_legacy("onebot:b", "persona"))
        self.assertNotEqual(first, self.runtime.bind_legacy("onebot:a", "other"))
        self.runtime.configure(configuration(bindings={"onebot:a::persona": "alice", "official:b::persona": "alice"}))
        self.assertEqual(self.runtime.bind_legacy("onebot:a", "persona"), "alice")
        self.assertEqual(self.runtime.bind_legacy("official:b", "persona"), "alice")
        self.assertEqual(self.store.account("web:site")["actor_id"], "alice")

    def test_shared_actor_life_survives_reopen_without_sharing_raw_chat(self):
        personal = self.context(actor_event=False, namespace="onebot:a", scope="private:1", session_key="chat1")
        execute("companion", personal, operation="remember", params={"text": "喜欢配乐", "kind": "preference"})
        other = self.context(actor_event=False, namespace="onebot:b", scope="group:2", session_key="chat2")
        self.assertEqual(execute("companion", other, operation="state")["interests"], ["喜欢配乐"])
        self.assertEqual(state_for(self.broker, "onebot:b", "chat2")["interests"], ["喜欢配乐"])
        execute("actor", personal, operation="remember", params={"text": "私聊细节不能跨群使用"})
        self.store.remember("alice", "实际网页观察", visibility="actor", source_id="https://example.com/article")
        visible = execute("actor", other, operation="memories")["memories"]
        self.assertEqual([item["text"] for item in visible], ["实际网页观察"])
        self.assertEqual(len(ActorStore(self.store.path).recall("alice", scope="private:1")), 2)

    def test_event_memory_and_old_life_import_are_idempotent(self):
        event = self.store.ingest("alice", "web:site", "content", {"text": "内容"}, source_id="remote:1")
        self.assertEqual(event, self.store.ingest("alice", "web:site", "content", {"text": "内容"}, source_id="remote:1"))
        self.store.import_life("alice", "old:1", {"interests": ["访谈"], "wishes": [{"id": "wish", "text": "愿望", "status": "declined"}]})
        self.store.change_life("alice", lambda state: state.update(reflection="后来的个人感受"))
        self.store.import_life("alice", "old:1", {"reflection": "旧感受"})
        self.assertEqual(self.store.life("alice")["reflection"], "后来的个人感受")
        self.assertEqual(self.store.life("alice")["wishes"][0]["status"], "declined")
        owner, events = self.store.claim_events("alice")
        self.assertEqual([row["id"] for row in events], [event])
        self.assertEqual(self.store.claim_events("alice"), ("", []))
        self.store.finish_events([event])
        self.store.release_lease(owner)
        self.assertFalse(self.store.events("alice", pending=True))

    def test_schedules_keep_due_time_and_are_claimed_once_across_restarts(self):
        due = self.store.due()
        self.assertEqual(len(due), 0)  # no read_feed grant in this fixture
        self.store.schedule("scheduled", "alice", "web:site", "https://example.com/", 60)
        self.assertEqual(len(self.store.due()), 1)
        self.runtime.configure(configuration())
        self.assertFalse(self.store.due())

    def test_action_claims_reject_concurrent_work_and_never_replay_unknown(self):
        first = self.store.prepare_action("alice", "web:site", "read_content", {}, "stable")
        second = self.store.prepare_action("alice", "web:site", "read_content", {}, "second")
        self.assertTrue(self.store.claim_action(first["id"]))
        self.assertFalse(self.store.claim_action(second["id"]))
        with self.store.connect() as db:
            db.execute("UPDATE actor_leases SET expires=0")
        self.assertTrue(self.store.claim_action(second["id"]))
        self.assertEqual(self.store.action(first["id"])["state"], "unknown")
        self.store.finish_action(second["id"], "confirmed", {"verified": True})
        self.assertFalse(self.store.claim_action(first["id"]))
        self.assertEqual(self.store.prepare_action("alice", "web:site", "read_content", {}, "stable")["id"], first["id"])
        with self.assertRaises(ValueError):
            self.store.prepare_action("alice", "web:site", "read_content", {"url": "changed"}, "stable")

    def test_permission_revision_is_checked_before_effect_and_queries_do_not_replay(self):
        pending = self.store.prepare_action("alice", "web:site", "read_content", {}, "queued")
        self.runtime.configure(configuration(accounts={"site": {"actor_id": "alice", "home_url": "https://example.com/", "actions": []}}))
        adapter = SimpleNamespace(execute=lambda *a, **kw: self.fail("revoked action reached adapter"))
        result = ActionGateway(self.store, {"web": adapter}).execute("alice", "web:site", "read_content", {}, "queued")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["result"]["code"], "ACTION_PERMISSION_CHANGED")
        self.assertEqual(execute("actor", self.context(), operation="status", params={"request_id": pending["id"]})["state"], "failed")

    def test_restart_recovers_expired_inflight_actions_as_unknown_without_a_retry(self):
        action = self.store.prepare_action("alice", "web:site", "read_content", {}, "inflight")
        self.store.claim_action(action["id"])
        with self.store.connect() as db:
            db.execute("UPDATE actor_leases SET expires=0")
        reopened = ActorStore(self.store.path)
        self.assertEqual(reopened.recover_expired_actions(), 1)
        self.assertEqual(reopened.action(action["id"])["state"], "unknown")
        self.assertFalse(reopened.claim_action(action["id"]))

    def test_draft_tools_are_local_and_worker_contexts_cannot_operate_accounts(self):
        ctx = self.context()
        first = execute("actor", ctx, operation="draft", params={"account_id": "web:site", "text": "准备的内容"})
        again = execute("actor", ctx, operation="draft", params={"account_id": "web:site", "text": "准备的内容"})
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(first["state"], "confirmed")
        self.assertFalse(self.store.events("alice", pending=True))
        for changes in ({"agent_worker_id": "worker"}, {"sandbox_session_id": "worker"}, {"activity_controller": True}):
            with self.subTest(changes=changes), self.assertRaises(PermissionError):
                execute("actor", self.context(**changes), operation="accounts")
        regular = self.context(actor_event=False, is_master=False)
        with self.assertRaises(PermissionError):
            execute("actor", regular, operation="publish", params={"account_id": "web:site", "draft_id": first["result"]["draft"]["id"]})
        execute("finish", ctx)
        with self.assertRaises((PermissionError, ValueError)):
            execute("actor", ctx, operation="remember", params={"text": "已结束不能再写"})

    def test_role_event_does_not_have_qq_or_software_capabilities(self):
        context = self.context()
        for action in ("reply", "submit", "desktop", "agents"):
            with self.subTest(action=action), self.assertRaises((PermissionError, ValueError)):
                execute(action, context, operation="screenshot" if action == "desktop" else "list" if action == "agents" else "")

    def test_content_events_only_wake_model_on_change(self):
        result = {"verified": True, "title": "来源", "url": "https://example.com/", "text": "相同内容"}
        gateway = ActionGateway(self.store, {"web": SimpleNamespace(execute=lambda *a, **kw: result)})
        gateway.execute("alice", "web:site", "read_content", {}, "first")
        gateway.execute("alice", "web:site", "read_content", {}, "second")
        self.assertEqual(len(self.store.events("alice", pending=True)), 1)
        self.assertEqual(len(self.store.recall("alice", shared_only=True)), 1)

    def test_explicit_person_links_do_not_merge_same_name(self):
        first = self.store.link_person("alice", "onebot:a", "123", "同名")
        second = self.store.link_person("alice", "web:site", "456", "同名")
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.link_person("alice", "web:site", "456", person_id=first), first)
        with self.assertRaises(ValueError):
            self.store.link_person("alice", "web:site", "789", person_id="unknown")


class ActorSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_qq_conversation_uses_the_explicitly_bound_actor_persona(self):
        from test_startup_participation import StartupParticipationTests
        fixture = StartupParticipationTests()
        fixture.setUp()
        try:
            bot, key = fixture.bot(stored_cid="actor-identity")
            bot.app_config.bot.mode = "onebot"
            console = bot._get_main_coordinator()
            bot.app_config.agent = configuration(bindings={console.namespace: "alice"},
                actors={"alice": {"name": "持续角色", "prompt": "共享角色的独立性格正文"}})
            self.assertIn("共享角色的独立性格正文", console.system_for(key))
            from test_project_audit_fixes import qt_app
            from PySide6.QtWidgets import QWidget
            from ui.components.persona_tab import PersonaTabWidget
            qt_app()
            owner = QWidget()
            owner.app_config = bot.app_config
            preview = PersonaTabWidget(bot.personas, owner)
            self.assertEqual(preview._preview_prompt(), console.rules(key, console.system_for(key))[1])
            owner.close()
        finally:
            await fixture.asyncTearDown()

    async def test_persisted_queued_work_resumes_without_repeating_completed_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ActorRuntime(configuration(), database_path=Path(tmp) / "actor.sqlite3")
            action = runtime.store.prepare_action("alice", "web:site", "read_content", {}, "queued")
            calls = []
            runtime.gateway.adapters["web"] = SimpleNamespace(execute=lambda *a, **kw: (calls.append(1) or {"verified": True}))
            await runtime.tick()
            await asyncio.gather(*list(runtime.tasks.values()))
            await runtime.tick()
            self.assertEqual(runtime.store.action(action["id"])["state"], "confirmed")
            self.assertEqual(calls, [1])
            await runtime.close()

    async def test_autonomous_reflection_has_actor_tools_and_cannot_post_plain_final_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = configuration(autonomous_model_enabled=True)
            async def reflect(key, text, **options):
                value = json.loads(text)
                context = value["console_context_file"]
                broker, token = ConsoleBroker.from_context(context)
                self.assertIn("actor", capabilities(broker.context(token)))
                self.assertNotIn("reply", capabilities(broker.context(token)))
                execute("actor", context, operation="remember", params={"text": "基于阅读产生的感受"})
                execute("finish", context)
                return "这段只保存个人感受"
            ai = SimpleNamespace(chat=AsyncMock(side_effect=reflect))
            runtime = ActorRuntime(config, database_path=Path(tmp) / "actors.sqlite3", ai=ai)
            event = runtime.store.ingest("alice", "web:site", "content", {"text": "实际观察"}, source_id="article")
            await runtime.tick()
            await asyncio.gather(*(task for _, task in runtime.model_tasks.values()))
            self.assertEqual(runtime.store.events("alice")[0]["state"], "processed")
            self.assertEqual(ai.chat.await_count, 1)
            self.assertEqual(len(runtime.store.recall("alice", shared_only=True)), 2)
            with runtime.store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM actor_actions").fetchone()[0], 0)
            await runtime.close()


class StandaloneTests(unittest.TestCase):
    def test_qq_free_cli_validates_and_operates_configured_accounts(self):
        from agent_core.cli import main
        from config import AppConfig
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = AppConfig(agent=configuration(database_path=str(root / "actor.sqlite3")))
            path = root / "config.yaml"
            config.save(path)
            with redirect_stdout(io.StringIO()) as output:
                code = main(["--config", str(path), "draft", "--account", "site", "--text", "独立内容"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["result"]["state"], "confirmed")
            self.assertFalse(config.qq.appid)

    def test_invalid_origins_actor_links_and_duplicate_profiles_are_rejected(self):
        self.assertEqual(origin("https://Example.COM:443/path"), "https://example.com")
        for url in ("file:///test", "https://user:password@example.com/", "javascript:alert(1)"):
            with self.assertRaises(ValueError):
                origin(url)
        with self.assertRaises(ValueError):
            configuration(bindings={"onebot:a": "missing"})
        with self.assertRaises(ValueError):
            configuration(accounts={"a": {"actor_id": "alice", "home_url": "https://example.com", "profile": "same"},
                                    "b": {"actor_id": "alice", "home_url": "https://example.com", "profile": "same"}})
