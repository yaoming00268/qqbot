"""SQLite media metadata and idempotent jobs; no credentials or file bodies."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
import uuid

from media_downloader import MediaError
from media_models import MediaAsset


TERMINAL = {"accepted", "failed", "cancelled", "unknown"}


class MediaStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS assets(id TEXT NOT NULL, scope TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(id,scope));
                CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY, owner TEXT NOT NULL, query TEXT NOT NULL,
                    kind TEXT NOT NULL, page INTEGER NOT NULL, expires REAL NOT NULL, assets TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, owner TEXT NOT NULL, scope TEXT NOT NULL,
                    idempotency TEXT UNIQUE NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, updated REAL NOT NULL, assets TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS sends(job TEXT NOT NULL, asset TEXT NOT NULL, state TEXT NOT NULL,
                    receipt TEXT NOT NULL DEFAULT '', PRIMARY KEY(job,asset));
                CREATE TABLE IF NOT EXISTS replies(owner TEXT PRIMARY KEY, body TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS delivery_plans(job TEXT NOT NULL, input TEXT NOT NULL,
                    data TEXT NOT NULL, PRIMARY KEY(job,input));
                CREATE TABLE IF NOT EXISTS task_leases(job_id TEXT PRIMARY KEY, path TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL);
            """)
            # Preserve old rows without assigning mixed legacy state to a persona.
            # A reused asset ID may now appear in several isolated scopes.
            db.execute("BEGIN IMMEDIATE")
            keys = [row[1] for row in sorted(db.execute("PRAGMA table_info(assets)"), key=lambda row: row[5]) if row[5]]
            if keys == ["id"]:
                db.execute("CREATE TABLE assets_scoped(id TEXT NOT NULL,scope TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(id,scope))")
                db.execute("INSERT INTO assets_scoped SELECT id,scope,data FROM assets")
                db.execute("DROP TABLE assets")
                db.execute("ALTER TABLE assets_scoped RENAME TO assets")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put_asset(self, scope, asset: MediaAsset):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO assets VALUES(?,?,?)", (asset.asset_id, scope, asset.model_dump_json()))

    def delivery_plan(self, job_id, input_key):
        with self.connect() as db:
            row = db.execute("SELECT data FROM delivery_plans WHERE job=? AND input=?", (job_id, input_key)).fetchone()
        return json.loads(row[0]) if row else None

    def save_delivery_plan(self, job, input_key, assets, skipped):
        data = {"files": [asset.model_dump() for asset in assets], "skipped": skipped}
        with self.connect() as db:
            for asset in assets:
                db.execute("INSERT OR REPLACE INTO assets VALUES(?,?,?)", (asset.asset_id, job["scope"], asset.model_dump_json()))
            db.execute("INSERT OR REPLACE INTO delivery_plans VALUES(?,?,?)", (job["id"], input_key, json.dumps(data, ensure_ascii=False)))
            db.execute("UPDATE jobs SET assets=?,updated=? WHERE id=?", (json.dumps([asset.asset_id for asset in assets]), time.time(), job["id"]))

    def asset(self, scope, asset_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM assets WHERE id=? AND scope=?", (asset_id, scope)).fetchone()
        if not row:
            raise MediaError("资源不存在或不属于当前会话。")
        return MediaAsset.model_validate_json(row[0])

    def put_results(self, owner, query, kind, page, assets, ttl):
        result_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("INSERT INTO results VALUES(?,?,?,?,?,?,?)", (result_id, owner, query, kind, page,
                       time.time() + ttl, json.dumps([a.asset_id for a in assets])))
            db.execute("DELETE FROM results WHERE expires < ?", (time.time(),))
        return result_id

    def results(self, owner, result_id=""):
        with self.connect() as db:
            if result_id:
                row = db.execute("SELECT * FROM results WHERE owner=? AND id=?", (owner, result_id)).fetchone()
            else:
                row = db.execute("SELECT * FROM results WHERE owner=? ORDER BY rowid DESC LIMIT 1", (owner,)).fetchone()
        if not row or row["expires"] < time.time():
            raise MediaError("搜索结果已过期或不属于你，请重新搜索。")
        return dict(row)

    def create_job(self, owner, scope, message_id):
        job_id = uuid.uuid4().hex[:12]
        idem = owner + ":" + (message_id or uuid.uuid4().hex)
        now = time.time()
        with self.connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO jobs(id,owner,scope,idempotency,state,created,updated) VALUES(?,?,?,?,?,?,?)",
                                (job_id, owner, scope, idem, "queued", now, now))
            row = db.execute("SELECT * FROM jobs WHERE idempotency=?", (idem,)).fetchone()
        return dict(row), cursor.rowcount == 1

    def update_job(self, job_id, state, detail="", assets=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?,detail=?,updated=? WHERE id=?", (state, detail[:1000], time.time(), job_id))
            if assets is not None:
                db.execute("UPDATE jobs SET assets=? WHERE id=?", (json.dumps(assets), job_id))

    def job(self, owner, job_id=""):
        with self.connect() as db:
            if job_id:
                row = db.execute("SELECT * FROM jobs WHERE owner=? AND id=?", (owner, job_id)).fetchone()
            else:
                row = db.execute("SELECT * FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 1", (owner,)).fetchone()
        if not row:
            raise MediaError("没有找到你的媒体任务。")
        return dict(row)

    def begin_send(self, job_id, asset_id):
        with self.connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO sends(job,asset,state) VALUES(?,?,?)", (job_id, asset_id, "unknown"))
        if cursor.rowcount != 1:
            raise MediaError("这项发送已有记录，不能自动重复发送；请查询任务进度。")

    def receipt(self, job_id, asset_id, state, receipt=""):
        with self.connect() as db:
            db.execute("UPDATE sends SET state=?,receipt=? WHERE job=? AND asset=?", (state, receipt, job_id, asset_id))

    def sends(self, job_id):
        with self.connect() as db:
            return {row["asset"]: row["state"] for row in db.execute("SELECT asset,state FROM sends WHERE job=?", (job_id,))}

    def remember_reply(self, owner, body):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO replies VALUES(?,?,?)", (owner, body[:100000], time.time()))

    def last_reply(self, owner):
        with self.connect() as db:
            row = db.execute("SELECT body FROM replies WHERE owner=?", (owner,)).fetchone()
        return row[0] if row else ""

    def recover(self, scope):
        with self.connect() as db:
            db.execute("""UPDATE jobs SET state=CASE WHEN EXISTS(
                SELECT 1 FROM sends WHERE sends.job=jobs.id AND sends.state='unknown') THEN 'unknown' ELSE 'failed' END,
                detail='运行中断，请核对聊天记录后重新请求。'
                WHERE scope=? AND state NOT IN ('accepted','failed','cancelled','unknown','ready')""", (scope,))

    def clear_scope(self, scope):
        with self.connect() as db:
            db.execute("DELETE FROM replies WHERE substr(owner,1,?)=?", (len(scope) + 1, scope + ":"))
            db.execute("DELETE FROM results WHERE substr(owner,1,?)=?", (len(scope) + 1, scope + ":"))
            db.execute("UPDATE jobs SET state='cancelled',detail='清空上下文后任务已失效。' WHERE scope=? AND state NOT IN ('accepted','failed','cancelled','unknown')", (scope,))

    def clear_namespace(self, namespace):
        with self.connect() as db:
            if namespace:
                for table in ("replies", "results"):
                    db.execute(f"DELETE FROM {table} WHERE substr(owner,1,?)=?", (len(namespace) + 1, namespace + ":"))
                db.execute("UPDATE jobs SET state='cancelled',detail='清空上下文后任务已失效。' WHERE substr(scope,1,?)=? AND state NOT IN ('accepted','failed','cancelled','unknown')", (len(namespace) + 1, namespace + ":"))
            else:
                db.execute("DELETE FROM replies")
                db.execute("DELETE FROM results")
                db.execute("UPDATE jobs SET state='cancelled',detail='清空上下文后任务已失效。' WHERE state NOT IN ('accepted','failed','cancelled','unknown')")

    def register_lease(self, job_id: str, path: str, ttl: float = 86400) -> None:
        with self.connect() as db:
            now = time.time()
            db.execute("INSERT OR REPLACE INTO task_leases VALUES(?,?,?,?)",
                       (job_id, str(Path(path).resolve()), now, now + ttl))

    def release_lease(self, job_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM task_leases WHERE job_id=?", (job_id,))

    def active_paths(self, max_age=86400):
        with self.connect() as db:
            now = time.time()
            rows = db.execute("SELECT assets FROM jobs WHERE state NOT IN ('accepted','failed','cancelled','unknown') AND updated>?", (now - max_age,)).fetchall()
            ids = {a for row in rows for a in json.loads(row[0])}
            for row in db.execute("SELECT assets FROM results WHERE expires>?", (now,)):
                ids.update(json.loads(row[0]))
            paths = [json.loads(row["data"]).get("local_path", "") for row in db.execute("SELECT id,data FROM assets") if row["id"] in ids]
            leases = db.execute("SELECT path FROM task_leases WHERE expires>? AND (job_id NOT IN (SELECT id FROM jobs) OR job_id IN (SELECT id FROM jobs WHERE state NOT IN ('accepted','failed','cancelled','unknown')))", (now,)).fetchall()
            paths.extend(row[0] for row in leases if row[0])
            return paths
