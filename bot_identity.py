"""Durable, account/persona scoped address books and editable personal growth.

Transport observations are labels, never personality claims. Only a main-agent
CLI transaction can edit a portrait or the evolving part of its own preset.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time

from console_tools.files import ConsoleFileError
from structured_memory import StructuredMemory


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def brief_contact(person):
    """Incremental prompt hint; the complete portrait remains available via CLI."""
    return {**{key: person[key] for key in ("user_id", "platform_name", "preferred_name", "revision") if key in person},
            "known_names": person.get("known_names", [])[-5:], "aliases": person.get("aliases", [])[:6],
            **{key: [str(item)[:180] for item in person[key][:3]] for key in ("facts", "preferences", "relationships", "tags") if person.get(key)},
            **{key: str(person[key])[:500] for key in ("speaking_style", "habits", "notes") if person.get(key)}}


class IdentityStore(StructuredMemory):
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._connections = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS contacts(
                    owner TEXT, scope TEXT, user_id TEXT, observed TEXT DEFAULT '',
                    labels TEXT DEFAULT '[]', profile TEXT DEFAULT '{}', revision INTEGER DEFAULT 0,
                    deleted INTEGER DEFAULT 0, updated REAL,
                    PRIMARY KEY(owner,scope,user_id));
                CREATE TABLE IF NOT EXISTS presets(
                    owner TEXT PRIMARY KEY, text TEXT, revision INTEGER, updated REAL);
                CREATE TABLE IF NOT EXISTS identity_history(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, scope TEXT, target TEXT,
                    revision INTEGER, value TEXT, reason TEXT, source_id TEXT, updated REAL);
                CREATE TABLE IF NOT EXISTS identity_archives(
                    id TEXT PRIMARY KEY, owner TEXT, scope TEXT, session_key TEXT,
                    payload TEXT, done INTEGER DEFAULT 0, updated REAL);
                CREATE INDEX IF NOT EXISTS identity_pending ON identity_archives(owner,scope,done);
            """)
            self.prepare_structured(db)
        self.recover_projections()

    @contextmanager
    def connect(self):
        # Reuse a transaction during archive reconciliation. All event/person
        # changes and its completion marker commit together, including retries.
        active = getattr(self._connections, "active", None)
        if active is not None:
            yield active
            return
        with closing(sqlite3.connect(self.path, timeout=20)) as db:
            db.row_factory = sqlite3.Row
            self._connections.active = db
            try:
                with db:
                    yield db
            finally:
                self._connections.active = None

    def observe(self, owner, scope, user_id, name):
        user_id, name = str(user_id or ""), str(name or "").strip()[:120]
        if not user_id or not name:
            return
        with self.connect() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM contacts WHERE owner=? AND scope=? AND user_id=?",
                             (owner, scope, user_id)).fetchone()
            # A deletion is a tombstone; platform refresh and old archives may
            # not bring a deliberately forgotten portrait back.
            if row and row["deleted"]:
                return
            labels = json.loads(row["labels"]) if row else []
            labels = list(dict.fromkeys([*labels, name]))[-30:]
            db.execute("INSERT INTO contacts(owner,scope,user_id,observed,labels,updated) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(owner,scope,user_id) DO UPDATE SET observed=excluded.observed,labels=excluded.labels,updated=excluded.updated",
                (owner, scope, user_id, name, encoded(labels), time.time()))

    @staticmethod
    def contact(row):
        if row is None:
            return None
        profile = json.loads(row["profile"])
        return {"user_id": row["user_id"], "platform_name": row["observed"],
                "known_names": json.loads(row["labels"]), **profile,
                "revision": row["revision"], "deleted": bool(row["deleted"])}

    def get(self, owner, scope, user_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM contacts WHERE owner=? AND scope=? AND user_id=?",
                             (owner, scope, user_id)).fetchone()
        return self.contact(row) or {"user_id": user_id, "revision": 0, "state": "missing"}

    def contacts(self, owner, scope, *, query="", offset=0, limit=20):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM contacts WHERE owner=? AND scope=? AND deleted=0 "
                "AND (?='' OR instr(lower(user_id||observed||labels||profile),lower(?))>0) "
                "ORDER BY updated DESC,user_id LIMIT ? OFFSET ?", (owner, scope, query, query, limit + 1, offset)).fetchall()
        return {"contacts": [self.contact(row) for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}

    def edit_contact(self, owner, scope, user_id, revision, reason, source_id, *, delete=False, **changes):
        with self.connect() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM contacts WHERE owner=? AND scope=? AND user_id=?",
                             (owner, scope, user_id)).fetchone()
            current = row["revision"] if row else 0
            profile = json.loads(row["profile"]) if row else {}
            value = {} if delete else {**profile, **changes}
            # Exact repeats after a lost receipt are harmless, including deletes.
            if row and current == revision + 1 and bool(row["deleted"]) == delete and profile == value:
                return self.contact(row)
            if current != revision:
                raise ConsoleFileError("REVISION_CONFLICT", "通讯录已改变，请先 contacts show 读取最新 revision。")
            if delete and not row:
                raise ValueError("该成员尚无记录。")
            labels = json.loads(row["labels"]) if row else []
            if not delete and changes.get("preferred_name") and profile.get("preferred_name"):
                labels = list(dict.fromkeys([*labels, profile["preferred_name"]]))[-30:]
            db.execute("INSERT INTO contacts(owner,scope,user_id,profile,revision,deleted,updated) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(owner,scope,user_id) DO UPDATE SET profile=excluded.profile,revision=excluded.revision,"
                "deleted=excluded.deleted,updated=excluded.updated" + (",observed='',labels='[]'" if delete else ""),
                (owner, scope, user_id, encoded(value), current + 1, int(delete), time.time()))
            if not delete:
                db.execute("UPDATE contacts SET labels=? WHERE owner=? AND scope=? AND user_id=?",
                           (encoded(labels), owner, scope, user_id))
            db.execute("INSERT INTO identity_history(owner,scope,target,revision,value,reason,source_id,updated) VALUES(?,?,?,?,?,?,?,?)",
                (owner, scope, user_id, current + 1, encoded({"deleted": delete, **value}), reason, source_id, time.time()))
        return self.get(owner, scope, user_id)

    def preset(self, owner):
        with self.connect() as db:
            row = db.execute("SELECT text,revision FROM presets WHERE owner=?", (owner,)).fetchone()
        return dict(row) if row else {"text": "", "revision": 0}

    def edit_preset(self, owner, text, revision, reason, source_id):
        with self.connect() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT text,revision FROM presets WHERE owner=?", (owner,)).fetchone()
            current = row["revision"] if row else 0
            if row and current == revision + 1 and row["text"] == text:
                return dict(row)
            if current != revision:
                raise ConsoleFileError("REVISION_CONFLICT", "预设已改变，请先 preset show 读取最新 revision。")
            if row and row["text"] == text or not row and not text:
                return dict(row) if row else {"text": "", "revision": 0}
            db.execute("INSERT INTO presets VALUES(?,?,?,?) ON CONFLICT(owner) DO UPDATE SET "
                "text=excluded.text,revision=excluded.revision,updated=excluded.updated", (owner, text, current + 1, time.time()))
            db.execute("INSERT INTO identity_history(owner,scope,target,revision,value,reason,source_id,updated) VALUES(?,?,?,?,?,?,?,?)",
                (owner, "", "__preset__", current + 1, encoded({"text": text}), reason, source_id, time.time()))
        return self.preset(owner)

    def history(self, owner, scope, target, limit=20):
        with self.connect() as db:
            rows = db.execute("SELECT revision,value,reason,source_id,updated FROM identity_history "
                "WHERE owner=? AND scope=? AND target=? ORDER BY seq DESC LIMIT ?", (owner, scope, target, limit)).fetchall()
        return {"history": [{**dict(row), "value": json.loads(row["value"])} for row in rows]}

    def enqueue_archive(self, owner, scope, session_key, payload):
        identity = hashlib.sha256(encoded([owner, scope, payload]).encode()).hexdigest()[:32]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            payload = {**payload, "preset_revision": self.preset(owner)["revision"]}
            db.execute("INSERT OR IGNORE INTO identity_archives VALUES(?,?,?,?,?,0,?)",
                       (identity, owner, scope, session_key, encoded(payload), time.time()))
            self.ingest_archive(owner, scope, identity, payload)
        return identity

    def pending(self, owner, scope, limit=4, *, detailed=False):
        with self.connect() as db:
            rows = db.execute("SELECT id,payload FROM identity_archives WHERE owner=? AND scope=? AND done=0 "
                              "ORDER BY updated LIMIT ?", (owner, scope, limit)).fetchall()
        result = []
        preset_revision = self.preset(owner)["revision"]
        for row in rows:
            payload = json.loads(row["payload"])
            if not detailed:
                payload = {key: payload[key] for key in ("path", "version", "source", "preset_revision", "session_start", "session_end") if key in payload}
                full = json.loads(row["payload"])
                payload.update(summary=str(full.get("summary", ""))[:1000],
                    events=[{key: item.get(key) for key in ("id", "title", "status", "tags")} for item in full.get("events", [])[:6] if isinstance(item, dict)],
                    people=[{key: item.get(key) for key in ("user_id", "operation")} for item in full.get("people", [])[:8] if isinstance(item, dict)],
                    growth=full.get("growth", [])[:4],
                    detail_hint="memory pending 查看完整分区；read-memory 按页核对归档依据，events/contacts 查询当前投影。")
            result.append({"archive_id": row["id"], **payload,
                "review_required": True, "growth_review": {
                    "current_preset_revision": preset_revision,
                    "triggers": ["主人明确纠正的习惯", "反复出现的有效或无效行为", "有真实阅读依据且愿意延续的兴趣", "与旧预设冲突的新证据"],
                    "instruction": "核对事件、人物与成长候选；有持续价值时先 preset update，否则说明保留理由。最后 memory review(archive_id,decision=keep/update,revision,reason)，再 finish。"}})
        return result

    def pending_sessions(self, owners):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT owner,scope,session_key,MAX(updated) AS updated "
                "FROM identity_archives WHERE done=0 GROUP BY owner,scope,session_key ORDER BY updated").fetchall()
                if row["owner"] in owners]

    def acknowledge(self, owner, scope, ids):
        with self.connect() as db:
            db.executemany("UPDATE identity_archives SET done=1 WHERE owner=? AND scope=? AND id=?",
                           [(owner, scope, item) for item in ids])

    def clear(self, owner, scope=None):
        with self.connect() as db:
            for table in ("contacts", "identity_archives", "memory_events", "event_history", "archive_projections", "memory_reviews"):
                db.execute(f"DELETE FROM {table} WHERE owner=?" + (" AND scope=?" if scope is not None else ""),
                           (owner, scope) if scope is not None else (owner,))
            db.execute("DELETE FROM identity_history WHERE owner=? AND target!='__preset__'" +
                       (" AND scope=?" if scope is not None else ""), (owner, scope) if scope is not None else (owner,))


