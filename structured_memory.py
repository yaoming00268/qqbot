"""Structured, revisioned event books and evidence-backed archive reconciliation."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
import logging

from console_tools.files import ConsoleFileError


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def strings(value, limit=30, width=500):
    return list(dict.fromkeys(str(item).strip()[:width] for item in value if isinstance(item, (str, int)) and str(item).strip()))[:limit] if isinstance(value, list) else []


def timestamp(value, *, end_of_day=False):
    """Compare ISO dates, local archive dates and offset datetimes consistently."""
    if value in (None, ""):
        return ""
    if isinstance(value, (float, int)):
        date = datetime.fromtimestamp(value, timezone.utc)
    else:
        value = str(value).strip()
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if end_of_day and len(value) == 10:
            date += timedelta(days=1, microseconds=-1)
    return date.astimezone(timezone.utc).isoformat(timespec="microseconds")


def known_date(value):
    try:
        normalized = timestamp(value)
        return str(value).strip() if isinstance(value, str) and normalized else normalized
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def normalize_sections(value):
    """Keep people and events as objects; never stringify structured summaries."""
    events, people, growth = [], [], []
    for row in value.get("events", [])[:80] if isinstance(value.get("events"), list) else []:
        if not isinstance(row, dict) or not (str(row.get("title", "")).strip() or row.get("id") and row.get("operation") == "delete"):
            continue
        events.append({"id": str(row.get("id", ""))[:100], "title": str(row.get("title", "")).strip()[:200],
            "summary": str(row.get("summary", ""))[:3000], "started_at": known_date(row.get("started_at", "")),
            "ended_at": known_date(row.get("ended_at", "")), "people": strings(row.get("people"), width=100),
            "tags": strings(row.get("tags"), width=60), "evidence": strings(row.get("evidence")),
            "status": row.get("status") if row.get("status") in ("open", "resolved", "historical", "uncertain") else "historical",
            "operation": "delete" if row.get("operation") == "delete" else "upsert"})
    for row in value.get("people", [])[:60] if isinstance(value.get("people"), list) else []:
        if not isinstance(row, dict) or not str(row.get("user_id", "")).strip():
            continue
        person = {"user_id": str(row["user_id"])[:100], "operation": "delete" if row.get("operation") == "delete" else "upsert"}
        for field in ("facts", "preferences", "relationships", "tags", "remove_facts", "remove_preferences", "remove_relationships", "remove_tags", "evidence"):
            if field in row:
                person[field] = strings(row[field], width=60 if field.endswith("tags") else 500)
        if isinstance(row.get("preferred_name"), str):
            person["preferred_name"] = row["preferred_name"][:120]
        people.append(person)
    for row in value.get("growth", [])[:12] if isinstance(value.get("growth"), list) else []:
        if isinstance(row, dict) and str(row.get("observation", "")).strip():
            growth.append({"observation": str(row["observation"])[:600], "lesson": str(row.get("lesson", ""))[:600],
                           "proposal": str(row.get("proposal", ""))[:600], "evidence": strings(row.get("evidence"))})
    return {"events": events, "people": people, "growth": growth}


class StructuredMemory:
    @staticmethod
    def prepare_structured(db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_events(owner TEXT,scope TEXT,id TEXT,data TEXT,
                revision INTEGER,deleted INTEGER DEFAULT 0,updated REAL,PRIMARY KEY(owner,scope,id));
            CREATE INDEX IF NOT EXISTS event_scope ON memory_events(owner,scope,deleted,updated);
            CREATE TABLE IF NOT EXISTS event_history(seq INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT,
                scope TEXT,id TEXT,revision INTEGER,data TEXT,reason TEXT,source_id TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS archive_projections(owner TEXT,scope TEXT,id TEXT,updated REAL,
                PRIMARY KEY(owner,scope,id));
            CREATE TABLE IF NOT EXISTS memory_reviews(owner TEXT,scope TEXT,archive_id TEXT,
                decision TEXT,preset_revision INTEGER,reason TEXT,updated REAL,PRIMARY KEY(owner,scope,archive_id));
        """)

    def event(self, owner, scope, event_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM memory_events WHERE owner=? AND scope=? AND id=?", (owner, scope, event_id)).fetchone()
        return {**json.loads(row["data"]), "id": row["id"], "revision": row["revision"], "deleted": bool(row["deleted"])} if row else {"id": event_id, "revision": 0, "state": "missing"}

    def events(self, owner, scope, *, query="", user_id="", tag="", since="", until="", offset=0, limit=20):
        since, until = timestamp(since), timestamp(until, end_of_day=True)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM memory_events WHERE owner=? AND scope=? AND deleted=0 "
                "AND (?='' OR instr(lower(data),lower(?))>0)", (owner, scope, query, query)).fetchall()
        result = []
        for row in rows:
            value = {**json.loads(row["data"]), "id": row["id"], "revision": row["revision"]}
            stamp = timestamp(value.get("started_at", ""))
            if user_id and user_id not in value.get("people", []) or tag and tag not in value.get("tags", []):
                continue
            if since and stamp < since or until and stamp > until:
                continue
            result.append(value)
        result.sort(key=lambda item: (timestamp(item.get("started_at", "")), item["id"]), reverse=True)
        return {"events": result[offset:offset + limit], "next_offset": offset + limit if len(result) > offset + limit else None}

    def edit_event(self, owner, scope, event_id, revision, reason, source_id, *, delete=False, **changes):
        for field in ("started_at", "ended_at"):
            if changes.get(field):
                timestamp(changes[field])  # Reject invalid dates before mutating memory.
        event_id = event_id or hashlib.sha256(encoded([owner, scope, changes.get("started_at", ""), changes.get("title", "")]).encode()).hexdigest()[:24]
        with self.connect() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM memory_events WHERE owner=? AND scope=? AND id=?", (owner, scope, event_id)).fetchone()
            current = row["revision"] if row else 0
            value = {**(json.loads(row["data"]) if row else {}), **changes}
            if row and current == revision + 1 and bool(row["deleted"]) == delete and json.loads(row["data"]) == value:
                return {**value, "id": event_id, "revision": current, "deleted": delete}
            if current != revision:
                raise ConsoleFileError("REVISION_CONFLICT", "事件已变化，请先 events show 获取最新 revision。")
            if row and bool(row["deleted"]) == delete and json.loads(row["data"]) == value:
                return {**value, "id": event_id, "revision": current, "deleted": delete}
            if not delete and not str(value.get("title", "")).strip():
                raise ValueError("事件需要明确标题。")
            now = time.time()
            db.execute("INSERT INTO memory_events VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner,scope,id) DO UPDATE SET "
                "data=excluded.data,revision=excluded.revision,deleted=excluded.deleted,updated=excluded.updated",
                (owner, scope, event_id, encoded(value), current + 1, int(delete), now))
            db.execute("INSERT INTO event_history(owner,scope,id,revision,data,reason,source_id,updated) VALUES(?,?,?,?,?,?,?,?)",
                (owner, scope, event_id, current + 1, encoded({**value, "deleted": delete}), reason, source_id, now))
        return self.event(owner, scope, event_id)

    def event_history(self, owner, scope, event_id, limit=20):
        with self.connect() as db:
            rows = db.execute("SELECT revision,data,reason,source_id,updated FROM event_history WHERE owner=? AND scope=? AND id=? ORDER BY seq DESC LIMIT ?",
                              (owner, scope, event_id, limit)).fetchall()
        return {"history": [{**dict(row), "data": json.loads(row["data"])} for row in rows]}

    def ingest_archive(self, owner, scope, archive_id, payload):
        """Idempotently project complete archives; preserve explicit tombstones."""
        with self.connect() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM archive_projections WHERE owner=? AND scope=? AND id=?", (owner, scope, archive_id)).fetchone():
                return
            self._project_archive(owner, scope, archive_id, payload)
            db.execute("INSERT OR IGNORE INTO archive_projections VALUES(?,?,?,?)", (owner, scope, archive_id, time.time()))

    def recover_projections(self):
        """Backfill legacy archives and retry interrupted projections on open."""
        with self.connect() as db:
            rows = db.execute("SELECT a.* FROM identity_archives a LEFT JOIN archive_projections p "
                "ON a.owner=p.owner AND a.scope=p.scope AND a.id=p.id WHERE p.id IS NULL ORDER BY a.updated").fetchall()
        for row in rows:
            try:
                self.ingest_archive(row["owner"], row["scope"], row["id"], json.loads(row["payload"]))
            except (ValueError, OSError) as error:
                logging.getLogger("StructuredMemory").warning("归档投影保留待恢复：%s (%s)", row["id"], type(error).__name__)

    def _project_archive(self, owner, scope, archive_id, payload):
        sections = normalize_sections(payload)
        participants = payload.get("participants", {})
        participants = {str(key): value for key, value in participants.items()} if isinstance(participants, dict) else {}
        known = set(participants) if isinstance(participants, dict) else set()
        known.update(str(row["user_id"]) for row in sections["people"] if self.get(owner, scope, row["user_id"]).get("state") != "missing")
        for user_id, person in participants.items() if isinstance(participants, dict) else []:
            if not isinstance(person, dict) or person.get("role") != "assistant":
                self.observe(owner, scope, str(user_id), (person.get("nickname") if isinstance(person, dict) else person) or str(user_id))
        started = known_date(payload.get("session_start") or payload.get("started_at") or "")
        ended = known_date(payload.get("session_end") or payload.get("ended_at") or started)
        if not sections["events"]:
            descriptions = strings(payload.get("notable_events"), limit=30, width=3000)
            if not descriptions and payload.get("summary"):
                descriptions = [str(payload["summary"])[:3000]]
            sections["events"] = [{"title": text[:100], "summary": text, "started_at": started, "ended_at": ended,
                "tags": strings(payload.get("key_topics"), width=60), "people": [str(user) for user, person in participants.items() if not isinstance(person, dict) or person.get("role") != "assistant"], "status": "historical", "evidence": []} for text in descriptions]
        for event in sections["events"]:
            explicit_id = event.get("id", "")
            event_id = explicit_id or hashlib.sha256(encoded([owner, scope, event.get("started_at") or started, event["title"]]).encode()).hexdigest()[:24]
            current = self.event(owner, scope, event_id)
            if current.get("deleted"):
                continue
            deletion = event.get("operation") == "delete"
            if deletion and current.get("state") == "missing":
                continue
            value = {key: event[key] for key in ("title", "summary", "status", "tags", "evidence") if key in event}
            value.update(started_at=event.get("started_at") or started, ended_at=event.get("ended_at") or ended,
                         people=[user for user in event.get("people", []) if user in known])
            value["sources"] = list(dict.fromkeys([*current.get("sources", []), archive_id]))[-40:]
            self.edit_event(owner, scope, event_id, current["revision"], "归档核对事件", archive_id, delete=deletion, **value)
        portraits = payload.get("member_portraits", {})
        if isinstance(portraits, dict):
            represented = {row["user_id"] for row in sections["people"]}
            sections["people"] += [{"user_id": str(user), "facts": [str(text)], "tags": []} for user, text in portraits.items() if str(user) in known and str(user) not in represented]
        for person in sections["people"]:
            user = person["user_id"]
            current = self.get(owner, scope, user)
            if user not in known or current.get("deleted"):
                continue
            if isinstance(participants.get(user), dict) and participants[user].get("role") == "assistant":
                continue
            changes = {}
            for field in ("facts", "preferences", "relationships", "tags"):
                old = current.get(field, [])
                removed = set(person.get("remove_" + field, []))
                changes[field] = strings([*(item for item in old if item not in removed), *person.get(field, [])], limit=60)
            if "preferred_name" in person:
                changes["preferred_name"] = person["preferred_name"]
            evidence = {"archive_id": archive_id, "at": ended or started, "evidence": person.get("evidence", [])}
            changes["timeline"] = [*(item for item in current.get("timeline", []) if item.get("archive_id") != archive_id), evidence][-80:]
            if person.get("operation") == "delete" or any(current.get(field) != value for field, value in changes.items()):
                self.edit_contact(owner, scope, user, current["revision"], "归档核对人物资料", archive_id,
                    delete=person.get("operation") == "delete", **changes)

    def review_archive(self, owner, scope, archive_id, decision, revision, reason):
        if decision not in ("keep", "update") or not str(reason).strip():
            raise ValueError("复盘需明确 keep/update，并提供具体依据。")
        with self.connect() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM identity_archives WHERE id=? AND owner=? AND scope=?", (archive_id, owner, scope)).fetchone()
            if not row:
                raise PermissionError("归档不属于当前人物和会话。")
            payload = json.loads(row[0])
            current = db.execute("SELECT revision FROM presets WHERE owner=?", (owner,)).fetchone()
            current_revision = current[0] if current else 0
            if revision != current_revision:
                raise ConsoleFileError("REVISION_CONFLICT", "成长预设已变化，请重新 preset show 后提交复盘。")
            if decision == "update" and current_revision <= payload.get("preset_revision", 0):
                raise ValueError("update 需要先通过 preset update 落实有依据的改变。")
            db.execute("INSERT INTO memory_reviews VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner,scope,archive_id) DO UPDATE SET "
                "decision=excluded.decision,preset_revision=excluded.preset_revision,reason=excluded.reason,updated=excluded.updated",
                (owner, scope, archive_id, decision, revision, reason, time.time()))
        return {"archive_id": archive_id, "state": "reviewed", "decision": decision, "preset_revision": revision}

    def reviewed(self, owner, scope):
        with self.connect() as db:
            return {row[0] for row in db.execute("SELECT archive_id FROM memory_reviews WHERE owner=? AND scope=?", (owner, scope))}

    def memory_index(self, owner, scope, user_id=""):
        with self.connect() as db:
            counts = {"events": db.execute("SELECT COUNT(*) FROM memory_events WHERE owner=? AND scope=? AND deleted=0", (owner, scope)).fetchone()[0],
                      "people": db.execute("SELECT COUNT(*) FROM contacts WHERE owner=? AND scope=? AND deleted=0", (owner, scope)).fetchone()[0]}
        recent = self.events(owner, scope, user_id=user_id, limit=4)["events"]
        return {**counts, "recent_events": [{key: row.get(key) for key in ("id", "title", "started_at", "status", "tags", "people")} for row in recent],
                "query": "events list(query,user_id,tag,since,until) 查事件与时间线；contacts list/show 查人物事实、偏好、关系与来源。"}


def operate(broker, token, action, operation, **params):
    from bot_identity import IdentityStore
    from console_tools.contracts import capabilities
    context = broker.context(token)
    if operation not in capabilities(context).get(action, []):
        raise PermissionError("当前上下文没有结构化记忆能力。")
    store = IdentityStore(context["identity_database"])
    owner, scope = context["identity_owner"], context["scope"]
    event_id = params.pop("event_id", "")
    if action == "events":
        if operation == "list":
            return store.events(owner, scope, **params)
        if operation == "show":
            return store.event(owner, scope, event_id)
        if operation == "history":
            return store.event_history(owner, scope, event_id, params.get("limit", 20))
    elif operation == "pending":
        return {"archives": store.pending(owner, scope, params.get("limit", 1), detailed=True)}
    with broker.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        broker._require_active_turn(db, token)
        if action == "memory":
            return store.review_archive(owner, scope, **params)
        if "text" in params:
            params["summary"] = params.pop("text")
        if "event_status" in params:
            params["status"] = params.pop("event_status")
        return store.edit_event(owner, scope, event_id, params.pop("revision"), params.pop("reason"),
            context.get("source_message_id") or context.get("session_key"), delete=operation == "delete", **params)
