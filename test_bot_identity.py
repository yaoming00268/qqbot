"""Offline regressions for durable people, self revision and explicit closure."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from bot_identity import IdentityStore, bind, store_for, archive_ready
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.files import ConsoleFileError
from console_tools.server import handle, tools_schema
from persona_isolation import identity, persona_for
import test_startup_participation as fixtures
from test_ai_fixture import prompt_object, ConsoleAgentMock


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = IdentityStore(self.home / "identity.sqlite3")
        self.broker = ConsoleBroker(self.home / "console.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def context(self, **extra):
        return self.broker.issue_context({"namespace": "onebot:a", "scope": "onebot:a:group:1",
            "session_key": "onebot_group_1::persona:p", "sender_id": "123", "source_message_id": "m1",
            "main_agent": True, "identity_owner": "account-a-persona-p", "identity_database": str(self.store.path),
            "identity_base_prompt": "基础人物", "is_group": True,
            "reply_sources": [{"id": "m1", "sender_id": "123", "text": "叫我阿林"}], **extra}, self.home / "contexts")

    def test_duplicate_names_renames_search_pagination_and_restart(self):
        self.store.observe("a", "g", "1", "小林")
        self.store.observe("a", "g", "2", "小林")
        self.store.edit_contact("a", "g", "1", 0, "本人告知", "m", preferred_name="阿林", habits="常在晚上聊游戏")
        self.store.observe("a", "g", "1", "新名片")
        reloaded = IdentityStore(self.store.path)
        first = reloaded.get("a", "g", "1")
        self.assertEqual(first["preferred_name"], "阿林")
        self.assertEqual(first["known_names"], ["小林", "新名片"])
        self.assertNotIn("habits", reloaded.get("a", "g", "2"))
        self.assertEqual(reloaded.contacts("a", "g", query="晚上")["contacts"][0]["user_id"], "1")
        self.assertIsNotNone(reloaded.contacts("a", "g", limit=1)["next_offset"])
        self.assertEqual(len(reloaded.contacts("a", "g", offset=1, limit=1)["contacts"]), 1)
        self.assertEqual(reloaded.contacts("other-account", "g")["contacts"], [])
        self.assertEqual(reloaded.contacts("a", "private:1")["contacts"], [])

    def test_revision_conflict_repeat_clear_delete_and_no_resurrection(self):
        edit = lambda **p: self.store.edit_contact("a", "g", "1", **p)
        first = edit(revision=0, reason="称呼", source_id="m", preferred_name="小林", notes="暂不确定")
        self.assertEqual(first, edit(revision=0, reason="称呼", source_id="m", preferred_name="小林", notes="暂不确定"))
        with self.assertRaises(ConsoleFileError):
            edit(revision=0, reason="过期修改", source_id="m2", preferred_name="别人")
        updated = edit(revision=1, reason="改称呼", source_id="m2", preferred_name="阿林", notes="")
        self.assertIn("小林", updated["known_names"])
        self.assertEqual(updated["notes"], "")
        edit(revision=2, reason="忘记", source_id="m3", delete=True)
        self.store.observe("a", "g", "1", "旧昵称")
        self.assertEqual(self.store.contacts("a", "g")["contacts"], [])
        self.assertTrue(self.store.get("a", "g", "1")["deleted"])
        self.assertEqual(len(self.store.history("a", "g", "1")["history"]), 3)
        self.assertFalse(edit(revision=3, reason="重新认识", source_id="m4", preferred_name="林")["deleted"])

    def test_real_cli_mcp_after_reply_before_finish_and_late_write_rejected(self):
        context = self.context()
        execute("reply", context, text="记住了。")
        cli = Path(__file__).with_name("console_tools") / "cli.py"
        process = subprocess.run([sys.executable, str(cli), "contacts", "upsert", "--context-file", context,
            "--user-id", "123", "--revision", "0", "--reason", "本人说叫阿林", "--preferred-name", "阿林"],
            capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(json.loads(process.stdout)["preferred_name"], "阿林")
        result = handle({"id": 1, "method": "tools/call", "params": {"name": "qqbot_cli", "arguments": {
            "action": "preset", "operation": "update", "context_file": context,
            "params": {"text": "倾听群友的完整表达。", "revision": 0, "reason": "反思群聊行为"}}}})["result"]
        self.assertFalse(result["isError"])
        execute("finish", context)
        with self.assertRaises(ConsoleFileError):
            execute("contacts", context, operation="upsert", params={"user_id": "123", "revision": 1,
                "reason": "迟到", "notes": "不能保存"})
        self.assertEqual(execute("preset", context, operation="show")["text"], "倾听群友的完整表达。")
        action_enum = tools_schema()[0]["inputSchema"]["properties"]["action"]["enum"]
        self.assertIn("contacts", action_enum)
        self.assertIn("preset", action_enum)

    def test_workers_other_personas_and_emotion_cannot_edit(self):
        for flags in ({"agent_worker_id": "w"}, {"sandbox_session_id": "s"}, {"parent_request_id": "r"},
                      {"activity_controller": True}, {"emotion_analysis": True}, {"main_agent": False}):
            context = self.context(**flags)
            with self.subTest(flags=flags), self.assertRaises(PermissionError):
                execute("preset", context, operation="update", params={"text": "不应写入", "revision": 0, "reason": "测试"})
        context = self.context()
        with self.assertRaises(PermissionError):
            execute("contacts", context, operation="upsert", params={"user_id": "invented", "revision": 0, "reason": "猜测"})
        execute("contacts", context, operation="upsert", params={"user_id": "123", "revision": 0, "reason": "认识此人", "notes": "资料"})
        self.assertEqual(execute("contacts", self.context(identity_owner="another-persona"), operation="list")["contacts"], [])

    def test_preset_history_conflict_rollback_and_reset(self):
        context = self.context()
        execute("preset", context, operation="update", params={"text": "喜欢讨论配乐。", "revision": 0, "reason": "实际阅读"})
        with self.assertRaises(ConsoleFileError):
            execute("preset", context, operation="reset", params={"revision": 0, "reason": "过期"})
        execute("preset", context, operation="update", params={"text": "耐心听完。", "revision": 1, "reason": "群聊反思"})
        history = execute("preset", context, operation="history")["history"]
        execute("preset", context, operation="update", params={"text": history[1]["value"]["text"], "revision": 2, "reason": "回退"})
        self.assertEqual(execute("preset", context, operation="show")["text"], "喜欢讨论配乐。")
        execute("preset", context, operation="reset", params={"revision": 3, "reason": "恢复"})
        self.assertEqual(execute("preset", context, operation="show")["base_prompt"], "基础人物")
        self.assertEqual(execute("preset", context, operation="show")["text"], "")

    def test_archives_durable_deduplicated_clear_keeps_preset(self):
        payload = {"path": str(self.home / "archive.json"), "version": "v1", "summary": "群聊"}
        first = self.store.enqueue_archive("a", "g", "key", payload)
        self.assertEqual(first, self.store.enqueue_archive("a", "g", "key", payload))
        self.assertEqual(len(IdentityStore(self.store.path).pending("a", "g")), 1)
        self.store.acknowledge("other-persona", "g", [first])
        self.assertEqual(len(self.store.pending("a", "g")), 1)
        self.store.acknowledge("a", "g", [first])
        self.assertEqual(self.store.pending("a", "g"), [])
        self.store.enqueue_archive("a", "g", "key", {**payload, "version": "v2"})
        self.store.edit_preset("a", "成长", 0, "反思", "m")
        self.store.observe("a", "g", "1", "林")
        self.store.clear("a", "g")
        self.assertEqual(self.store.contacts("a", "g")["contacts"], [])
        self.assertEqual(self.store.pending("a", "g"), [])
        self.assertEqual(self.store.preset("a")["text"], "成长")


class IdentityMainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_natural_final_continues_same_context_then_edits_and_finishes_both_transports(self):
        for official in (False, True):
            bot, key = self.fixture.bot(official, stored_cid="persistent-cid")
            coordinator = bot._get_main_coordinator()
            seen = []
            async def model(session_key, prompt, **options):
                context = prompt_object(prompt, "console_context_file")["console_context_file"]
                seen.append(context)
                if len(seen) == 1:
                    execute("reply", context, text="以后叫你阿林。")
                    return "回复完成，但还有记录要保存"
                self.assertEqual(prompt_object(prompt, "type")["type"], "turn_continuation")
                execute("contacts", context, operation="upsert", params={"user_id": "456", "revision": 0,
                    "reason": "本人要求", "preferred_name": "阿林", "speaking_style": "常用短句"})
                execute("preset", context, operation="update", params={"text": "讨论时先听完群友的表达。", "revision": 0,
                    "reason": "反思自身对话"})
                execute("finish", context)
                return ""
            bot.ai.chat = AsyncMock(side_effect=model)
            with patch.object(coordinator, "start"), patch.object(coordinator, "poll", new=AsyncMock()):
                await self.fixture.receive(bot, official, "m1", "以后叫我阿林")
                self.assertEqual(len(seen), 2)
                self.assertEqual(seen[0], seen[1])
                self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "persistent-cid")
                self.assertIn("讨论时先听完群友的表达。", coordinator.system_for(key))
                bot.ai.chat = ConsoleAgentMock(return_value="你好，阿林。")
                await self.fixture.receive(bot, official, "m2", "还记得我吗", elapsed=1)
                next_prompt = bot.ai.chat.await_args.args[1]
                people = prompt_object(next_prompt, "address_book")["address_book"]["people"]
                self.assertEqual(people[0]["preferred_name"], "阿林")
                self.assertIn('"type": "growth_update"', next_prompt)
                if not official:
                    self.assertEqual(bot.get_cached_member_name("12345678", "456"), "阿林")

    async def test_archive_maintenance_is_main_queued_cli_and_retry_survives_failure(self):
        bot, key = self.fixture.bot(stored_cid="archive-cid")
        coordinator = bot._get_main_coordinator()
        binding = bind(bot, key)
        owner, scope = binding["identity_owner"], "onebot:" + bot.account_key + ":group:12345678"
        archive = Path("archive.json").resolve()
        archive.write_text(json.dumps({"summary_complete": True, "summary": "群友称呼自己阿林。", "participants": {"456": "阿林"}}), encoding="utf-8")
        archive_ready(bot, owner, "12345678", archive)
        archive_ready(bot, owner, "12345678", archive)
        self.assertEqual(len(coordinator.identity.pending(owner, scope)), 1)
        coordinator.identity.observe(owner, scope, "456", "林")
        bot.ai.chat = AsyncMock(side_effect=RuntimeError("backend unavailable"))
        with patch.object(coordinator, "start"), patch.object(coordinator, "poll", new=AsyncMock()):
            with self.assertRaises(RuntimeError):
                await coordinator.chat(key, "整理", idle_event=True, activity_scope=scope, memory_maintenance=True)
            self.assertEqual(len(coordinator.identity.pending(owner, scope)), 1)
            async def model(session_key, prompt, **options):
                context = prompt_object(prompt, "console_context_file")["console_context_file"]
                with self.assertRaises(PermissionError):
                    execute("reply", context, text="不应群发")
                execute("contacts", context, operation="upsert", params={"user_id": "456", "revision": 0,
                    "reason": "归档核对", "preferred_name": "阿林"})
                pending = execute("memory", context, operation="pending")["archives"]
                execute("memory", context, operation="review", params={"archive_id": pending[0]["archive_id"],
                    "decision": "keep", "revision": 0, "reason": "称呼变化已落实到通讯录，无需更改自己的行为预设。"})
                execute("finish", context)
                return ""
            bot.ai.chat = AsyncMock(side_effect=model)
            await coordinator.chat(key, "整理", idle_event=True, activity_scope=scope, memory_maintenance=True)
        self.assertEqual(coordinator.identity.pending(owner, scope), [])
        self.assertEqual(coordinator.identity.get(owner, scope, "456")["preferred_name"], "阿林")
        bot.send_group_message.assert_not_awaited()
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "archive-cid")

    async def test_maintenance_and_next_message_share_reply_queue(self):
        bot, key = self.fixture.bot(stored_cid="queue-cid")
        coordinator = bot._get_main_coordinator()
        scope = "onebot:" + bot.account_key + ":group:12345678"
        started, release = asyncio.Event(), asyncio.Event()
        order = []
        async def model(session_key, prompt, **options):
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            broker, token = ConsoleBroker.from_context(context)
            if broker.context(token).get("memory_maintenance"):
                order.append("maintain")
                started.set()
                await release.wait()
                execute("finish", context)
                order.append("finish")
            else:
                order.append("message")
                execute("reply", context, text="记得。")
                execute("finish", context)
            return ""
        bot.ai.chat = AsyncMock(side_effect=model)
        with patch.object(coordinator, "start"), patch.object(coordinator, "poll", new=AsyncMock()):
            maintenance = asyncio.create_task(coordinator.chat(key, "整理", idle_event=True,
                activity_scope=scope, memory_maintenance=True))
            await asyncio.wait_for(started.wait(), 2)
            next_message = asyncio.create_task(self.fixture.receive(bot, False, "next", "还记得吗？"))
            try:
                await asyncio.sleep(.05)
                self.assertEqual(order, ["maintain"])
                release.set()
                await asyncio.wait_for(asyncio.gather(maintenance, next_message), 5)
            finally:
                release.set()
                for task in (maintenance, next_message):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(maintenance, next_message, return_exceptions=True)
        self.assertEqual(order, ["maintain", "finish", "message"])
        bot.send_group_message.assert_awaited_once()

    async def test_group_archive_hook_startup_restore_and_clear(self):
        bot, key = self.fixture.bot(stored_cid="restore-cid")
        bot.app_config.bot.memory_enabled = True
        coordinator = bot._get_main_coordinator()
        from bot_identity import scope_for
        binding = bind(bot, key)
        owner, scope = binding["identity_owner"], scope_for(bot, "12345678")
        bot.memory_mgr.set_participant_nickname("12345678", "456", "小林")
        bot.memory_mgr.record_session_message("12345678", "456", "小林", "我习惯先听完。")
        digest = {"summary": "小林先听完再说。", "member_portraits": {"456": "习惯先听完"}, "participants": {"456": "小林"}}
        folded = {"complete": True, "digest": digest, "chunks": []}
        with patch("memory_digest.summarize_records", new=AsyncMock(return_value=folded)):
            saved = await bot.memory_mgr.summarize_and_save_session(bot.ai)
        self.assertTrue(saved)
        pending = coordinator.identity.pending(owner, scope)
        self.assertEqual(len(pending), 1)
        await coordinator._prepare(key)
        self.assertEqual(len(coordinator.identity.pending(owner, scope)), 1)
        bot.ai.chat.assert_not_awaited()
        self.assertEqual(coordinator.identity.get(owner, scope, "456")["platform_name"], "小林")
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "restore-cid")
        coordinator.identity.edit_preset(owner, "保持耐心。", 0, "成长", "m")
        bot.memory_mgr.clear_group_memory("12345678")
        self.assertEqual(coordinator.identity.pending(owner, scope), [])
        self.assertEqual(coordinator.identity.contacts(owner, scope)["contacts"], [])
        self.assertEqual(coordinator.identity.preset(owner)["text"], "保持耐心。")

    async def test_persona_rename_keeps_growth_and_contact_identity(self):
        bot, key = self.fixture.bot(stored_cid="rename-cid")
        coordinator = bot._get_main_coordinator()
        from persona_manager import PersonaPreset
        before = bind(bot, key)
        preset = persona_for(bot, "12345678", key)
        coordinator.identity.edit_preset(before["identity_owner"], "喜爱讨论音乐。", 0, "阅读", "m")
        replacement = preset.model_copy(update={"name": preset.name + "改名"})
        bot.personas.update_preset(preset.name, replacement)
        after = bind(bot, key)
        self.assertEqual(before["identity_owner"], after["identity_owner"])
        self.assertIn("喜爱讨论音乐。", coordinator.system_for(key))
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "rename-cid")

    async def test_scheduler_drains_more_than_one_batch_without_retrying_failed_batch(self):
        bot, key = self.fixture.bot(stored_cid="batch-cid")
        coordinator = bot._get_main_coordinator()
        owner = bind(bot, key)["identity_owner"]
        scope = "onebot:" + bot.account_key + ":group:12345678"
        path = Path("summary.json").resolve()
        path.write_text('{"summary":"归档"}', encoding="utf-8")
        for index in range(7):
            coordinator.identity.enqueue_archive(owner, scope, key, {"path": str(path), "version": str(index), "summary": "归档"})
        bot.ai.chat = ConsoleAgentMock(return_value="")
        with patch.object(coordinator, "start"), patch.object(coordinator, "poll", new=AsyncMock()):
            await coordinator.schedule_identity_maintenance()
            await asyncio.wait_for(coordinator.identity_tasks[key], 3)
            self.assertEqual(len(coordinator.identity.pending(owner, scope)), 3)
            coordinator.identity_scan_after = 0
            await coordinator.schedule_identity_maintenance()
            await asyncio.wait_for(coordinator.identity_tasks[key], 3)
            self.assertEqual(coordinator.identity.pending(owner, scope), [])
            coordinator.identity.enqueue_archive(owner, scope, key, {"path": str(path), "version": "fail", "summary": "归档"})
            bot.ai.chat = AsyncMock(side_effect=RuntimeError("backend unavailable"))
            coordinator.identity_scan_after = 0
            await coordinator.schedule_identity_maintenance()
            await asyncio.wait_for(coordinator.identity_tasks[key], 3)
            self.assertEqual(bot.ai.chat.await_count, 1)
            coordinator.identity_scan_after = 0
            await coordinator.schedule_identity_maintenance()
            self.assertEqual(bot.ai.chat.await_count, 1)
            self.assertEqual(len(coordinator.identity.pending(owner, scope)), 1)

    async def test_preset_show_and_growth_use_the_effective_bound_actor_foundation(self):
        bot, key = self.fixture.bot(stored_cid="actor-cid")
        from agent_core.configuration import AgentConfig
        from persona_isolation import namespace
        bot.app_config.agent = AgentConfig.model_validate({"actors": {"reader": {
            "name": "Reader", "prompt": "绑定角色的真实基础预设。"}}, "autonomous_model_enabled": False,
            "bindings": {namespace(bot): "reader"}})
        coordinator = bot._get_main_coordinator()
        observed = []
        async def model(session_key, prompt, **options):
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            observed.append(execute("preset", context, operation="show")["base_prompt"])
            execute("preset", context, operation="update", params={"text": "读后先分辨事实和感受。", "revision": 0,
                "reason": "实际阅读后的反思"})
            execute("reply", context, text="记下了。")
            execute("finish", context)
            return ""
        bot.ai.chat = AsyncMock(side_effect=model)
        with patch.object(coordinator, "start"), patch.object(coordinator, "poll", new=AsyncMock()):
            await self.fixture.receive(bot, False, "m", "记得阅读时的感想")
        self.assertEqual(observed, ["绑定角色的真实基础预设。"])
        self.assertIn("绑定角色的真实基础预设。", coordinator.system_for(key))
        self.assertIn("读后先分辨事实和感受。", coordinator.system_for(key))
        self.assertEqual(bot._actor_runtime.store.actor("reader")["prompt"], "绑定角色的真实基础预设。")
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "actor-cid")