def store_for(client):
    path = Path(client.app_config.bot.runtime_database_path).resolve().parent / "main_chat" / "identity.sqlite3"
    cached = getattr(client, "_identity_store", None)
    if not isinstance(cached, IdentityStore) or cached.path != path:
        cached = client._identity_store = IdentityStore(path)
    return cached


def bind(client, session_key):
    from persona_isolation import persona_for, session_group, identity, namespace
    persona = persona_for(client, session_group(session_key), session_key)
    store = store_for(client)
    base_prompt = persona.prompt
    from agent_core.runtime import for_client
    runtime = for_client(client)
    if runtime:
        actor_id = runtime.bind_legacy(namespace(client), getattr(persona, "id", "") or persona.name,
                                       persona.name, persona.prompt)
        base_prompt = runtime.store.actor(actor_id)["prompt"] or base_prompt
    return {"identity_database": str(store.path), "identity_owner": identity(client, persona),
            "identity_base_prompt": base_prompt, "identity_persona_name": persona.name}


def scope_for(client, group):
    from persona_isolation import namespace
    return namespace(client) + ":group:" + str(group)


def set_name(client, session_key, scope, user_id, name, source_id):
    binding = bind(client, session_key)
    store, owner = store_for(client), binding["identity_owner"]
    store.observe(owner, scope, user_id, name)
    current = store.get(owner, scope, user_id)
    return store.edit_contact(owner, scope, user_id, current["revision"], "本人或主人明确设置称呼", source_id,
                              preferred_name=name)


