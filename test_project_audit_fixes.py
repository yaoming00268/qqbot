"""Offline behavior regressions for the 2026-11-05 project audit."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent_work import run_agent_work
from config import AppConfig, GroupCustomConfig, WorkPermissions
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from conversation_runtime import EventContext
from main_chat import MainChat
from media_downloader import MediaError
from onebot_client import OneBotClient
from persona_isolation import effective_persona
from persona_manager import PersonaManager, PersonaPreset
from prompt_builder import compose_system_prompt
import test_startup_participation as fixtures

QT_APP = None


def qt_app():
    global QT_APP
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QT_APP = QApplication.instance() or QApplication([])
    return QT_APP


class RuntimeAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_reconnect_preserves_coordinator_work_and_services(self):
        bot, key = self.fixture.bot()
        bot.ws = SimpleNamespace(closed=False)
        bot.send_action = AsyncMock(return_value={"status": "ok", "data": {"user_id": 999, "nickname": "Fixture"}})
        blocker, entered = asyncio.Event(), asyncio.Event()

        async def watcher(_coordinator):
            await blocker.wait()

        async def work():
            entered.set()
            await blocker.wait()

        with patch.object(MainChat, "_watch", watcher), patch.object(MainChat, "prepare_startup", lambda obj: obj.start()):
            coordinator = bot._get_main_coordinator()
            coordinator.start()
            worker = coordinator.worker
            ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
                message_id="reconnect", text="background work", is_group=True)
            _, token, _ = coordinator.issue_chat_context(ctx, key)
            request = coordinator.broker.submit(token, "task", "offline work")
            runtime = bot._get_runtime()
            services = (bot._get_media_service(), bot._chat_turns, bot._proactive_chat, bot._text_outbox)
            job = runtime.submit_work(EventContext(ctx.scope, ctx.message_id, ctx.received_at, owner=ctx.owner), "audit", work)
            coordinator.broker.update(request["id"], "running", {"runtime_job_id": job})
            await entered.wait()
            for _ in range(3):
                await bot._fetch_login_info()
                self.assertIs(bot._get_main_coordinator(), coordinator)
                self.assertIs(coordinator.worker, worker)
                self.assertIs(bot._get_runtime(), runtime)
                self.assertEqual(coordinator.broker.record(request["id"])["state"], "running")
                self.assertFalse(runtime.active_work[job][1].done())
                self.assertFalse(worker.done())
                self.assertEqual(services, (bot._get_media_service(), bot._chat_turns, bot._proactive_chat, bot._text_outbox))

    async def test_stopping_allows_clean_runtime_reinitialization(self):
        bot, _ = self.fixture.bot()
        coordinator = bot._get_main_coordinator()
        await bot._close_runtime()
        self.assertTrue(coordinator.closed)
        bot._init_media_runtime()
        self.assertIsNot(bot._get_main_coordinator(), coordinator)

    async def test_reset_or_persona_switch_cancels_remaining_reply_chunks(self):
        for change in ("reset", "persona", "private_reset", "new_message"):
            with self.subTest(change=change):
                bot, key = self.fixture.bot()
                is_group = change != "private_reset"
                target = "12345678" if is_group else "456"
                if not is_group:
                    key = bot._chat_session_key("onebot_private_456")
                bot.app_config.bot.max_reply_length = 5
                bot.app_config.bot.enable_action_descriptions = True
                bot.ws = SimpleNamespace(closed=False)
                bot.send_group_message = OneBotClient.send_group_message.__get__(bot, OneBotClient)
                coordinator, runtime = bot._get_main_coordinator(), bot._get_runtime()
                ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id=target,
                    message_id="chunk-" + change, text="reply", is_group=is_group)
                bot._chat_turns.observe(ctx.scope, ctx.message_id, ctx.sender_id, ctx.text, ctx.received_at)
                context_file, token, _ = coordinator.issue_chat_context(ctx, key)
                execute("reply", context_file, text="ABCDEFGHIJ")
                ctx.console_token, ctx.cli_messages = token, coordinator.broker.seal_messages(token)
                submitted = []

                async def transport(_action, params, **_options):
                    submitted.append(params["message"][-1]["data"]["text"])
                    if len(submitted) == 1:
                        if change in ("reset", "private_reset"):
                            bot.ai.reset_session(key)
                            bot._text_outbox.cancel(ctx.scope)
                        elif change == "persona":
                            bot.personas.set_active(next(name for name in bot.personas.presets
                                                         if name != bot.personas.active_persona_name))
                        else:
                            bot._chat_turns.observe(ctx.scope, "new-message", ctx.sender_id, "follow-up", ctx.received_at + 1)
                    return {"status": "ok", "retcode": 0, "data": {"message_id": str(len(submitted))}}

                bot.send_action = AsyncMock(side_effect=transport)
                bot._text_outbox.send = bot.send_action
                result = await bot._send_decision_messages(None, ctx, "Fixture")
                self.assertEqual(submitted, ["ABCDE", "FGHIJ"] if change == "new_message" else ["ABCDE"])
                if change == "new_message":
                    self.assertEqual(result, "ABCDEFGHIJ")
                else:
                    self.assertFalse(ctx.guard())
                    self.assertEqual(result, "")
                    self.assertNotIn("last_delivery", bot.ai.get_or_create_session(key).chat_state)

    async def test_work_accepted_before_wait_still_runs(self):
        bot, key = self.fixture.bot()
        coordinator = bot._get_main_coordinator()
        ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
            message_id="before-wait", text="work", is_group=True)
        context, token, _ = coordinator.issue_chat_context(ctx, key)
        receipt = execute("submit", context, task_type="code", task="accepted work")
        execute("wait", context)
        coordinator.broker.seal_messages(token)
        entered = asyncio.Event()

        async def work(*_args):
            entered.set()

        with patch.object(coordinator, "_execute", work):
            await coordinator.poll()
            await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(coordinator.broker.record(receipt["id"])["state"], "queued")

    async def test_official_reset_cancels_unsent_reply_chunks(self):
        bot, key = self.fixture.bot(True)
        bot.app_config.bot.max_reply_length = 5
        bot.app_config.bot.enable_action_descriptions = True
        submitted = []

        async def send(text, **_options):
            submitted.append(text)
            bot.ai.reset_session(key)
            return {"id": "accepted"}

        ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
            message_id="official-chunks", text="reply", is_group=True, reply=send)
        coordinator = bot._get_main_coordinator()
        context, token, _ = coordinator.issue_chat_context(ctx, key)
        execute("reply", context, text="ABCDEFGHIJ")
        ctx.console_token, ctx.cli_messages = token, coordinator.broker.seal_messages(token)
        with self.assertRaisesRegex(MediaError, "会话已重置"):
            await bot._send_decision_messages(None, ctx, "Fixture")
        self.assertEqual(submitted, ["ABCDE"])

    async def test_policy_is_rechecked_before_work_is_queued(self):
        bot, key = self.fixture.bot()
        coordinator = bot._get_main_coordinator()
        ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
            message_id="policy-change", text="work", is_group=True)
        context, _, _ = coordinator.issue_chat_context(ctx, key)
        receipt = execute("submit", context, task_type="code", task="accepted work")
        bot.app_config.bot.work_permissions = WorkPermissions(access="disabled")
        with patch.object(coordinator, "_queue_result"), patch.object(bot._get_runtime(), "submit_work") as submit:
            await coordinator.poll()
        submit.assert_not_called()
        row = coordinator.broker.record(receipt["id"])
        self.assertEqual(row["state"], "ready")
        self.assertFalse(json.loads(row["result"])["success"])

    async def test_policy_change_while_queued_prevents_execution(self):
        bot, key = self.fixture.bot()
        coordinator, runtime = bot._get_main_coordinator(), bot._get_runtime()
        ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
            message_id="queued-policy-change", text="work", is_group=True)
        context, _, _ = coordinator.issue_chat_context(ctx, key)
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocking_job():
            entered.set()
            await release.wait()

        runtime.submit_work(EventContext(ctx.scope, "blocker", ctx.received_at, owner=ctx.owner),
                            "offline blocker", blocking_job)
        await asyncio.wait_for(entered.wait(), 2)
        receipt = execute("submit", context, task_type="code", task="queued work")
        with patch.object(coordinator, "_queue_result"), patch(
                "skills.task_skills.RunTaskSkill.execute", new=AsyncMock()) as run_task:
            try:
                await coordinator.poll()
                self.assertEqual(coordinator.broker.record(receipt["id"])["state"], "queued")
                bot.app_config.bot.work_permissions = WorkPermissions(access="disabled")
            finally:
                release.set()
            await asyncio.wait_for(runtime.work_worker, 3)
        run_task.assert_not_awaited()
        row = coordinator.broker.record(receipt["id"])
        self.assertEqual(row["state"], "ready")
        self.assertIn("无权发起", json.loads(row["result"])["error"])

    async def test_preview_matches_production_rules_and_custom_aliases(self):
        qt_app()
        from PySide6.QtWidgets import QWidget, QTabWidget
        from ui.components.persona_tab import PersonaTabWidget
        bot, key = self.fixture.bot()
        bot.app_config.bot.mode = "onebot"
        bot.app_config.bot.enable_action_descriptions = False
        bot.app_config.bot.reply_char_limit = 250
        name = "Audit custom persona"
        bot.personas.upsert_preset(PersonaPreset(name=name, prompt="你叫青岚。禁止动作描写。", aliases=["青岚"]))
        bot.personas.set_active(name)
        key = bot._chat_session_key("onebot_group_12345678")
        owner = QWidget()
        owner.app_config = bot.app_config
        tabs = QTabWidget(owner)
        tab = PersonaTabWidget(bot.personas, owner)
        tabs.addTab(tab, "Personas")
        try:
            self.assertEqual(tab._preview_prompt(), bot._get_main_coordinator().rules(
                key, bot._get_main_coordinator().system_for(key))[1])
            self.assertIn("青岚", bot._bot_names())
            self.assertNotIn("ふぶき", bot._bot_names())
            self.assertNotIn("ヨンミン", bot._bot_names())
        finally:
            owner.close()
            owner.deleteLater()
            QT_APP.processEvents()


class PersonaAuditTests(unittest.TestCase):
    def test_disabling_actions_preserves_identity_and_normal_instructions(self):
        for compact in (False, True):
            for base in ("你叫青岚，是数学老师。禁止动作描写，列出计算过程。",
                         "你叫青岚。\n允许动作神态描写。\n解释时列出依据。",
                         "你叫青岚。不要穿插括号，正常回答问题。"):
                with self.subTest(compact=compact, base=base):
                    prompt = compose_system_prompt(base, enable_action_descriptions=False, compact=compact)
                    self.assertIn(base, prompt)
                    self.assertIn("纯对话台词模式", prompt)

    def test_reparented_tab_rename_preserves_group_binding_and_identity(self):
        qt_app()
        from PySide6.QtWidgets import QWidget, QTabWidget
        from ui.components.persona_tab import PersonaTabWidget
        from ui.components.config_tab import ConfigTabWidget
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            personas = PersonaManager(str(root / "personas.yaml"))
            old_name = list(personas.presets)[1]
            expected_id = personas.get_preset(old_name).id
            owner = QWidget()
            owner.app_config = AppConfig()
            owner.config_path = str(root / "config.yaml")
            owner.app_config.bot.group_settings["123"] = GroupCustomConfig(group_id="123", persona=old_name)
            owner.app_config.save(owner.config_path)
            config_tab = ConfigTabWidget(owner.app_config, parent=owner)
            config_tab.load_group_settings()
            tabs, tab = QTabWidget(owner), PersonaTabWidget(personas, owner)
            tab.persona_renamed_signal.connect(config_tab.rename_persona_references)
            tabs.addTab(tab, "Personas")
            try:
                for index in range(tab.persona_list.count()):
                    if tab.persona_list.item(index).data(256) == old_name:
                        tab.persona_list.setCurrentRow(index)
                        break
                tab.edit_persona_name.setText("Renamed fixture")
                with patch("ui.components.persona_tab.QMessageBox.information"), patch(
                        "ui.components.persona_tab.QMessageBox.warning") as warning:
                    tab._on_save_persona()
                warning.assert_not_called()
                restored = AppConfig.load(owner.config_path)
                resolved = effective_persona(SimpleNamespace(app_config=restored, personas=personas), "123")
                self.assertEqual(restored.bot.group_settings["123"].persona, "Renamed fixture")
                self.assertEqual(resolved.id, expected_id)
                self.assertEqual(resolved.name, "Renamed fixture")
                config_tab.save_group_settings()
                self.assertEqual(owner.app_config.bot.group_settings["123"].persona, "Renamed fixture")
            finally:
                owner.close()
                owner.deleteLater()
                QT_APP.processEvents()

    def test_group_table_preserves_unedited_work_permissions_and_admins(self):
        qt_app()
        from ui.components.config_tab import ConfigTabWidget
        config = AppConfig()
        config.bot.group_settings["123"] = GroupCustomConfig(group_id="123", admin_ids=["admin-id"],
            work_permissions=WorkPermissions(access="master", task_access={"code": "disabled"}))
        tab = ConfigTabWidget(config)
        try:
            tab.load_group_settings()
            tab.table_groups.item(0, 0).setText("456")
            tab.save_group_settings()
            self.assertEqual(config.bot.group_settings["456"].work_permissions.access, "master")
            self.assertEqual(config.bot.group_settings["456"].work_permissions.task_access, {"code": "disabled"})
            self.assertEqual(config.bot.group_settings["456"].admin_ids, ["admin-id"])
        finally:
            tab.close()
            tab.deleteLater()
            QT_APP.processEvents()


class LoopAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_call_ids_and_timestamps_do_not_keep_same_failure_running(self):
        for nested in (False, True):
            with self.subTest(nested=nested), tempfile.TemporaryDirectory() as directory:
                calls = 0

                async def ask(_message, trace, _timeout):
                    nonlocal calls
                    calls += 1
                    if calls > 3:
                        raise AssertionError("non-progressing loop was not stopped")
                    function = {"name": "read_file", "arguments": {"path": "missing.txt"}}
                    call = {"id": "call-" + str(calls), "timestamp": calls,
                            **({"function": function} if nested else function)}
                    trace.extend([{"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [call]},
                                  {"type": "TOOL_RESULT", "status": "ERROR", "content": "File does not exist"}])
                    return '{"status":"in_progress","answer":"Still working"}'

                with self.assertRaisesRegex(MediaError, "重复返回"):
                    await run_agent_work(ask, "offline task", directory, [], lambda data, trace: data)
                self.assertEqual(calls, 2)

    async def test_semantic_ids_in_tool_arguments_are_not_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = 0

            async def ask(_message, trace, _timeout):
                nonlocal calls
                calls += 1
                trace.append({"type": "PLANNER_RESPONSE", "tool_calls": [{"id": "new-" + str(calls),
                    "name": "fetch_record", "arguments": json.dumps({"id": calls, "timestamp": calls})}]})
                return '{"status":"in_progress"}' if calls < 3 else '{"status":"completed","answer":"done"}'

            _, answer = await run_agent_work(ask, "offline task", directory, [], lambda data, trace: data["answer"])
            self.assertEqual(answer, "done")
            self.assertEqual(calls, 3)


class ConsoleAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.payload = {"namespace": "audit", "scope": "group", "sender_id": "456", "is_group": True,
                        "is_master": False, "is_admin": False}

    def context(self, **values):
        context = self.broker.issue_context({**self.payload, **values}, self.root / "contexts")
        return context, ConsoleBroker.from_context(context)[1]

    def test_closed_turn_rejects_new_work_but_keeps_receipts_and_queries(self):
        for mode in ("wait", "sealed", "reply_finished"):
            with self.subTest(mode=mode):
                context, token = self.context()
                receipt = execute("submit", context, task_type="code", task="existing")
                file_id = self.broker.record_files("audit", "group", "upload", "456",
                    [{"name": "input.txt", "native_id": "offline-file"}])[0]
                received = execute("receive", context, file_id=file_id)
                if mode == "wait":
                    execute("wait", context)
                elif mode == "sealed":
                    self.broker.seal_messages(token, cancel=True)
                else:
                    execute("reply", context, text="acknowledged")
                    self.assertEqual(execute("submit", context, task_type="code", task="after reply")["state"], "submitted")
                    # Reply submission no longer ends work. Natural completion
                    # seals the turn before the closed-context checks below.
                    self.broker.seal_messages(token)
                for action, params in (("submit", {"task_type": "code", "task": "new task"}),
                                       ("search", {"query": "new search"}),
                                       ("generate-image", {"prompt": "a new image"}),
                                       ("receive", {"file_id": file_id, "request_key": "late-receive"}),
                                       ("command", {"text": "/code new code"})):
                    with self.subTest(action=action), self.assertRaisesRegex(ValueError, "本轮已经结束"):
                        execute(action, context, **params)
                self.assertEqual(execute("submit", context, task_type="code", task="existing")["id"], receipt["id"])
                self.assertEqual(execute("receive", context, file_id=file_id)["id"], received["id"])
                self.assertEqual(execute("status", context, request_id=receipt["id"])["state"], "submitted")
                execute("cancel", context, request_id=receipt["id"])

    def test_closing_turn_during_input_resolution_prevents_task_creation(self):
        context, token = self.context()

        def inputs(*_args):
            self.broker.wait_for_message(token)
            return {}

        with patch.object(self.broker, "task_inputs", side_effect=inputs), self.assertRaisesRegex(ValueError, "本轮已经结束"):
            self.broker.submit(token, "code", "late work")
        self.assertEqual(self.broker.list_requests(token), [])

    def test_public_work_remains_default_and_optional_policies_cover_both_entrypoints(self):
        for access, allowed in (("all", True), ("admin", False), ("master", False),
                                ("allowlist", False), ("disabled", False)):
            with self.subTest(access=access):
                context, _ = self.context(work_permissions={"access": access})
                for action, params in (("command", {"text": "/code harmless report"}),
                                       ("command", {"text": "/next"}),
                                       ("submit", {"task_type": "command", "task": "harmless report"})):
                    if allowed:
                        self.assertEqual(execute(action, context, **params)["state"], "submitted")
                    else:
                        with self.assertRaises(PermissionError):
                            execute(action, context, **params)
        context, _ = self.context()
        self.assertEqual(execute("command", context, text="/执行 harmless report")["state"], "submitted")
        for role in ("is_master", "is_admin"):
            context, _ = self.context(**{role: True}, work_permissions={"access": "admin"})
            self.assertEqual(execute("submit", context, task_type="code", task=role)["state"], "submitted")
        context, _ = self.context(work_permissions={"access": "allowlist", "allowed_user_ids": ["456"],
            "task_access": {"code": "disabled"}})
        self.assertEqual(execute("submit", context, task_type="search", task="allowed")["state"], "submitted")
        with self.assertRaises(PermissionError):
            execute("submit", context, task_type="code", task="blocked")
        context, _ = self.context(work_permissions={"task_access": {"search": "disabled"}})
        with self.assertRaises(PermissionError):
            execute("command", context, text="/next")
        config = AppConfig()
        config.bot.work_permissions = WorkPermissions(access="master")
        config.bot.group_settings["123"] = GroupCustomConfig(group_id="123", work_permissions=WorkPermissions(access="all"))
        self.assertEqual(config.bot.get_work_permissions("123").access, "all")
        self.assertEqual(config.bot.get_work_permissions("other").access, "master")
        with self.assertRaises(ValueError):
            WorkPermissions(task_access={"misspelled_code": "master"})
