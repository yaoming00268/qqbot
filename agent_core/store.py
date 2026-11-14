"""Durable actor state. Transactions protect claims; external writes are never replayed."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class ActorStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS actors(id TEXT PRIMARY KEY,name TEXT,prompt TEXT,enabled INTEGER,
                    life TEXT NOT NULL DEFAULT '{}',updated REAL);
                CREATE TABLE IF NOT EXISTS actor_bindings(legacy_namespace TEXT,persona_id TEXT,actor_id TEXT,
                    PRIMARY KEY(legacy_namespace,persona_id));
                CREATE TABLE IF NOT EXISTS actor_accounts(id TEXT PRIMARY KEY,actor_id TEXT,platform TEXT,
                    config TEXT,revision TEXT,enabled INTEGER,updated REAL);
                CREATE TABLE IF NOT EXISTS actor_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,
                    actor_id TEXT,account_id TEXT,kind TEXT,scope TEXT,visibility TEXT,data TEXT,created REAL,
                    state TEXT DEFAULT 'pending');
                CREATE INDEX IF NOT EXISTS actor_events_pending ON actor_events(state,seq);
                CREATE TABLE IF NOT EXISTS actor_memories(id TEXT PRIMARY KEY,actor_id TEXT,account_id TEXT,
                    kind TEXT,text TEXT,scope TEXT,visibility TEXT,source_id TEXT,evidence TEXT,created REAL);
                CREATE INDEX IF NOT EXISTS actor_memory_scope ON actor_memories(actor_id,visibility,scope,created);
                CREATE TABLE IF NOT EXISTS actor_actions(id TEXT PRIMARY KEY,actor_id TEXT,account_id TEXT,
                    kind TEXT,payload TEXT,request_key TEXT,config_revision TEXT,state TEXT,result TEXT,
                    created REAL,updated REAL,UNIQUE(account_id,request_key));
                CREATE TABLE IF NOT EXISTS actor_leases(account_id TEXT PRIMARY KEY,owner TEXT,expires REAL);
                CREATE TABLE IF NOT EXISTS actor_drafts(id TEXT PRIMARY KEY,actor_id TEXT,account_id TEXT,
                    kind TEXT,text TEXT,target_url TEXT,state TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS actor_schedules(id TEXT PRIMARY KEY,actor_id TEXT,account_id TEXT,
                    url TEXT,interval REAL,next_due REAL,enabled INTEGER);
                CREATE TABLE IF NOT EXISTS actor_imports(actor_id TEXT,source TEXT,PRIMARY KEY(actor_id,source));
                CREATE TABLE IF NOT EXISTS actor_people(actor_id TEXT,platform TEXT,remote_id TEXT,person_id TEXT,
                    name TEXT,PRIMARY KEY(actor_id,platform,remote_id));
                CREATE TABLE IF NOT EXISTS actor_relationships(actor_id TEXT,person_id TEXT,scope TEXT,
                    data TEXT,updated REAL,PRIMARY KEY(actor_id,person_id,scope));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def upsert_actor(self, actor_id, name="", prompt="", enabled=True):
        with self.connect() as db:
            db.execute("INSERT INTO actors(id,name,prompt,enabled,updated) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET name=excluded.name,prompt=excluded.prompt,"
                       "enabled=excluded.enabled,updated=excluded.updated",
                       (actor_id, name or actor_id, prompt, int(enabled), time.time()))

    def actor(self, actor_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM actors WHERE id=?", (actor_id,)).fetchone()
        if not row:
            raise ValueError("角色不存在。")
        result = dict(row)
        result["life"] = json.loads(result["life"])
        return result

    def bind(self, namespace, persona_id, actor_id):
        with self.connect() as db:
            db.execute("INSERT INTO actor_bindings VALUES(?,?,?) ON CONFLICT(legacy_namespace,persona_id) "
                       "DO UPDATE SET actor_id=excluded.actor_id", (namespace, persona_id, actor_id))

    def account(self, account_id, actor_id=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM actor_accounts WHERE id=?", (account_id,)).fetchone()
        if not row or actor_id is not None and row["actor_id"] != actor_id:
            raise PermissionError("账号不属于当前角色。")
        result = dict(row)
        result["config"] = json.loads(result["config"])
        return result

    def accounts(self, actor_id):
        with self.connect() as db:
            rows = db.execute("SELECT id,platform,enabled,revision,config FROM actor_accounts WHERE actor_id=?",
                              (actor_id,)).fetchall()
        return [{"id": row["id"], "platform": row["platform"], "enabled": bool(row["enabled"]),
                 "actions": json.loads(row["config"]).get("actions", [])} for row in rows]

    def upsert_account(self, account_id, actor_id, platform, config, enabled=True):
        revision = digest([actor_id, platform, config, bool(enabled)])
        with self.connect() as db:
            db.execute("INSERT INTO actor_accounts VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "actor_id=excluded.actor_id,platform=excluded.platform,config=excluded.config,"
                       "revision=excluded.revision,enabled=excluded.enabled,updated=excluded.updated",
                       (account_id, actor_id, platform, encoded(config), revision, int(enabled), time.time()))
        return revision

    def ingest(self, actor_id, account_id, kind, data, *, source_id="", scope="", visibility="private", processed=False):
        identity = digest([actor_id, account_id, kind, scope, source_id or uuid.uuid4().hex])
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO actor_events(id,actor_id,account_id,kind,scope,visibility,data,created,state) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", (identity, actor_id, account_id, kind, scope, visibility,
                       encoded(data), time.time(), "processed" if processed else "pending"))
        return identity

    def events(self, actor_id=None, *, pending=False, limit=20):
        clauses, args = [], []
        if actor_id:
            clauses.append("actor_id=?"); args.append(actor_id)
        if pending:
            clauses.append("state='pending'")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM actor_events" + (" WHERE " + " AND ".join(clauses) if clauses else "") +
                              " ORDER BY seq LIMIT ?", (*args, min(100, limit))).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def finish_events(self, identities, state="processed"):
        with self.connect() as db:
            db.executemany("UPDATE actor_events SET state=? WHERE id=?", [(state, item) for item in identities])

    def claim_events(self, actor_id, limit=8):
        owner = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            key, now = "model:" + actor_id, time.time()
            lease = db.execute("SELECT * FROM actor_leases WHERE account_id=?", (key,)).fetchone()
            if lease and lease["expires"] > now:
                return "", []
            db.execute("UPDATE actor_events SET state='interrupted' WHERE actor_id=? AND state='processing'", (actor_id,))
            rows = db.execute("SELECT * FROM actor_events WHERE actor_id=? AND state='pending' ORDER BY seq LIMIT ?",
                              (actor_id, limit)).fetchall()
            if not rows:
                return "", []
            db.execute("INSERT OR REPLACE INTO actor_leases VALUES(?,?,?)", (key, owner, now + 60))
            db.executemany("UPDATE actor_events SET state='processing' WHERE id=?", [(row["id"],) for row in rows])
        return owner, [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def renew_lease(self, owner):
        with self.connect() as db:
            return db.execute("UPDATE actor_leases SET expires=? WHERE owner=? AND expires>?",
                              (time.time() + 60, owner, time.time())).rowcount == 1

    def owns_model_lease(self, actor_id, owner):
        with self.connect() as db:
            row = db.execute("SELECT owner,expires FROM actor_leases WHERE account_id=?", ("model:" + actor_id,)).fetchone()
        return bool(row and row["owner"] == owner and row["expires"] > time.time())

    def release_lease(self, owner):
        with self.connect() as db:
            db.execute("DELETE FROM actor_leases WHERE owner=?", (owner,))

    def remember(self, actor_id, text, *, kind="observation", account_id="", scope="", visibility="private",
                 source_id="", evidence=()):
        if not text.strip() or visibility not in ("private", "actor", "public"):
            raise ValueError("记忆正文或可使用范围无效。")
        identity = digest([actor_id, account_id, kind, source_id, scope, visibility, text])
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO actor_memories VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (identity, actor_id, account_id, kind, text[:64000], scope, visibility,
                        source_id, encoded(list(evidence)), time.time()))
        return identity

    def recall(self, actor_id, *, scope="", query="", limit=8, shared_only=False):
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.connect() as db:
            rows = db.execute("SELECT * FROM actor_memories WHERE actor_id=? AND "
                "(visibility IN ('actor','public') OR (?=0 AND scope=?)) AND text LIKE ? ESCAPE '\\' "
                "ORDER BY created DESC LIMIT ?", (actor_id, int(shared_only), scope, pattern, min(50, limit))).fetchall()
        return [{**dict(row), "evidence": json.loads(row["evidence"])} for row in rows]

    def life(self, actor_id):
        value = self.actor(actor_id)["life"]
        return {"reflection": "", "interests": [], "wishes": [], **value}

    def change_life(self, actor_id, change):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT life FROM actors WHERE id=?", (actor_id,)).fetchone()
            if not row:
                raise ValueError("角色不存在。")
            state = {"reflection": "", "interests": [], "wishes": [], **json.loads(row[0])}
            change(state)
            db.execute("UPDATE actors SET life=?,updated=? WHERE id=?", (encoded(state), time.time(), actor_id))
        return state

    def import_life(self, actor_id, source, value):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM actor_imports WHERE actor_id=? AND source=?", (actor_id, source)).fetchone():
                return
            row = db.execute("SELECT life FROM actors WHERE id=?", (actor_id,)).fetchone()
            state = {"reflection": "", "interests": [], "wishes": [], **json.loads(row[0])}
            if not state["reflection"]:
                state["reflection"] = value.get("reflection", "")
            state["interests"] = list(dict.fromkeys([*state["interests"], *value.get("interests", [])]))[-24:]
            wishes = {item["id"]: item for item in value.get("wishes", [])}
            wishes.update({item["id"]: item for item in state["wishes"]})
            state["wishes"] = list(wishes.values())[-36:]
            db.execute("UPDATE actors SET life=? WHERE id=?", (encoded(state), actor_id))
            db.execute("INSERT INTO actor_imports VALUES(?,?)", (actor_id, source))

    def action(self, identity, actor_id=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM actor_actions WHERE id=?", (identity,)).fetchone()
        if not row or actor_id is not None and row["actor_id"] != actor_id:
            raise PermissionError("动作不属于当前角色。")
        return {**dict(row), "payload": json.loads(row["payload"]), "result": json.loads(row["result"])}

    def prepare_action(self, actor_id, account_id, kind, payload, request_key):
        with self.connect() as db:
            existing = db.execute("SELECT id,actor_id,kind,payload FROM actor_actions WHERE account_id=? AND request_key=?",
                                  (account_id, request_key)).fetchone()
        if existing:
            if existing["actor_id"] != actor_id:
                raise PermissionError("动作不属于当前角色。")
            if existing["kind"] != kind or existing["payload"] != encoded(payload):
                raise ValueError("相同幂等键不能提交不同动作。")
            return self.action(existing["id"], actor_id)
        account = self.account(account_id, actor_id)
        if not account["enabled"] or not self.actor(actor_id)["enabled"]:
            raise PermissionError("角色或账号已停用。")
        if kind not in account["config"].get("actions", []):
            raise PermissionError("当前账号未启用动作：" + kind)
        identity = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO actor_actions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (identity, actor_id, account_id, kind, encoded(payload), request_key, account["revision"],
                 "queued", "{}", time.time(), time.time()))
            row = db.execute("SELECT id,kind,payload FROM actor_actions WHERE account_id=? AND request_key=?",
                             (account_id, request_key)).fetchone()
            if row["kind"] != kind or row["payload"] != encoded(payload):
                raise ValueError("相同幂等键不能提交不同动作。")
        return self.action(row["id"], actor_id)

    def claim_action(self, identity, *, lease_seconds=180):
        action = self.action(identity)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            lease = db.execute("SELECT * FROM actor_leases WHERE account_id=?", (action["account_id"],)).fetchone()
            if lease and lease["expires"] > now:
                return False
            # An expired claim may already have produced a remote side effect.
            if lease:
                db.execute("UPDATE actor_actions SET state='unknown',updated=? WHERE id=? AND state='running'",
                           (now, lease["owner"]))
            current = db.execute("SELECT state FROM actor_actions WHERE id=?", (identity,)).fetchone()
            if current[0] != "queued":
                return False
            db.execute("INSERT OR REPLACE INTO actor_leases VALUES(?,?,?)", (action["account_id"], identity, now + lease_seconds))
            db.execute("UPDATE actor_actions SET state='running',updated=? WHERE id=?", (now, identity))
        return True

    def queued_actions(self, limit=2):
        with self.connect() as db:
            rows = db.execute("SELECT a.id FROM actor_actions a LEFT JOIN actor_leases l ON l.account_id=a.account_id "
                "WHERE a.state='queued' AND (l.expires IS NULL OR l.expires<=?) ORDER BY a.created LIMIT ?",
                (time.time(), limit)).fetchall()
        return [self.action(row["id"]) for row in rows]

    def recover_expired_actions(self):
        result = {"code": "ACTION_INTERRUPTED", "input_posted": None,
                  "error": "执行租约已过期，保留动作并禁止自动重发。"}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT a.id FROM actor_actions a LEFT JOIN actor_leases l ON l.owner=a.id "
                              "WHERE a.state='running' AND (l.expires IS NULL OR l.expires<=?)", (time.time(),)).fetchall()
            for row in rows:
                db.execute("UPDATE actor_actions SET state='unknown',result=?,updated=? WHERE id=?",
                           (encoded(result), time.time(), row["id"]))
                db.execute("DELETE FROM actor_leases WHERE owner=?", (row["id"],))
        for row in rows:
            action = self.action(row["id"])
            self.ingest(action["actor_id"], action["account_id"], "action.unknown",
                {"action_id": action["id"], "kind": action["kind"], "result": result}, source_id=action["id"], visibility="actor")
        return len(rows)

    def finish_action(self, identity, state, result):
        with self.connect() as db:
            db.execute("UPDATE actor_actions SET state=?,result=?,updated=? WHERE id=?",
                       (state, encoded(result), time.time(), identity))
            db.execute("DELETE FROM actor_leases WHERE owner=?", (identity,))
        action = self.action(identity)
        source = identity
        if action["kind"] in ("read_content", "read_feed"):
            source = digest([action["config_revision"], state, result.get("url", action["payload"].get("url")),
                             result.get("title"), result.get("text"), result.get("code")])
        self.ingest(action["actor_id"], action["account_id"], "action." + state,
                    {"action_id": identity, "kind": action["kind"], "result": result}, source_id=source,
                    scope=action["payload"].get("target_url", ""), visibility="actor", processed=action["kind"] == "create_draft")
        return self.action(identity)

    def create_draft(self, actor_id, account_id, text, *, kind="publish_post", target_url="", draft_id=""):
        if not text.strip() or len(text) > 16000 or kind not in ("publish_post", "reply_comment"):
            raise ValueError("草稿类型或长度无效。")
        identity = draft_id or uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO actor_drafts VALUES(?,?,?,?,?,?,?,?)",
                       (identity, actor_id, account_id, kind, text, target_url, "draft", time.time()))
        return self.draft(identity, actor_id, account_id)

    def draft(self, identity, actor_id, account_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM actor_drafts WHERE id=? AND actor_id=? AND account_id=?",
                             (identity, actor_id, account_id)).fetchone()
        if not row:
            raise PermissionError("草稿不属于当前角色和账号。")
        return dict(row)

    def schedule(self, identity, actor_id, account_id, url, interval, enabled=True):
        with self.connect() as db:
            db.execute("INSERT INTO actor_schedules VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "actor_id=excluded.actor_id,account_id=excluded.account_id,url=excluded.url,"
                       "interval=excluded.interval,enabled=excluded.enabled",
                       (identity, actor_id, account_id, url, interval, time.time(), int(enabled)))

    def due(self, limit=2):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            rows = db.execute("SELECT s.* FROM actor_schedules s JOIN actor_accounts a ON a.id=s.account_id "
                "JOIN actors r ON r.id=s.actor_id WHERE s.enabled=1 AND a.enabled=1 AND r.enabled=1 AND s.next_due<=? "
                "ORDER BY s.next_due LIMIT ?", (now, limit)).fetchall()
            for row in rows:
                db.execute("UPDATE actor_schedules SET next_due=? WHERE id=?", (now + row["interval"], row["id"]))
        return [dict(row) for row in rows]

    def link_person(self, actor_id, platform, remote_id, name="", person_id=""):
        with self.connect() as db:
            existing = db.execute("SELECT person_id FROM actor_people WHERE actor_id=? AND platform=? AND remote_id=?",
                                  (actor_id, platform, remote_id)).fetchone()
            identity = person_id or (existing[0] if existing else uuid.uuid4().hex)
            if person_id and not db.execute("SELECT 1 FROM actor_people WHERE actor_id=? AND person_id=?",
                                           (actor_id, person_id)).fetchone():
                raise ValueError("关联目标必须是同一角色已有的人物 ID。")
            db.execute("INSERT INTO actor_people VALUES(?,?,?,?,?) ON CONFLICT(actor_id,platform,remote_id) "
                       "DO UPDATE SET person_id=excluded.person_id,name=excluded.name",
                       (actor_id, platform, remote_id, identity, name))
        return identity

    def relationship(self, actor_id, person_id, scope, data=None):
        with self.connect() as db:
            if data is not None:
                db.execute("INSERT INTO actor_relationships VALUES(?,?,?,?,?) ON CONFLICT(actor_id,person_id,scope) "
                           "DO UPDATE SET data=excluded.data,updated=excluded.updated",
                           (actor_id, person_id, scope, encoded(data), time.time()))
            row = db.execute("SELECT data FROM actor_relationships WHERE actor_id=? AND person_id=? AND scope=?",
                             (actor_id, person_id, scope)).fetchone()
        return json.loads(row[0]) if row else {}
