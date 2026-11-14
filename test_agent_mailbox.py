"""Offline public-console checks for durable agent identities and isolated mail."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_mailbox import AgentMailbox
from autonomy_requests import direct_self_request
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute, main
from console_tools.contracts import capabilities
from console_tools.server import handle


class AgentMailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qq agents 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.mailbox = AgentMailbox(self.broker)
        self.metadata = {"namespace": "account-a", "scope": "group:123", "session_key": "persona-a", "generation": 3,
            "main_agent": True, "is_master": True, "sender_id": "owner", "source_message_id": "human-msg",
            "work_permissions": {"access": "all"}, "self_activity_config": {"enabled": True},
            "output_dir": str(self.root), "artifact_roots": [str(self.root)]}
        self.context = self.issue()

    def issue(self, **changes):
        return self.broker.issue_context({**self.metadata, **changes}, self.root / "contexts")

    def call(self, operation, context=None, **params):
        return execute("agents", context or self.context, operation=operation, params=params)

    def create(self, **params):
        return self.call("create", name="资料员", text="具体任务", **params)

    def worker(self, identity, message):
        workspace = self.mailbox.workspace(self.mailbox.agent(identity))
        workspace.mkdir(parents=True, exist_ok=True)
        context = self.issue(main_agent=False, is_master=False, agent_worker_id=identity,
            agent_input_id=message["seq"], artifact_roots=[str(workspace)], output_dir=str(workspace))
        return context, workspace

    def test_cli_mcp_and_repeated_create_share_identity_and_a_durable_ordered_queue(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["agents", "create", "--context-file", self.context, "--name", "资料员", "--text", "具体任务"])
        first = json.loads(output.getvalue())
        again = self.create()
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(first["message_id"], again["message_id"])
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "qqbot_cli", "arguments": {"action": "agents", "operation": "send", "context_file": self.context,
                "params": {"agent_id": first["id"], "text": "后续\n消息"}}}})
        self.assertFalse(response["result"]["isError"])
        sent = json.loads(response["result"]["content"][0]["text"])
        current = self.mailbox.claim(first["id"])
        self.assertIsNone(self.mailbox.claim(first["id"]))
        self.assertEqual(current["text"], "具体任务")
        self.mailbox.complete(self.mailbox.agent(first["id"]), current, "completed", "首条结果")
        restored = AgentMailbox(ConsoleBroker(self.broker.path))
        following = restored.claim(first["id"])
        self.assertEqual(following["seq"], sent["message_id"])
        self.assertEqual(following["text"], "后续\n消息")
        self.assertEqual(self.call("list")["agents"][0]["id"], first["id"])
        with self.assertRaises(ValueError):
            self.call("create", name="资料员", text="其他任务", instructions="不同职责")

    def test_worker_can_report_validated_files_but_cannot_send_chat_spawn_or_read_other_agent(self):
        first, second = self.create(), self.call("create", name="其他", text="其他任务")
        message = self.mailbox.claim(first["id"])
        worker, workspace = self.worker(first["id"], message)
        for action, kwargs in (("reply", {"text": "不能发送"}), ("submit", {"task": "不能提交"}),
                ("command", {"text": "/status"}), ("companion", {"operation": "wish", "params": {"text": "无权写入"}}),
                ("agents", {"operation": "create", "params": {"name": "树", "text": "不能派生"}}),
                ("agents", {"operation": "read", "params": {"agent_id": second["id"]}})):
            with self.subTest(action=action), self.assertRaises(PermissionError):
                execute(action, worker, **kwargs)
        context = execute("context", worker)
        self.assertEqual(context["role"], "agent_worker")
        self.assertEqual(context["capabilities"]["agents"], ["read", "report"])
        self.assertFalse(context["messages"])
        outside = self.root / "private.txt"
        outside.write_text("private", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.call("report", worker, text="不允许外部文件", files=[str(outside)])
        result = workspace / "report.txt"
        result.write_text("真实成果", encoding="utf-8")
        receipt = self.call("report", worker, text="真实结果", files=[str(result)], finish=True)
        self.assertEqual(receipt["next_action"], "done")
        self.assertEqual(receipt["seq"], self.call("report", worker, text="真实结果", files=[str(result)], finish=True)["seq"])
        self.assertFalse(self.mailbox.pending("account-a", "persona-a", 3))
        self.mailbox.complete(self.mailbox.agent(first["id"]), message, "completed", "", payload={"tools": ["search_web"]})
        mail = self.mailbox.pending("account-a", "persona-a", 3)
        self.assertEqual(len(mail), 1)
        payload = json.loads(mail[0]["payload"])
        self.assertTrue(payload["success"])
        self.assertEqual(payload["files"][0]["path"], str(result))
        self.assertEqual(self.call("read", agent_id=first["id"])["messages"][-1]["text"], "真实结果")

    def test_scope_generation_and_role_boundaries_are_enforced(self):
        first = self.create()
        for changed in ({"namespace": "account-b"}, {"scope": "group:other"}, {"session_key": "persona-b"}, {"generation": 4}):
            context = self.issue(**changed)
            self.assertFalse(self.call("list", context)["agents"])
            for op, params in (("read", {}), ("send", {"text": "不允许"}), ("close", {})):
                with self.subTest(changed=changed, op=op), self.assertRaises(PermissionError):
                    self.call(op, context, agent_id=first["id"], **params)
        for changed in ({"main_agent": False}, {"parent_request_id": "task"}, {"sandbox_session_id": "game"},
                        {"activity_controller": True}, {"idle_event": True}, {"proactive_event": True}):
            with self.subTest(changed=changed), self.assertRaises(PermissionError):
                self.call("create", self.issue(**changed), name="不能申请", text="无权")
        with self.assertRaises(PermissionError):
            self.call("report", text="主代理不能冒充子代理")

    def test_tasks_cancel_only_the_callers_mail_in_a_shared_agent(self):
        first = self.create()
        guest = self.issue(sender_id="guest", is_master=False)
        second = self.call("send", guest, agent_id=first["id"], text="另一个成员的任务")
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(self.create()["task_id"], first["task_id"])
        self.assertEqual([row["id"] for row in execute("tasks", self.context)["tasks"]], [first["task_id"]])
        self.assertEqual([row["id"] for row in execute("tasks", guest)["tasks"]], [second["task_id"]])
        self.assertFalse(execute("cancel", guest, request_id=first["task_id"])["ok"])
        self.assertEqual(execute("cancel", self.context, request_id=first["task_id"])["state"], "cancelled")
        self.assertEqual(execute("status", self.context, request_id=first["task_id"])["state"], "cancelled")
        self.assertEqual(self.mailbox.claim(first["id"])["seq"], second["message_id"])
        self.assertNotIn(self.mailbox.agent(first["id"])["state"], ("closing", "closed"))

    def test_cancel_without_id_reaches_all_pages_of_owned_mail(self):
        from types import SimpleNamespace
        from config import AppConfig
        from console_tools.chat_commands import execute_control
        for number in range(103):
            self.call("create", name="资料员", text="任务 " + str(number))
        request = execute("command", self.context, text="/cancel")
        token = ConsoleBroker.from_context(self.context)[1]
        coordinator = SimpleNamespace(broker=self.broker, client=SimpleNamespace(app_config=AppConfig()))
        execute_control(coordinator, self.broker.record(request["id"]), None, self.broker.context(token))
        with self.broker.connect() as db:
            states = db.execute("SELECT state,COUNT(*) FROM agent_mail WHERE direction='to_agent' GROUP BY state").fetchall()
        self.assertEqual([tuple(row) for row in states], [("cancelled", 103)])

    def test_old_mail_schema_migration_preserves_rows_and_stable_task_ids(self):
        import sqlite3
        from contextlib import closing
        path = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE agent_mail(seq INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT, "
                "direction TEXT,text TEXT,payload TEXT,state TEXT,token TEXT,request_key TEXT,reply_to INTEGER,"
                "seen INTEGER DEFAULT 0,created REAL,UNIQUE(token,direction,request_key))")
            db.execute("INSERT INTO agent_mail(agent_id,direction,text,payload,state,token,request_key,created) "
                "VALUES(?,?,?,?,?,?,?,?)", ("old-agent", "to_agent", "历史原文", '{"files":[]}', "queued", "old-token", "old-key", 1))
            db.commit()
        migrated = ConsoleBroker(path)
        with migrated.connect() as db:
            first = dict(db.execute("SELECT * FROM agent_mail").fetchone())
        self.assertEqual(first["text"], "历史原文")
        self.assertEqual(first["state"], "queued")
        self.assertRegex(first["task_id"], r"^[a-f0-9]{32}$")
        restored = ConsoleBroker(path)
        with restored.connect() as db:
            self.assertEqual(dict(db.execute("SELECT * FROM agent_mail").fetchone()), first)

    def test_agent_task_ids_obey_account_persona_generation_binding_and_autonomous_owner_policy(self):
        first = self.create()
        for change in ({"namespace": "account-b"}, {"scope": "group:other"}, {"session_key": "persona-b"}, {"generation": 4}):
            context = self.issue(**change)
            self.assertFalse(execute("tasks", context)["tasks"])
            self.assertFalse(execute("status", context, request_id=first["task_id"])["ok"])
            self.assertFalse(execute("cancel", context, request_id=first["task_id"])["ok"])
        idle = self.issue(self_activity=True, idle_event=True, is_master=False, sender_id="")
        public = self.call("create", idle, name="自主阅读", text="阅读公开资料", task_type="search")
        ordinary = self.issue(is_master=False)
        self.assertNotIn(public["task_id"], [row["id"] for row in execute("tasks", ordinary)["tasks"]])
        self.assertFalse(execute("cancel", ordinary, request_id=public["task_id"])["ok"])
        self.assertIn(public["task_id"], [row["id"] for row in execute("tasks", self.context)["tasks"]])
        self.assertEqual(execute("cancel", self.context, request_id=public["task_id"])["state"], "cancelled")
        restored = ConsoleBroker(self.broker.path)
        self.assertEqual(AgentMailbox(restored).list_tasks(ConsoleBroker.from_context(self.context)[1])[1]["id"], first["task_id"])

    def test_admission_uses_work_permissions_and_autonomy_has_no_owner_identity(self):
        for context in (self.issue(work_permissions={"access": "disabled"}),
                        self.issue(is_master=False, work_permissions={"access": "master"})):
            with self.assertRaises(PermissionError):
                self.call("create", context, name="无权", text="无权")
        own = self.create()
        idle = self.issue(self_activity=True, idle_event=True, is_master=False, sender_id="")
        with self.assertRaises(PermissionError):
            self.call("create", idle, name="编程", text="不允许", task_type="code")
        public = self.call("create", idle, name="公开阅读", text="学习公开内容", task_type="search")
        self.assertTrue(self.mailbox.agent(public["id"])["autonomous"])
        with self.assertRaises(PermissionError):
            self.call("send", idle, agent_id=own["id"], text="借用主人任务")
        with self.assertRaises(PermissionError):
            self.call("close", idle, agent_id=own["id"])
        with self.assertRaises(PermissionError):
            self.call("create", self.issue(self_activity=True, is_master=True, work_permissions={"access": "master"}),
                      name="不能冒充主人", task_type="search", text="公开阅读")
        self.assertNotIn("reply", capabilities(self.broker.context(ConsoleBroker.from_context(idle)[1])))

    def test_reply_leaves_mail_open_until_natural_completion_then_seal_revokes_new_work(self):
        first = self.create()
        execute("reply", self.context, text="稍后看看")
        again = self.create()
        self.assertEqual(first["message_id"], again["message_id"])
        followup = self.call("send", agent_id=first["id"], text="回复之后继续安排任务")
        second = self.call("create", name="另一个子代理", text="回复之后的新任务")
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["message_id"], followup["message_id"])
        self.broker.seal_messages(ConsoleBroker.from_context(self.context)[1])
        with self.assertRaises(ValueError):
            self.call("send", agent_id=first["id"], text="不能追加")
        with self.assertRaises(ValueError):
            self.call("create", name="已结束不能创建", text="新任务")
        self.assertEqual(self.mailbox.claim(first["id"])["text"], "具体任务")

    def test_restart_interrupts_running_mail_without_replaying_and_keeps_unstarted_mail(self):
        first = self.create()
        self.call("send", agent_id=first["id"], text="还未开始")
        running = self.mailbox.claim(first["id"])
        restored = AgentMailbox(ConsoleBroker(self.broker.path))
        restored.recover("account-a")
        restored.recover("account-a")
        self.assertEqual(restored.claim(first["id"])["text"], "还未开始")
        messages = self.call("read", agent_id=first["id"])["messages"]
        self.assertEqual(messages[0]["state"], "interrupted")
        notifications = restored.pending("account-a", "persona-a", 3)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["reply_to"], running["seq"])
        cancelled = self.call("create", name="重启前取消", text="取消必须在重启后保留")
        active = self.mailbox.claim(cancelled["id"])
        execute("cancel", self.context, request_id=cancelled["task_id"])
        recovered = restored.recover("account-a")
        self.assertIn(active["seq"], [row["seq"] for row in recovered])
        self.assertEqual(execute("status", self.context, request_id=cancelled["task_id"])["state"], "cancelled")
        self.assertIsNone(restored.claim(cancelled["id"]))

    def test_large_mail_is_paginated_and_stays_unread_until_main_success(self):
        first = self.create()
        message = self.mailbox.claim(first["id"])
        body = "长内容" * 6000
        self.mailbox.complete(self.mailbox.agent(first["id"]), message, "completed", body)
        notification = self.mailbox.pending("account-a", "persona-a", 3)[0]
        page = self.call("read", message_id=notification["seq"], max_chars=1000)["messages"][0]
        self.assertTrue(page["truncated"])
        rest = self.call("read", message_id=notification["seq"], offset=page["next_offset"], max_chars=1000)["messages"][0]
        self.assertEqual(page["text"] + rest["text"], body[:2000])
        self.assertTrue(self.mailbox.pending("account-a", "persona-a", 3))
        self.mailbox.acknowledge([notification])
        self.assertFalse(self.mailbox.pending("account-a", "persona-a", 3))

    def test_interrupted_review_relay_is_not_executed_twice_with_a_fresh_context(self):
        a = self.create()
        message = self.mailbox.claim(a["id"])
        self.mailbox.complete(self.mailbox.agent(a["id"]), message, "completed", "A 的资料")
        notice = self.mailbox.pending("account-a", "persona-a", 3)[0]
        first = self.issue(agent_event=True, idle_event=True, agent_notice_ids=[notice["seq"]])
        b = self.call("create", first, name="B", text="分析 A 的资料")
        self.broker.seal_messages(ConsoleBroker.from_context(first)[1])
        # The interrupted result has not been acknowledged; the next real
        # chat can receive it with a new token and make the same relay call.
        second = self.issue(agent_notice_ids=[notice["seq"]])
        repeated = self.call("create", second, name="B", text="分析 A 的资料")
        self.assertEqual(repeated["message_id"], b["message_id"])
        active = self.mailbox.claim(b["id"])
        self.mailbox.complete(self.mailbox.agent(b["id"]), active, "completed", "B 的结果")
        self.assertIsNone(self.mailbox.claim(b["id"]))

    def test_same_task_and_request_key_can_be_sent_to_different_children(self):
        a = self.call("create", name="A", text="独立核对同一问题", request_key="parallel-review")
        b = self.call("create", name="B", text="独立核对同一问题", request_key="parallel-review")
        self.assertNotEqual(a["id"], b["id"])
        self.assertNotEqual(a["message_id"], b["message_id"])
        self.assertEqual(self.mailbox.claim(a["id"])["text"], self.mailbox.claim(b["id"])["text"])

    def test_long_running_worker_can_report_after_a_day_but_cannot_reuse_a_finished_turn(self):
        agent = self.create()
        message = self.mailbox.claim(agent["id"])
        worker, _ = self.worker(agent["id"], message)
        future = time.time() + 90000
        with patch("time.time", return_value=future):
            self.call("report", worker, text="长任务真实结果", finish=True)
            with self.assertRaises(ValueError):
                self.call("report", worker, text="本轮结束后不能追加")

    def test_direct_invitation_requires_owner_and_does_not_execute_quotes_or_questions(self):
        self.assertTrue(direct_self_request("你自己玩去吧，学学人类的说话方式和行为", is_master=True))
        self.assertTrue(direct_self_request("有空自己去看看感兴趣的内容", is_master=True, directed=True))
        for text, owner in (("你自己玩去吧", False), ('他说“你自己玩去吧”', True),
                            ("不要自己去浏览", True), ("你自己玩过吗？", True), ("我自己去看看", True)):
            self.assertFalse(direct_self_request(text, is_master=owner, directed=True))
