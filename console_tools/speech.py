"""Durable fragments; choosing an output also confirms speech is complete."""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from console_tools.files import ConsoleFileError


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS speech_fragments(
            cache_id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
            source_id TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
            namespace TEXT, scope TEXT, session_key TEXT, generation INTEGER,
            UNIQUE(owner,source_id));
        CREATE INDEX IF NOT EXISTS speech_session ON speech_fragments(namespace,scope,session_key,generation);
        CREATE TABLE IF NOT EXISTS speech_decisions(
            token TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL);
    """)


def owner_key(context):
    identity = [context.get(key, "") for key in
                ("namespace", "scope", "session_key", "generation", "sender_id")]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


class SpeechCommands:
    @staticmethod
    def _bind_speech(db, context):
        """Capture a fixed snapshot; a later token cannot erase newer fragments."""
        context = dict(context)
        if any(context.get(key) for key in ("sandbox_session_id", "parent_request_id", "agent_worker_id",
                "agent_event", "idle_event", "activity_controller", "self_activity", "proactive_event",
                "self_directed", "autonomous_agents")):
            # Derived grants copy ordinary metadata, never the human's gate or
            # speech snapshot. Their reports and completion use their own rules.
            context["speech_check_required"] = False
        if not context.get("speech_check_required"):
            context.pop("speech", None)
            context.pop("speech_current_messages", None)
            return context
        owner = owner_key(context)
        cached = [{"cache_id": row["cache_id"], **json.loads(row["payload"])} for row in db.execute(
            "SELECT cache_id,payload FROM speech_fragments WHERE owner=? ORDER BY cache_id", (owner,))]
        current = {}
        for row in context.get("speech_current_messages", []):
            if str(row.get("sender_id", "")) == str(context.get("sender_id", "")) and row.get("id"):
                current[str(row["id"])] = {key: row.get(key, "") for key in ("id", "sender_id", "text", "received_at")}
        identity = str(context.get("source_message_id") or "input:" + uuid.uuid4().hex)
        current[identity] = {"id": identity, "sender_id": str(context.get("sender_id", "")),
            "text": context.get("source_text", ""), "received_at": context.get("received_at", time.time())}
        context["speech"] = {"required": True, "cached_messages": cached,
                             "current_messages": list(current.values())}
        context.pop("speech_current_messages", None)
        return context

    def _speech_context(self, token):
        context = self.context(token)
        if any(context.get(key) for key in ("sandbox_session_id", "parent_request_id", "agent_worker_id",
                "agent_event", "idle_event", "activity_controller", "self_activity", "proactive_event",
                "self_directed", "autonomous_agents")):
            raise PermissionError("发言缓存只属于当前自然聊天的主代理，内部事件和执行分支不能读写。")
        if not context.get("speech_check_required"):
            raise ConsoleFileError("SPEECH_UNAVAILABLE", "当前事件不需要判断对方是否说完。", "按当前事件处理任务或内部通知。")
        return context

    def speech_status(self, token):
        context = self._speech_context(token)
        with self.connect() as db:
            row = db.execute("SELECT state FROM speech_decisions WHERE token=?", (token,)).fetchone()
        return {**context["speech"], "state": row[0] if row else "unchecked",
                "instruction": "合看 cached_messages 与 current_messages。未说完只用 speech hold，程序自动结束本轮，无需 finish；已说完直接 reply，提交后可维护通讯录、预设，全部操作完成后独立 finish，无需 speech ready。缓存只在新消息到来时重新判断，不催促补全。"}

    def speech(self, token, operation, text=""):
        if operation == "status":
            return self.speech_status(token)
        context = self._speech_context(token)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM speech_decisions WHERE token=?", (token,)).fetchone()
            if operation == "hold":
                receipt = {"state": "waiting", "next_action": "next_message",
                    "instruction": "原始发言已缓存，程序自动结束本轮。无需再调用 finish、wait 或停止工具，不发送追问或待命文字；下条消息携带缓存再判断。"}
                if row and row[0] == "held":
                    return receipt
                self._require_open_turn(db, token)
                if row and row[0] == "released":
                    raise ConsoleFileError("SPEECH_ALREADY_RELEASED", "本轮已有输出提交，不能再取消回答或缓存为未说完。")
                for message in context["speech"]["current_messages"]:
                    db.execute("INSERT OR IGNORE INTO speech_fragments(owner,source_id,payload,created,namespace,scope,session_key,generation) VALUES(?,?,?,?,?,?,?,?)",
                        (owner_key(context), str(message["id"]), json.dumps(message, ensure_ascii=False), time.time(),
                         context["namespace"], context["scope"], context.get("session_key", ""), context.get("generation", "")))
                db.execute("INSERT OR REPLACE INTO speech_decisions VALUES(?,?,?)", (token, "held", str(text)[:500]))
                self._wait_in_transaction(db, token, text or "发言未完，已缓存")
                return receipt
            if operation == "ready":
                if row and row[0] in ("ready", "released"):
                    return {"state": "ready", "next_action": "reply_or_finish"}
                self._require_open_turn(db, token)
                db.execute("INSERT OR REPLACE INTO speech_decisions VALUES(?,?,?)", (token, "ready", str(text)[:500]))
                return {"state": "ready", "next_action": "reply_or_finish"}
        raise ValueError("未知发言缓存操作；使用 status/hold/ready。")

    @staticmethod
    def _require_speech_ready(db, token, *, release=False):
        row = db.execute("SELECT payload FROM contexts WHERE token=?", (token,)).fetchone()
        context = json.loads(row[0]) if row else {}
        if not context.get("speech_check_required"):
            return
        decision = db.execute("SELECT state FROM speech_decisions WHERE token=?", (token,)).fetchone()
        if not decision:
            # The output command is itself the model's choice to answer. Keep
            # it in the same transaction as validation and submission so a
            # failed reply cannot consume speech or race a concurrent hold.
            db.execute("INSERT INTO speech_decisions VALUES(?,?,?)", (token, "ready", "直接输出确认发言完整"))
        elif decision[0] not in ("ready", "released"):
            raise ConsoleFileError("TURN_CLOSED", "本轮已缓存或取消，不能重新确认发言或发送。",
                "等待下一条消息提供的新 context_file。")
        if release:
            db.execute("UPDATE speech_decisions SET state='released' WHERE token=?", (token,))

    @staticmethod
    def _consume_speech(db, token):
        row = db.execute("SELECT payload FROM contexts WHERE token=?", (token,)).fetchone()
        context = json.loads(row[0]) if row else {}
        if not context.get("speech_check_required"):
            return
        decision = db.execute("SELECT state FROM speech_decisions WHERE token=?", (token,)).fetchone()
        if not decision or decision[0] not in ("ready", "released"):
            return
        for message in context["speech"]["cached_messages"]:
            db.execute("DELETE FROM speech_fragments WHERE owner=? AND cache_id=?",
                       (owner_key(context), message["cache_id"]))

    def has_pending_speech(self, namespace, scope, session_key, generation):
        # Readers need not initialize the work/sandbox runtime to check silence.
        with self.connect() as db:
            return db.execute("SELECT 1 FROM speech_fragments WHERE namespace=? AND scope=? AND session_key=? AND generation=? LIMIT 1",
                              (namespace, scope, session_key, generation)).fetchone() is not None
