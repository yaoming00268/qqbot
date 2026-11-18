"""Durable drafts, deduplicated live events and non-replaying external receipts."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class BroadcastStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS broadcast_drafts(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL,
                revision INTEGER NOT NULL, state TEXT NOT NULL, created REAL, updated REAL);
            CREATE TABLE IF NOT EXISTS broadcast_receipts(
                request_key TEXT PRIMARY KEY, operation TEXT NOT NULL, payload_hash TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                created REAL, updated REAL);
            CREATE TABLE IF NOT EXISTS broadcast_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL, body TEXT NOT NULL, created REAL, routed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS bilibili_action_limits(
                request_key TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL,
                text_hash TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS bilibili_schedule(
                name TEXT PRIMARY KEY, next_due REAL NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def draft_row(row):
        return {**dict(row), "body": json.loads(row["body"])}

    def drafts(self, limit=50):
        with self.connect() as db:
            return [self.draft_row(r) for r in db.execute("SELECT * FROM broadcast_drafts ORDER BY updated DESC LIMIT ?", (min(100, max(1, int(limit))),))]

    def draft(self, draft_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM broadcast_drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            raise ValueError("草稿不存在。")
        return self.draft_row(row)

    def save_draft(self, kind, body, *, draft_id="", revision=0):
        if kind not in ("dynamic", "video"):
            raise ValueError("草稿类型必须为 dynamic 或 video。")
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if draft_id:
                previous = db.execute("SELECT * FROM broadcast_drafts WHERE id=?", (draft_id,)).fetchone()
                if not previous or previous["revision"] != revision:
                    raise ValueError("草稿版本已变化，请重新读取。")
                if previous["state"] in ("sending", "published", "unknown"):
                    raise ValueError("此版本已提交；请复制为新草稿。结果不明时先核对平台。")
                db.execute("UPDATE broadcast_drafts SET kind=?,body=?,revision=revision+1,state='draft',updated=? WHERE id=?",
                           (kind, json.dumps(body, ensure_ascii=False), now, draft_id))
            else:
                if revision:
                    raise ValueError("新草稿版本必须为 0。")
                draft_id = "bd_" + uuid.uuid4().hex
                db.execute("INSERT INTO broadcast_drafts VALUES(?,?,?,1,'draft',?,?)",
                           (draft_id, kind, json.dumps(body, ensure_ascii=False), now, now))
        return self.draft(draft_id)

    def delete_draft(self, draft_id, revision):
        with self.connect() as db:
            changed = db.execute("DELETE FROM broadcast_drafts WHERE id=? AND revision=? AND state IN ('draft','failed')", (draft_id, revision)).rowcount
            if not changed:
                raise ValueError("只能删除版本一致且未提交的草稿。")
        return {"deleted": draft_id}

    def receipt(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM broadcast_receipts WHERE request_key=?", (key,)).fetchone()
        return {**dict(row), "result": json.loads(row["result"])} if row else None

    def claim(self, operation, payload, key, *, draft_id="", revision=0, limits=None):
        if not key or len(key) > 200:
            raise ValueError("外发操作需要唯一 request_key（最多200字）。")
        signature = digest(payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM broadcast_receipts WHERE request_key=?", (key,)).fetchone()
            if row:
                if row["payload_hash"] != signature or row["operation"] != operation:
                    raise ValueError("request_key 已用于另一项操作；禁止改变参数重放。")
                return False
            if limits:
                now = time.time()
                recent = db.execute("SELECT COUNT(*),MAX(created) FROM bilibili_action_limits WHERE created>?", (now - 3600,)).fetchone()
                if recent[0] >= limits["hourly"] or recent[1] is not None and now - recent[1] < limits["interval"]:
                    raise PermissionError("账号操作已达频率限制，请稍后再继续。")
                start_day = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
                if limits["kind"] == "comment":
                    count = db.execute("SELECT COUNT(*) FROM bilibili_action_limits WHERE kind='comment' AND created>=?", (start_day,)).fetchone()[0]
                    thread = db.execute("SELECT COUNT(*) FROM bilibili_action_limits WHERE kind='comment' AND target=?", (limits["target"],)).fetchone()[0]
                    if count >= limits["daily_comments"] or thread >= limits["thread_replies"]:
                        raise PermissionError("今日评论或该讨论串参与次数已达上限。")
                if limits["text_hash"] and db.execute("SELECT 1 FROM bilibili_action_limits WHERE text_hash=? AND created>?",
                    (limits["text_hash"], now - 86400)).fetchone():
                    raise PermissionError("24 小时内已经提交相同内容，不重复发布。")
            if draft_id:
                changed = db.execute("UPDATE broadcast_drafts SET state='sending',updated=? WHERE id=? AND revision=? AND state='draft'",
                                     (time.time(), draft_id, revision)).rowcount
                if not changed:
                    raise ValueError("草稿版本不一致或已经提交，不能再次发布。")
            now = time.time()
            db.execute("INSERT INTO broadcast_receipts(request_key,operation,payload_hash,state,created,updated) VALUES(?,?,?,'running',?,?)", (key, operation, signature, now, now))
            if limits:
                db.execute("INSERT INTO bilibili_action_limits VALUES(?,?,?,?,?)",
                    (key, limits["kind"], limits["target"], limits["text_hash"], now))
        return True

    def schedule_due(self, name, interval, *, force=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = db.execute("SELECT next_due FROM bilibili_schedule WHERE name=?", (name,)).fetchone()
            if row and row[0] > now and not force:
                return False
            db.execute("INSERT OR REPLACE INTO bilibili_schedule VALUES(?,?)", (name, now + interval))
        return True

    def operations(self, limit=30):
        with self.connect() as db:
            rows = db.execute("SELECT request_key,operation,state,result,error,created,updated FROM broadcast_receipts ORDER BY created DESC LIMIT ?",
                              (min(100, max(1, int(limit))),)).fetchall()
        return [{**dict(row), "result": json.loads(row["result"])} for row in rows]

    def complete(self, key, state, *, result=None, error="", draft_id=""):
        with self.connect() as db:
            db.execute("UPDATE broadcast_receipts SET state=?,result=?,error=?,updated=? WHERE request_key=?",
                       (state, json.dumps(result or {}, ensure_ascii=False), str(error)[:600], time.time(), key))
            if draft_id:
                db.execute("UPDATE broadcast_drafts SET state=?,updated=? WHERE id=?",
                           ("published" if state == "accepted" else state, time.time(), draft_id))
        return self.receipt(key)

    def recover(self):
        # A crashed sender may already have reached the platform. Never replay it.
        with self.connect() as db:
            db.execute("UPDATE broadcast_receipts SET state='unknown',error='服务重启，须核对平台回执' WHERE state='running'")
            db.execute("UPDATE broadcast_drafts SET state='unknown' WHERE state='sending'")

    def ingest(self, kind, body, event_key):
        with self.connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO broadcast_events(event_key,kind,body,created) VALUES(?,?,?,?)", (event_key, kind, json.dumps(body, ensure_ascii=False), time.time()))
            return cursor.lastrowid if cursor.rowcount else None

    def events(self, after=0, limit=50, *, pending=False):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM broadcast_events WHERE seq>?" + (" AND routed=0" if pending else "") + " ORDER BY seq LIMIT ?", (int(after), min(200, max(1, int(limit))))).fetchall()
        return [{**dict(row), "body": json.loads(row["body"])} for row in rows]

    def mark_routed(self, sequences):
        with self.connect() as db:
            db.executemany("UPDATE broadcast_events SET routed=1 WHERE seq=?", ((s,) for s in sequences))
