# -*- coding: utf-8 -*-
"""Verify all 14 code review fixes (R1-R14) from CODE_REVIEW_REPORT_2026-10-29.md."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
from test_ai_fixture import mock_agent, drain_runtime

from test_ai_fixture import ConsoleAgentMock

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import unittest

ROOT = Path(__file__).resolve().parent

class ReviewFixesVerification(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="test_fixes_")
        self.base = Path(self.tmp_dir.name)

    def tearDown(self):
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_r1_group_memory_prefix_isolation(self):
        """R1: clear_group_memory('12345') must not delete group_123456.json or group_123_456.json, even if corrupt/empty"""
        from group_memory import GroupMemoryManager
        mgr = GroupMemoryManager(storage_dir=str(self.base))
        p1 = self.base / "group_12345.json"
        p2 = self.base / "group_123456.json"
        p1.write_text('{"group_openid": "12345", "total_dialog_turns": 1}', encoding="utf-8")
        p2.write_text('{"group_openid": "123456", "total_dialog_turns": 2}', encoding="utf-8")
        p_sub = self.base / "group_123.json"
        p_other = self.base / "group_123_456.json"
        p_other_arch = self.base / "group_123_456_20261029_112235.json"
        p_other_empty = self.base / "group_123_456_20261029_112236.json"
        p_other_corrupt = self.base / "group_123_456_20261029_112237.json"
        p_sub.write_text('{"group_openid": "123", "total_dialog_turns": 1}', encoding="utf-8")
        p_other.write_text('{"group_openid": "123_456", "total_dialog_turns": 3}', encoding="utf-8")
        p_other_arch.write_text('{"group_openid": "123_456", "total_dialog_turns": 3, "summary": "OTHER SECRET"}', encoding="utf-8")
        p_other_empty.write_text('{}', encoding="utf-8")
        p_other_corrupt.write_text('corrupt_json', encoding="utf-8")

        mgr.clear_group_memory("12345")
        self.assertFalse(p1.exists(), "group_12345.json should be deleted")
        self.assertTrue(p2.exists(), "group_123456.json should NOT be deleted")

        # Verify load_recent_memories('123') does not load archives of group '123_456'
        mems = mgr.load_recent_memories("123")
        self.assertEqual(len(mems), 0, "group 123 must not load archives of group 123_456")

        mgr.clear_group_memory("123")
        self.assertFalse(p_sub.exists(), "group_123.json should be deleted")
        self.assertTrue(p_other.exists(), "group_123_456.json should NOT be deleted")
        self.assertTrue(p_other_arch.exists(), "group_123_456 archive should NOT be deleted")
        self.assertTrue(p_other_empty.exists(), "group_123_456 empty archive should NOT be deleted")
        self.assertTrue(p_other_corrupt.exists(), "group_123_456 corrupt archive should NOT be deleted")

    async def test_r2_clear_all_memories_and_in_flight_summary(self):
        """R2: clear_all_memories clears session messages, packages, and discards in-flight summary across multiple groups"""
        from group_memory import GroupMemoryManager
        mgr = GroupMemoryManager(storage_dir=str(self.base))
        pkg_dir = self.base / "packages"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "test.json").write_text("{}", encoding="utf-8")
        mgr.record_session_message("grp_1", "user1", "Alice", "hello", False)
        self.assertIn("grp_1", mgr._session_messages)

        mgr.clear_all_memories()
        self.assertEqual(len(mgr._session_messages), 0)
        self.assertFalse(pkg_dir.exists())

        # Multi-group in-flight summary race
        async def slow_chat(session_key, *args, **kwargs):
            if "grp_multi_1" in session_key:
                mgr.clear_group_memory("grp_multi_2")
            return '{"character_portraits": {}, "summary": "race"}'

        class FakeAI:
            chat = staticmethod(slow_chat)

        mgr.record_session_message("grp_multi_1", "u1", "A", "msg 1", False)
        mgr.record_session_message("grp_multi_2", "u2", "B", "msg 2", False)
        res = await mgr.summarize_and_save_session(FakeAI())
        self.assertIn("grp_multi_1", res)
        self.assertNotIn("grp_multi_2", res, "Cleared group 2 must not be archived")

    def test_r3_sync_system_prompt_detects_group_persona_change(self):
        """Persona overrides use deltas; authority changes still invalidate work."""
        from config import AppConfig, GroupCustomConfig
        from persona_manager import PersonaManager, PersonaPreset
        from group_memory import GroupMemoryManager
        from onebot_client import OneBotClient
        cfg = AppConfig()
        cfg.bot.group_settings = {"1001": GroupCustomConfig(group_id="1001", persona="PersonaA")}
        ai = mock_agent()
        personas = MagicMock(spec=PersonaManager)
        p_active = PersonaPreset(name="Active", prompt="prompt active")
        p_a = PersonaPreset(name="PersonaA", prompt="prompt A")
        p_b = PersonaPreset(name="PersonaB", prompt="prompt B")
        personas.active_persona_name = "Active"
        personas.presets = {"Active": p_active, "PersonaA": p_a, "PersonaB": p_b}
        personas.get_active_persona.return_value = p_active
        personas.get_persona.side_effect = lambda n: p_a if n == "PersonaA" else (p_b if n == "PersonaB" else p_active)
        personas.get_preset.side_effect = lambda n: p_a if n == "PersonaA" else (p_b if n == "PersonaB" else p_active)
        bot = OneBotClient(cfg, ai, personas, GroupMemoryManager(storage_dir=str(self.base)))
        ai.reset_session.reset_mock()

        # Change group persona override
        cfg.bot.group_settings = {"1001": GroupCustomConfig(group_id="1001", persona="PersonaB")}
        bot.sync_system_prompt()
        ai.reset_session.assert_not_called()
        self.assertIn("prompt B", bot._get_main_coordinator().system_for("onebot_group_1001"))

        # Change group master_title
        ai.reset_session.reset_mock()
        cfg.bot.group_settings = {"1001": GroupCustomConfig(group_id="1001", persona="PersonaB", master_title="主人司令")}
        bot.sync_system_prompt()
        ai.reset_session.assert_any_call(bot._chat_session_key("onebot_group_1001"))
        ai.reset_session.assert_any_call("__work__:" + bot._chat_session_key("onebot_group_1001"))

    async def test_r4_temporary_chats_ignore_legacy_queue_limits(self):
        """Temporary conversations have no legacy pending-job ceiling."""
        from conversation_runtime import ConversationRuntime, EventContext, SendError, RuntimeStore
        store = RuntimeStore(str(self.base / "rt4.db"), "test_ns")
        rt = ConversationRuntime(store, max_pending_jobs=2, per_conversation_queue_limit=2)
        evt = EventContext("scope_1", "m1", 0, owner="owner_1")
        entered, release = set(), asyncio.Event()
        async def work(index):
            entered.add(index)
            await release.wait()
        try:
            for index in range(40):
                rt.submit_work(evt, f"task {index}", lambda index=index: work(index))
            await asyncio.sleep(0)
            self.assertEqual(entered, set(range(40)))
            release.set()
            await rt.work_worker
        finally:
            await rt.close()
            store.db.close()

    async def test_r5_cancel_specific_job(self):
        """R5: cancel with job_id targets only that job; invalid job_id leaves all jobs intact"""
        from conversation_runtime import ConversationRuntime, EventContext, RuntimeStore
        store = RuntimeStore(str(self.base / "rt5.db"), "test_ns")
        rt = ConversationRuntime(store)
        evt = EventContext("scope_1", "m1", 0, owner="owner_1")
        j1 = rt.submit_work(evt, "task 1", lambda: asyncio.sleep(10))
        j2 = rt.submit_work(evt, "task 2", lambda: asyncio.sleep(10))
        # Cancel with invalid ID should not cancel any job
        rt.cancel(owner="owner_1", job_id="non_existent_id")
        s1 = next(row["state"] for row in rt.store.jobs("owner_1") if row["id"] == j1)
        s2 = next(row["state"] for row in rt.store.jobs("owner_1") if row["id"] == j2)
        self.assertEqual(s1, "queued")
        self.assertEqual(s2, "queued")

        rt.cancel(owner="owner_1", job_id=j1)
        s1 = next(row["state"] for row in rt.store.jobs("owner_1") if row["id"] == j1)
        s2 = next(row["state"] for row in rt.store.jobs("owner_1") if row["id"] == j2)
        self.assertEqual(s1, "cancelled")
        self.assertEqual(s2, "queued")

        # Test cancelling without job_id when a job is in ready state
        j3 = rt.submit_work(evt, "task 3", lambda: asyncio.sleep(10))
        rt.store.db.execute("UPDATE work SET state='ready' WHERE id=?", (j3,))
        rt.store.db.commit()
        j4 = rt.submit_work(evt, "task 4", lambda: asyncio.sleep(10))
        jobs = rt.store.jobs("owner_1")
        recent = [j for j in jobs if j.get("state") in ("queued", "running", "ready")]
        self.assertEqual(recent[0]["id"], j4)
        rt.cancel(owner="owner_1", job_id=recent[0]["id"])
        s3 = next(row["state"] for row in rt.store.jobs("owner_1") if row["id"] == j3)
        s4 = next(row["state"] for row in rt.store.jobs("owner_1") if row["id"] == j4)
        self.assertEqual(s3, "ready")
        self.assertEqual(s4, "cancelled")
        await rt.close()

    async def test_r6_cancel_ready_job(self):
        """R6: cancelling ready job transitions it to cancelled, prevents sending, and updates runtime work"""
        from media_jobs import MediaService
        from config import MediaConfig
        from messaging import SendReceipt
        from media_models import MessageEnvelope, MediaAsset, MediaIntent
        from conversation_runtime import ConversationRuntime, RuntimeStore
        cfg = MediaConfig(database_path=str(self.base / "m6.sqlite3"), search_provider="auto")
        svc = MediaService(cfg)
        ctx = MessageEnvelope("onebot", "account", "12345", "u", "1", "", AsyncMock(), is_group=True)
        asset = MediaAsset(kind="file", title="test.txt", local_path="test.txt")
        svc.store.put_asset(ctx.scope, asset)
        job, _ = svc.store.create_job(ctx.owner, ctx.scope, "original")
        svc.update(job, "ready", assets=[asset.asset_id])
        svc.sender.send = AsyncMock(return_value=SendReceipt(message_id="receipt"))
        await svc.control(ctx, MediaIntent(operation="cancel", job_id=job["id"]))
        after_cancel = svc.store.job(ctx.owner, job["id"])["state"]
        self.assertEqual(after_cancel, "cancelled")
        await svc.control(ctx, MediaIntent(operation="status", job_id=job["id"]))
        self.assertEqual(svc.sender.send.await_count, 0)

        # Verify runtime cancellation of ready state in work table
        store = RuntimeStore(str(self.base / "m6_rt.db"), "onebot:account")
        store.db.execute("INSERT INTO work VALUES(?,?,?,?,?,?,?,?,?)",
            (job["id"], "onebot:account", ctx.scope, ctx.owner, "1", "label", "ready", "", 0.0))
        store.db.commit()
        rt = ConversationRuntime(store)
        rt.cancel(owner=ctx.owner, job_id=job["id"])
        db_state = store.db.execute("SELECT state FROM work WHERE id=?", (job["id"],)).fetchone()[0]
        self.assertEqual(db_state, "cancelled")
        await rt.close()

    async def test_r7_candidate_delivery_success(self):
        """R7: candidate list delivery produces success=True and completed runtime work state"""
        from media_jobs import MediaService
        from config import MediaConfig
        from media_models import MessageEnvelope, MediaAsset, MediaIntent
        from conversation_runtime import ConversationRuntime, RuntimeStore, EventContext
        from skills.base import SkillResult
        cfg = MediaConfig(database_path=str(self.base / "m7.sqlite3"), search_provider="auto")
        svc = MediaService(cfg)
        ctx = MessageEnvelope("onebot", "account", "12345", "u", "msg_candidate_7", "", AsyncMock(), is_group=True)
        svc.searcher.search = AsyncMock(return_value=[MediaAsset(kind="image", title="cat", url="https://example.org/cat.png")])
        outcome = {}
        await svc.handle(ctx, MediaIntent(operation="search", kind="image", query="cat"), outcome=outcome)
        self.assertTrue(outcome.get("reply_handled"))
        self.assertTrue(outcome.get("success"), "candidate list should have success=True")

        # Verify runtime work state transition
        store = RuntimeStore(str(self.base / "rt7.db"), "test_ns")
        rt = ConversationRuntime(store)
        async def work_cb():
            from conversation_runtime import current_event
            current_event.get().result = SkillResult(bool(outcome.get("success")), outcome.get("reply_text"), action_type="media", data=outcome)
        jid = rt.submit_work(EventContext(ctx.scope, "m7", 0, owner=ctx.owner), "search", work_cb)
        await rt.work_worker
        st = next(row["state"] for row in rt.store.jobs(ctx.owner) if row["id"] == jid)
        self.assertEqual(st, "completed", "candidate delivery must mark runtime work completed, not failed")
        await rt.close()

    async def test_r8_legacy_speech_limits_do_not_interrupt_chat_and_mentions_remain_optional(self):
        """Replies reach QQ despite legacy limits without automatic mentions."""
        from config import AppConfig
        from antigravity_client import AntigravityClient
        from onebot_client import OneBotClient
        from persona_manager import PersonaManager
        from group_memory import GroupMemoryManager
        from unittest.mock import patch
        cfg = AppConfig()
        cfg.bot.autonomous_enabled = True
        cfg.bot.mention_probability = 0
        cfg.bot.cooldown_seconds = 3600
        cfg.bot.runtime_database_path = str(self.base / "r8-runtime.sqlite3")
        cfg.media.database_path = str(self.base / "r8-media.sqlite3")
        cfg.media.cache_dir = str(self.base / "r8-cache")
        ai = mock_agent()
        ai.chat = ConsoleAgentMock(return_value='{"should_reply": true, "reply": "test reply", "action": "at", "target_user_id": "67890"}')
        bot = OneBotClient(cfg, ai, PersonaManager(str(self.base / "r8-personas.yaml")), GroupMemoryManager(storage_dir=str(self.base)))
        bot.self_id = "999"
        bot.send_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "100"}})

        # Old zero probability must not suppress an AI-approved reply.
        cfg.bot.reply_probability = 0.0
        msg_data = {"post_type": "message", "message_type": "group", "group_id": 12345, "user_id": 67890,
                    "message_id": "1", "raw_message": "大家好", "sender": {"nickname": "小明"}}
        await bot._handle_group_message(msg_data)
        bot.send_action.assert_awaited_once()
        ai.chat.assert_awaited_once()
        self.assertFalse(any(seg["type"] == "at" for seg in bot.send_action.call_args.args[1]["message"]))

        # The model may decline one turn without silencing the next.
        cfg.bot.reply_probability = 0.5
        ai.chat.return_value = '{"should_reply": false, "reason": "无须插话"}'
        with patch("random.random", return_value=0.8):
            msg_data["message_id"] = "startup"
            await bot._handle_group_message(msg_data)
        self.assertEqual(ai.chat.await_count, 2)
        bot.send_action.assert_awaited_once()
        ai.chat.return_value = '{"should_reply": true, "reply": "test reply", "action": "at", "target_user_id": "67890"}'
        with patch("random.random", return_value=0.8):
            msg_data["message_id"] = "2"
            await bot._handle_group_message(msg_data)
            self.assertEqual(bot.send_action.await_count, 2)

        # Legacy model actions and mention settings do not add @ to plain chat.
        cfg.bot.mention_probability = 1.0
        with patch("random.random", return_value=0.2):
            msg_data["message_id"] = "3"
            await bot._handle_group_message(msg_data)
            self.assertEqual(bot.send_action.await_count, 3)
            payload = bot.send_action.await_args.args[1]
            at_count = sum(item["type"] == "at" for item in payload["message"])
            self.assertEqual(at_count, 0)
        await bot._close_runtime()

    async def test_r9_onebot_old_commands(self):
        """R9: /name and /status return True and execute correctly"""
        from config import AppConfig
        from antigravity_client import AntigravityClient
        from onebot_client import OneBotClient
        from persona_manager import PersonaManager
        from group_memory import GroupMemoryManager
        cfg = AppConfig()
        ai = mock_agent()
        ai._sessions = {}
        ai.ls_address = "127.0.0.1:7873"
        ai.chat = ConsoleAgentMock(return_value="命令审核")
        bot = OneBotClient(cfg, ai, PersonaManager("personas.yaml"), GroupMemoryManager(storage_dir=str(self.base)))
        bot.self_id = "999"
        bot.send_group_message = AsyncMock(return_value={"status": "ok"})
        h_name = await bot._handle_command("/name 自定义昵称", group_id="12345", user_id="67890", is_group=True)
        await drain_runtime(bot)
        h_status = await bot._handle_command("/status", group_id="12345", user_id="67890", is_group=True)
        await drain_runtime(bot)
        self.assertTrue(h_name)
        self.assertTrue(h_status)
        p = bot.memory_mgr.get_group_memory("12345").participants["67890"]
        self.assertIn("自定义昵称", p.nickname)
        await bot._close_runtime()

    async def test_r10_plain_quoted_text_uses_chat_lane(self):
        """R10: plain quoted text routes to chat lane, not blocked by work lane"""
        from config import AppConfig
        from antigravity_client import AntigravityClient
        from onebot_client import OneBotClient
        from persona_manager import PersonaManager
        from group_memory import GroupMemoryManager
        from conversation_runtime import EventContext
        cfg = AppConfig()
        cfg.bot.runtime_database_path = str(self.base / "runtime.sqlite3")
        ai = mock_agent()
        bot = OneBotClient(cfg, ai, PersonaManager("personas.yaml"), GroupMemoryManager(storage_dir=str(self.base)))
        rt = bot._get_runtime()
        entered, release, plain_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def block():
            entered.set()
            await release.wait()
        rt.submit_work(EventContext("another-group", owner="another-user"), "long task", block)
        await entered.wait()

        dispatched = []
        async def dispatch(data):
            dispatched.append(data["message_id"])
            if data["message_id"] == "quoted_2":
                plain_seen.set()
        bot._dispatch_event = dispatch
        bot._queue_event({"post_type": "message", "message_type": "private", "user_id": 67890,
                          "message_id": "quoted_1", "raw_message": "谢谢",
                          "message": [{"type": "reply", "data": {"id": "123"}}, {"type": "text", "data": {"text": "谢谢"}}]})
        # Ordinary Chinese phrase with 照 must not be misclassified as image inquiry
        bot._queue_event({"post_type": "message", "message_type": "private", "user_id": 67890,
                          "message_id": "quoted_2", "raw_message": "按照你的意思：好的",
                          "message": [{"type": "reply", "data": {"id": "124"}}, {"type": "text", "data": {"text": "按照你的意思：好的"}}]})
        await asyncio.wait_for(plain_seen.wait(), 2)
        self.assertIn("quoted_1", dispatched)
        self.assertIn("quoted_2", dispatched)
        release.set()
        await rt.work_worker
        await bot._close_runtime()

    async def test_r11_queued_outbox_does_not_commit_memory(self):
        """R11: offline queued outbox message does not record memory interaction in group or private chat"""
        from config import AppConfig
        from antigravity_client import AntigravityClient
        from onebot_client import OneBotClient
        from persona_manager import PersonaManager
        from group_memory import GroupMemoryManager
        cfg = AppConfig()
        cfg.bot.runtime_database_path = str(self.base / "runtime.sqlite3")
        ai = mock_agent()
        ai.chat = ConsoleAgentMock(return_value='{"should_reply": true, "reply": "test reply", "action": "none"}')
        bot = OneBotClient(cfg, ai, PersonaManager("personas.yaml"), GroupMemoryManager(storage_dir=str(self.base)))
        bot.self_id = "999"
        bot._get_runtime()
        # Group chat
        msg_data = {"post_type": "message", "message_type": "group", "group_id": 12345, "user_id": 67890,
                    "message_id": "1", "raw_message": "[CQ:at,qq=999] 你好", "sender": {"nickname": "小明"}}
        await bot._handle_group_message(msg_data)
        interactions = len(bot.memory_mgr.get_group_memory("12345").recent_interactions)
        self.assertEqual(interactions, 0, "offline queued outbox must not record interaction")

        # Private chat
        priv_data = {"post_type": "message", "message_type": "private", "user_id": 67890,
                     "message_id": "2", "raw_message": "你好", "sender": {"nickname": "小明"}}
        await bot._handle_private_message(priv_data)
        self.assertNotIn("onebot:account_1:private:67890:67890", bot._last_chat_replies)
        await bot._close_runtime()

    def test_r12_account_isolated_sessions(self):
        """R12: sessions file is isolated per account, and persists on account switch"""
        from antigravity_client import AntigravityClient
        s_file = str(self.base / "sessions.json")
        ai = AntigravityClient(sessions_file=s_file, account_key="my_custom_bot")
        self.assertIn("sessions_my_custom_bot.json", ai.sessions_file)
        # Create a session in my_custom_bot
        sess = ai.get_or_create_session("my_session_key")
        sess.conversation_id = "cid_custom_1"
        # Switch account
        ai.bind_account("second_custom_account")
        self.assertIn("sessions_second_custom_account.json", ai.sessions_file)
        self.assertNotIn("my_custom_bot", ai.sessions_file)
        # Verify previous account session was saved to disk
        ai_reloaded = AntigravityClient(sessions_file=s_file, account_key="my_custom_bot")
        self.assertEqual(ai_reloaded.get_or_create_session("my_session_key").conversation_id, "cid_custom_1")

    async def test_r13_file_skills_security_checks(self):
        """R13: SendFileSkill enforces media enabled, file_enabled, allowed roots, and size limit"""
        from config import AppConfig
        from skills.base import SkillContext
        from skills.file_skills import SendFileSkill
        skill = SendFileSkill()

        # 1. media.enabled = False
        cfg1 = AppConfig()
        cfg1.media.enabled = False
        res1 = await skill.execute({"file_name": "test.txt", "content": "hello"}, SkillContext("scope", "123", "u", app_config=cfg1))
        self.assertFalse(res1.success)
        self.assertIn("媒体服务已禁用", res1.message)

        # 2. media.file_enabled = False
        cfg2 = AppConfig()
        cfg2.media.enabled = True
        cfg2.media.file_enabled = False
        res2 = await skill.execute({"file_name": "test.txt", "content": "hello"}, SkillContext("scope", "123", "u", app_config=cfg2))
        self.assertFalse(res2.success)
        self.assertIn("文件发送功能已禁用", res2.message)

        # 3. Source file outside allowed roots
        cfg3 = AppConfig()
        cfg3.media.enabled = True
        cfg3.media.file_enabled = True
        outside = self.base / "outside_secret.txt"
        outside.write_text("secret_data", encoding="utf-8")
        res3 = await skill.execute({"file_name": "stolen.txt", "file_path": str(outside)}, SkillContext("scope", "123", "u", app_config=cfg3))
        self.assertFalse(res3.success)
        self.assertIn("授权资料库", res3.message)

        # 4. Content exceeding max_file_mb
        cfg4 = AppConfig()
        cfg4.media.max_file_mb = 1
        res4 = await skill.execute({"file_name": "big.txt", "content": "x" * (2 * 1024 * 1024)}, SkillContext("scope", "123", "u", app_config=cfg4))
        self.assertFalse(res4.success)
        self.assertIn("超出限制", res4.message)

        # 5. Path traversal attempts with file_name
        cfg5 = AppConfig()
        cfg5.media.enabled = True
        cfg5.media.file_enabled = True
        cfg5.media.cache_dir = str(self.base / "cache")
        res5 = await skill.execute({"file_name": "../traversal.txt", "content": "traversal"}, SkillContext("scope", "123", "u", app_config=cfg5))
        self.assertFalse((self.base / "cache" / "traversal.txt").exists(), "Must not write to parent of exports")
        self.assertTrue((self.base / "cache" / "exports" / "traversal.txt").exists(), "Must stay contained in exports")

        # 6. file_name is ".."
        res6 = await skill.execute({"file_name": "..", "content": "dots"}, SkillContext("scope", "123", "u", app_config=cfg5))
        self.assertTrue((self.base / "cache" / "exports" / "document.txt").exists(), "Must fallback to document.txt")

        # 7. Self-copy does not raise PermissionError
        existing_export = self.base / "cache" / "exports" / "existing.txt"
        existing_export.write_text("existing content", encoding="utf-8")
        res7 = await skill.execute({"file_name": "existing.txt", "file_path": str(existing_export)}, SkillContext("scope", "123", "u", app_config=cfg5))
        self.assertNotIn("WinError", res7.message)

    def test_r14_sandbox_validation_completeness(self):
        """R14: validate.py contains skills, master, ui packages and valid AST"""
        import ast
        val_path = ROOT / "implementation_artifacts" / "validate.py"
        if not val_path.exists():
            val_path = Path("G:/qqbot/implementation_artifacts/validate.py")
        code = val_path.read_text(encoding="utf-8")
        tree = ast.parse(code)
        self.assertIn("skills", code)
        self.assertIn("master", code)
        self.assertIn("ui", code)

if __name__ == "__main__":
    unittest.main()