def archive_ready(client, owner, group, path):
    """Called only after a validated archive write; safe during shutdown too."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("archive_file"):
        original = Path(data["archive_file"])
        # The local summary is only a pointer within its own profile directory.
        if original.resolve().parent != Path(path).resolve().parent:
            raise ValueError("归档指针不属于本人物记忆目录。")
        path = original
        data = json.loads(original.read_text(encoding="utf-8"))
    if not data.get("summary_complete"):
        return
    from persona_isolation import MARKER
    key = ("onebot_group_" if hasattr(client, "send_action") else "group_") + str(group) + MARKER + owner
    store_for(client).enqueue_archive(owner, scope_for(client, group), key,
        {"path": str(Path(path).resolve()), "version": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
         **{name: data[name] for name in ("summary", "member_portraits", "participants", "events", "people", "growth", "notable_events", "key_topics", "session_start", "session_end") if name in data}})


def operate(broker, token, action, operation, **params):
    context = broker.context(token)
    from console_tools.contracts import capabilities
    if operation not in capabilities(context).get(action, []) or not context.get("identity_owner"):
        raise PermissionError("当前上下文没有主代理通讯录或预设能力。")
    store = IdentityStore(context["identity_database"])
    owner, scope = context["identity_owner"], context["scope"]
    user_id = params.pop("user_id", "")
    limit, offset, query = params.pop("limit", 20), params.pop("offset", 0), params.pop("query", "")
    if action == "contacts":
        if operation == "list":
            return store.contacts(owner, scope, query=query, offset=offset, limit=limit)
        if operation == "show":
            return store.get(owner, scope, user_id)
        if operation == "history":
            return store.history(owner, scope, user_id, limit)
    elif operation == "show":
        return {**store.preset(owner), "base_prompt": context["identity_base_prompt"],
                "instruction": "text 是可编辑的个人成长预设，替换该层不会更改基础人设、账号权限或主会话规则。"}
    elif operation == "history":
        return store.history(owner, "", "__preset__", limit)
    # Serialize edits against finish: no late write can race the turn closure.
    with broker.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        broker.context(token)
        broker._require_active_turn(db, token)
        source = context.get("source_message_id", "") or context.get("session_key", "")
        if action == "contacts":
            known = store.get(owner, scope, user_id)
            sources = [str(row.get("sender_id", "")) for row in context.get("reply_sources", [])]
            if known.get("state") == "missing" and user_id not in [context.get("sender_id"), *sources]:
                raise PermissionError("新成员须使用本会话实际出现的账号 ID，不能用昵称猜测或合并身份。")
            return store.edit_contact(owner, scope, user_id, params.pop("revision"), params.pop("reason"), source,
                                      delete=operation == "delete", **params)
        return store.edit_preset(owner, "" if operation == "reset" else params["text"],
                                 params["revision"], params["reason"], source)
