"""Console-owned, durable identities and mail for reusable work agents."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import uuid

from console_tools.files import outgoing_files
from console_tools.work_permissions import authorize_work

AGENT_TYPES = ("task", "search", "code", "process")
PARENT_OPERATIONS = ("create", "send", "list", "read", "close")


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY, namespace TEXT, parent_key TEXT,
            generation INTEGER, scope TEXT, name TEXT, instructions TEXT, task_type TEXT,
            state TEXT, autonomous INTEGER, created REAL, updated REAL,
            UNIQUE(namespace,parent_key,generation,name));
        CREATE INDEX IF NOT EXISTS agents_parent ON agents(namespace,parent_key,generation,state);
        CREATE TABLE IF NOT EXISTS agent_mail(seq INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT,
            direction TEXT, text TEXT, payload TEXT, state TEXT, token TEXT, request_key TEXT,
            reply_to INTEGER, seen INTEGER DEFAULT 0, created REAL,
            UNIQUE(token,direction,request_key));
        CREATE INDEX IF NOT EXISTS agent_mail_queue ON agent_mail(agent_id,direction,state,seq);
        CREATE INDEX IF NOT EXISTS agent_mail_dedup ON agent_mail(agent_id,direction,request_key);
    """)
    # Give each instruction its own public task ID. The agent remains shared
    # by its parent conversation; cancelling one member's mail must not close
    # another member's queued work or the reusable agent identity.
    db.execute("BEGIN IMMEDIATE")
    if "task_id" not in {row[1] for row in db.execute("PRAGMA table_info(agent_mail)")}:
        db.execute("ALTER TABLE agent_mail ADD COLUMN task_id TEXT")
    for row in db.execute("SELECT seq FROM agent_mail WHERE direction='to_agent' AND task_id IS NULL").fetchall():
        db.execute("UPDATE agent_mail SET task_id=? WHERE seq=?", (uuid.uuid4().hex, row[0]))
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS agent_mail_task ON agent_mail(task_id)")


class AgentMailbox:
    def __init__(self, broker):
        self.broker = broker

    @staticmethod
    def binding(context):
        return context["namespace"], context["session_key"], context.get("generation", 0), context["scope"]

    def workspace(self, agent):
        parent = hashlib.sha256((agent["namespace"] + ":" + agent["parent_key"]).encode()).hexdigest()[:24]
        return self.broker.path.parent / parent / "agents" / agent["id"]

    def agent(self, identity):
        with self.broker.connect() as db:
            row = db.execute("SELECT * FROM agents WHERE id=?", (identity,)).fetchone()
        if not row:
            raise ValueError("子代理不存在；用 agents list 查询真实 ID。")
        return dict(row)

    def _bound(self, context, identity):
        agent = self.agent(identity)
        if self.binding(context) != (agent["namespace"], agent["parent_key"], agent["generation"], agent["scope"]):
            raise PermissionError("子代理不属于当前账号、人设、会话或会话版本。")
        if context.get("agent_worker_id") and context["agent_worker_id"] != identity:
            raise PermissionError("执行分支只能读取自己的信箱。")
        return agent

    @staticmethod
    def _admit(context, kind, autonomous):
        if kind not in AGENT_TYPES:
            raise ValueError("子代理类型只支持 task/search/code/process。")
        if autonomous:
            if not (context.get("self_activity_config") or {}).get("enabled"):
                raise PermissionError("独立活动已关闭；不能开始或继续自主任务。")
            if kind != "search":
                raise PermissionError("自主子代理只研究公开内容；游戏、登录或新能力请向主人提出。")
            # Autonomy is a separate public-reading grant, never the owner's
            # master/admin authority or an allowlisted human identity.
            authorize_work(kind, {**context, "is_master": False, "is_admin": False, "sender_id": ""})
        else:
            authorize_work(kind, context)

    @staticmethod
    def _public(agent):
        return {key: agent[key] for key in ("id", "name", "task_type", "state", "autonomous", "updated")}

    def operate(self, token, operation, *, agent_id="", name="", instructions="", task_type="task", text="",
                request_key="", files=None, after=0, limit=20, message_id=0, offset=0, max_chars=4000,
                finish=False):
        context = self.broker.context(token)
        worker = context.get("agent_worker_id")
        if worker:
            if operation not in ("read", "report"):
                raise PermissionError("子代理只能读取自己的信箱并向主代理报告。")
            agent_id = agent_id or worker
        elif (not context.get("main_agent") or operation not in PARENT_OPERATIONS
              or context.get("sandbox_session_id") or context.get("parent_request_id")
              or context.get("activity_controller") or context.get("proactive_event")
              or context.get("idle_event") and not (context.get("self_activity") or context.get("agent_event"))):
            raise PermissionError("只有当前主代理能申请、联系和关闭子代理。")
        if operation == "list":
            with self.broker.connect() as db:
                rows = db.execute("SELECT * FROM agents WHERE namespace=? AND parent_key=? AND generation=? "
                    "AND scope=? ORDER BY updated DESC,id LIMIT ?", (*self.binding(context), limit)).fetchall()
            return {"agents": [self._public(row) for row in rows]}
        if operation == "read":
            if agent_id:
                self._bound(context, agent_id)
            with self.broker.connect() as db:
                rows = db.execute("SELECT m.*,a.name FROM agent_mail m JOIN agents a ON a.id=m.agent_id "
                    "WHERE a.namespace=? AND a.parent_key=? AND a.generation=? AND a.scope=? "
                    "AND (?='' OR a.id=?) AND m.seq>? AND (?=0 OR m.seq=?) "
                    "AND m.state!='staged' ORDER BY m.seq LIMIT ?",
                    (*self.binding(context), agent_id, agent_id, after, message_id, message_id, limit)).fetchall()
            result = []
            for row in rows:
                item = {key: row[key] for key in ("seq", "agent_id", "name", "direction", "state", "reply_to", "created")}
                end = offset + max_chars
                result.append({**item, **json.loads(row["payload"]), "text": row["text"][offset:end],
                    "truncated": end < len(row["text"]), "next_offset": end if end < len(row["text"]) else None})
            return {"messages": result, "next_after": rows[-1]["seq"] if rows else after}
        if operation == "report":
            agent = self._bound(context, agent_id)
            attachments = outgoing_files(context, files) if files else []
            with self.broker.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                # Deduplicate a retried final report even after it ended the turn.
                key = request_key or hashlib.sha256(json.dumps([text, attachments, finish], sort_keys=True).encode()).hexdigest()
                old = db.execute("SELECT seq,state FROM agent_mail WHERE token=? AND direction='to_main' AND request_key=?",
                                 (token, key)).fetchone()
                if old:
                    return dict(old)
                self.broker._require_open_turn(db, token)
                active = db.execute("SELECT * FROM agent_mail WHERE seq=? AND agent_id=? AND state='running'",
                                    (context["agent_input_id"], agent_id)).fetchone()
                if agent["state"] in ("closing", "closed") or not active:
                    raise PermissionError("本次子代理任务已结束或已撤销。")
                seq = db.execute("INSERT INTO agent_mail(agent_id,direction,text,payload,state,token,request_key,reply_to,created) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", (agent_id, "to_main", text,
                    json.dumps({"files": attachments, "kind": "result" if finish else "progress"}, ensure_ascii=False),
                    "staged" if finish else "ready", token, key, context["agent_input_id"], time.time())).lastrowid
                if finish:
                    db.execute("INSERT INTO reply_controls VALUES(?,?,?)", (token, "reported", "子代理报告已提交"))
            return {"seq": seq, "state": "submitted", "next_action": "done" if finish else "continue"}
        if operation == "create":
            name = name.strip()
            if not name or not text.strip():
                raise ValueError("申请子代理需要名称和首条具体任务。")
            autonomous = bool(context.get("self_activity") or context.get("autonomous_agents"))
            self._admit(context, task_type, autonomous)
            attachments = outgoing_files(context, files) if files else []
            with self.broker.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT * FROM agents WHERE namespace=? AND parent_key=? AND generation=? AND scope=? AND name=?",
                                 (*self.binding(context), name)).fetchone()
                if old:
                    agent = dict(old)
                    if (agent["instructions"] != instructions or agent["task_type"] != task_type
                            or bool(agent["autonomous"]) != autonomous):
                        raise ValueError("该名称已绑定其他职责；请用 agents send 继续原任务，或使用另一个名称。")
                else:
                    self.broker._require_active_turn(db, token)
                    identity, now = uuid.uuid4().hex, time.time()
                    db.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (identity,
                        *self.binding(context)[:3], context["scope"], name, instructions, task_type, "idle", int(autonomous), now, now))
                    agent = dict(db.execute("SELECT * FROM agents WHERE id=?", (identity,)).fetchone())
                return self._send(db, token, agent, text, attachments, request_key)
        agent = self._bound(context, agent_id)
        if operation == "send":
            autonomous = bool(agent["autonomous"] or context.get("self_activity") or context.get("autonomous_agents"))
            if autonomous != bool(agent["autonomous"]):
                raise PermissionError("独处时只能联系自主阅读子代理，不能借用其他工作分支的权限。")
            self._admit(context, agent["task_type"], autonomous)
            attachments = outgoing_files(context, files) if files else []
            with self.broker.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                return self._send(db, token, agent, text, attachments, request_key)
        if operation == "close":
            if (context.get("self_activity") or context.get("autonomous_agents")) and not agent["autonomous"]:
                raise PermissionError("独处时不能结束由用户安排的工作子代理。")
            with self.broker.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self.broker._require_active_turn(db, token)
                db.execute("UPDATE agents SET state='closing',updated=? WHERE id=? AND state!='closed'", (time.time(), agent_id))
                db.execute("UPDATE agent_mail SET state='cancelled' WHERE agent_id=? AND direction='to_agent' AND state='queued'", (agent_id,))
            return {"id": agent_id, "state": "closed" if agent["state"] == "closed" else "closing"}
        raise ValueError("未知子代理操作。")

    def _send(self, db, token, agent, text, files, request_key):
        if not text.strip():
            raise ValueError("发给子代理的消息不能为空。")
        context = self.broker.context(token)
        notices = context.get("agent_notice_ids") or []
        # Retrying an interrupted main review must not execute the same relay
        # twice, even when the next review/real chat has a fresh context token.
        origin = "notice:" + str(min(notices)) if notices else "turn:" + hashlib.sha256(token.encode()).hexdigest()
        key = agent["id"] + ":" + origin + ":" + (request_key or hashlib.sha256(json.dumps([text, files], sort_keys=True).encode()).hexdigest())
        old = db.execute("SELECT seq,state,task_id FROM agent_mail WHERE agent_id=? AND direction='to_agent' AND request_key=?", (agent["id"], key)).fetchone()
        if old:
            return {"id": agent["id"], "name": agent["name"], "message_id": old["seq"], "task_id": old["task_id"], "state": old["state"]}
        self.broker._require_active_turn(db, token)
        state = db.execute("SELECT state FROM agents WHERE id=?", (agent["id"],)).fetchone()[0]
        if state in ("closing", "closed"):
            raise PermissionError("该子代理已关闭；记录仍可读取，新任务请申请另一个名称。")
        task_id = uuid.uuid4().hex
        seq = db.execute("INSERT INTO agent_mail(agent_id,direction,text,payload,state,token,request_key,created,task_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (agent["id"], "to_agent", text, json.dumps({"files": files}, ensure_ascii=False),
            "queued", token, key, time.time(), task_id)).lastrowid
        db.execute("UPDATE agents SET state=?,updated=? WHERE id=?", ("running" if state == "running" else "queued", time.time(), agent["id"]))
        return {"id": agent["id"], "name": agent["name"], "message_id": seq, "task_id": task_id, "state": "queued", "next_action": "continue"}

    @staticmethod
    def _task_allowed(context, row):
        if not context.get("session_key"):
            return False
        if any(context.get(key) for key in ("agent_worker_id", "parent_request_id", "sandbox_session_id", "activity_controller")):
            return False
        if AgentMailbox.binding(context) != (row["namespace"], row["parent_key"], row["generation"], row["scope"]):
            return False
        if row["autonomous"]:
            return bool(context.get("is_master"))
        source = json.loads(row["source_context"])
        def sender(data):
            return str(data.get("sender_id") or data.get("user_id") or data.get("owner") or "").strip()
        return bool(sender(context)) and sender(context) == sender(source)

    @staticmethod
    def _task_view(row):
        return {"id": row["task_id"], "kind": "agent", "task": row["text"][:500], "state": row["state"],
            "created": row["created"], "result": {"agent_id": row["agent_id"], "message_id": row["seq"],
                "name": row["name"], "task_type": row["task_type"], "autonomous": bool(row["autonomous"])}}

    @staticmethod
    def _task_query():
        return ("SELECT m.*,a.namespace,a.parent_key,a.generation,a.scope,a.name,a.task_type,a.autonomous,c.payload AS source_context "
                "FROM agent_mail m JOIN agents a ON a.id=m.agent_id JOIN contexts c ON c.token=m.token "
                "WHERE m.direction='to_agent' ")

    def task_status(self, token, identity):
        context = self.broker.context(token)
        with self.broker.connect() as db:
            row = db.execute(self._task_query() + "AND m.task_id=?", (identity,)).fetchone()
        if row is None:
            return None
        if not self._task_allowed(context, row):
            raise PermissionError("无权访问其他发起者、账号、人设或会话版本的子代理任务；自主任务仅主人可管理。")
        return self._task_view(row)

    def list_tasks(self, token, states=(), limit=20):
        context = self.broker.context(token)
        if not context.get("session_key"):
            return []
        with self.broker.connect() as db:
            rows = db.execute(self._task_query() + "AND a.namespace=? AND a.parent_key=? AND a.generation=? AND a.scope=? "
                "ORDER BY m.created DESC,m.seq DESC", self.binding(context)).fetchall()
        return [self._task_view(row) for row in rows if self._task_allowed(context, row)
                and (not states or row["state"] in states)][:limit]

    def cancel_task(self, token, identity):
        row = self.task_status(token, identity)
        if row is None:
            return None
        with self.broker.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE agent_mail SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'cancel_requested' END "
                "WHERE task_id=? AND state IN ('queued','running')", (identity,))
            agent_id = row["result"]["agent_id"]
            db.execute("UPDATE agents SET state=CASE "
                "WHEN EXISTS(SELECT 1 FROM agent_mail WHERE agent_id=? AND direction='to_agent' AND state IN ('running','cancel_requested')) THEN 'running' "
                "WHEN EXISTS(SELECT 1 FROM agent_mail WHERE agent_id=? AND direction='to_agent' AND state='queued') THEN 'queued' ELSE 'idle' END,updated=? "
                "WHERE id=? AND state NOT IN ('closing','closed')", (agent_id, agent_id, time.time(), agent_id))
        return self.task_status(token, identity)

    def all(self, namespace):
        with self.broker.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM agents WHERE namespace=? AND state!='closed'", (namespace,))]

    def claim(self, identity):
        with self.broker.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            agent = db.execute("SELECT state FROM agents WHERE id=?", (identity,)).fetchone()
            if not agent or agent[0] in ("closing", "closed"):
                return None
            if db.execute("SELECT 1 FROM agent_mail WHERE agent_id=? AND state='running' AND direction='to_agent'", (identity,)).fetchone():
                return None
            row = db.execute("SELECT * FROM agent_mail WHERE agent_id=? AND direction='to_agent' AND state='queued' ORDER BY seq LIMIT 1", (identity,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE agent_mail SET state='running' WHERE seq=?", (row["seq"],))
            db.execute("UPDATE agents SET state='running',updated=? WHERE id=?", (time.time(), identity))
            return dict(row)

    def complete(self, agent, message, state, text, *, payload=None):
        """Complete once; a final tool report becomes visible only after verification."""
        with self.broker.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM agent_mail WHERE seq=?", (message["seq"],)).fetchone()
            if not row or row[0] not in ("queued", "running", "cancel_requested"):
                return
            if row[0] == "cancel_requested":
                state, text = "cancelled", "发起者已取消这条子代理任务；工作区和历史记录已保留。"
                payload = {"code": "CANCELLED"}
            db.execute("UPDATE agent_mail SET state=? WHERE seq=?", (state, message["seq"]))
            reports = db.execute("SELECT seq FROM agent_mail WHERE agent_id=? AND reply_to=? AND state='staged'",
                                 (agent["id"], message["seq"])).fetchall()
            if state == "completed" and reports:
                for report in reports:
                    detail = json.loads(db.execute("SELECT payload FROM agent_mail WHERE seq=?", (report["seq"],)).fetchone()[0])
                    db.execute("UPDATE agent_mail SET payload=? WHERE seq=?",
                        (json.dumps({**detail, "success": True, **(payload or {})}, ensure_ascii=False), report["seq"]))
                db.execute("UPDATE agent_mail SET state='ready' WHERE agent_id=? AND reply_to=? AND state='staged'", (agent["id"], message["seq"]))
            else:
                db.execute("UPDATE agent_mail SET state='cancelled',seen=1 WHERE agent_id=? AND reply_to=? AND state='staged'", (agent["id"], message["seq"]))
                db.execute("INSERT OR IGNORE INTO agent_mail(agent_id,direction,text,payload,state,token,request_key,reply_to,created) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", (agent["id"], "to_main", text,
                    json.dumps({"kind": "result", "success": state == "completed", **(payload or {})}, ensure_ascii=False),
                    "ready", message["token"], "result:" + str(message["seq"]), message["seq"], time.time()))
            waiting = db.execute("SELECT 1 FROM agent_mail WHERE agent_id=? AND direction='to_agent' AND state='queued'", (agent["id"],)).fetchone()
            db.execute("UPDATE agents SET state=CASE WHEN state IN ('closing','closed') THEN state ELSE ? END,updated=? WHERE id=?",
                       ("queued" if waiting else "idle", time.time(), agent["id"]))

    def recover(self, namespace):
        with self.broker.connect() as db:
            rows = db.execute("SELECT m.* FROM agent_mail m JOIN agents a ON a.id=m.agent_id "
                "WHERE a.namespace=? AND m.direction='to_agent' AND m.state IN ('running','cancel_requested')", (namespace,)).fetchall()
        for row in rows:
            self.complete(self.agent(row["agent_id"]), dict(row), "interrupted",
                "机器人重启中断了这条消息；保留工作区和子代理身份，未重复执行。需要继续时发送新消息。", payload={"code": "INTERRUPTED"})
        return [dict(row) for row in rows]

    def inbox_parents(self, namespace):
        with self.broker.connect() as db:
            return [row[0] for row in db.execute("SELECT DISTINCT a.parent_key FROM agents a JOIN agent_mail m ON a.id=m.agent_id "
                "WHERE a.namespace=? AND m.direction='to_main' AND m.state='ready' AND m.seen=0", (namespace,))]

    def pending(self, namespace, parent_key, generation, limit=20):
        with self.broker.connect() as db:
            return [dict(row) for row in db.execute("SELECT m.*,a.name,a.autonomous FROM agent_mail m JOIN agents a ON a.id=m.agent_id "
                "WHERE a.namespace=? AND a.parent_key=? AND a.generation=? AND m.direction='to_main' AND m.state='ready' "
                "AND m.seen=0 ORDER BY m.seq LIMIT ?", (namespace, parent_key, generation, limit))]

    def acknowledge(self, messages):
        with self.broker.connect() as db:
            db.executemany("UPDATE agent_mail SET seen=1 WHERE seq=? AND direction='to_main'", [(row["seq"],) for row in messages])
