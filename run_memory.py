"""Durable transcripts and cumulative digests for each bot open/close interval."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


class RunMemory:
    def __init__(self, directory):
        self.root = Path(directory) / "runs"
        self.id, self.started = uuid.uuid4().hex, time.time()

    @property
    def database(self):
        return self.root / "journal.sqlite3"

    @contextmanager
    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, started REAL, ended REAL);
                    CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,run TEXT,grp TEXT,data TEXT);
                    CREATE INDEX IF NOT EXISTS run_messages ON messages(run,grp,seq);
                    CREATE TABLE IF NOT EXISTS digests(run TEXT,grp TEXT,cursor INTEGER,data TEXT,PRIMARY KEY(run,grp));
                """)
                yield db
        finally:
            db.close()

    def append(self, group, message):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO runs VALUES(?,?,NULL)", (self.id, self.started))
            db.execute("INSERT INTO messages(run,grp,data) VALUES(?,?,?)",
                (self.id, str(group), json.dumps(asdict(message), ensure_ascii=False)))

    def pending(self):
        if not self.database.exists():
            return []
        with self.connect() as db:
            return [(row[0], row[1]) for row in db.execute(
                "SELECT m.run,m.grp FROM messages m LEFT JOIN digests d ON d.run=m.run AND d.grp=m.grp "
                "JOIN runs r ON r.id=m.run WHERE m.seq>COALESCE(d.cursor,0) "
                "GROUP BY m.run,m.grp ORDER BY r.started,m.run,m.grp")]

    def pending_groups(self):
        return [group for run, group in self.pending() if run == self.id]

    def snapshot(self, group, run=None):
        run = run or self.id
        if not self.database.exists():
            return {"records": [], "cursor": 0, "previous": None, "total": 0,
                    "run_id": run, "started": self.started}
        with self.connect() as db:
            previous = db.execute("SELECT cursor,data FROM digests WHERE run=? AND grp=?", (run, str(group))).fetchone()
            rows = db.execute("SELECT seq,data FROM messages WHERE run=? AND grp=? AND seq>? ORDER BY seq",
                              (run, str(group), previous["cursor"] if previous else 0)).fetchall()
            total = db.execute("SELECT COUNT(*) FROM messages WHERE run=? AND grp=?", (run, str(group))).fetchone()[0]
            started = db.execute("SELECT started FROM runs WHERE id=?", (run,)).fetchone()
        return {"records": [json.loads(row["data"]) for row in rows],
                "cursor": rows[-1]["seq"] if rows else previous["cursor"] if previous else 0,
                "previous": json.loads(previous["data"]) if previous else None, "total": total,
                "run_id": run, "started": started[0] if started else self.started}

    def reference(self, group, run=None):
        return self.root / (run or self.id) / ("group_" + hashlib.sha256(str(group).encode()).hexdigest()[:24] + ".json")

    def save(self, group, snapshot, digest, *, complete):
        run = snapshot.get("run_id", self.id)
        reference = self.reference(group, run)
        reference.parent.mkdir(parents=True, exist_ok=True)
        raw = reference.with_suffix(".messages.jsonl")
        temporary = raw.with_suffix(".tmp")
        with self.connect() as db, temporary.open("w", encoding="utf-8") as stream:
            for row in db.execute("SELECT data FROM messages WHERE run=? AND grp=? AND seq<=? ORDER BY seq",
                                  (run, str(group), snapshot["cursor"])):
                stream.write(row[0] + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, raw)
        payload = {"group_openid": str(group), "run_id": run, "run_started_at": snapshot.get("started", self.started),
                   "run_message_count": snapshot["total"], "summary_complete": complete,
                   "coverage": {"new_messages": len(snapshot["records"]), "all_messages": snapshot["total"]},
                   **(digest or {})}
        temporary = reference.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, reference)
        if complete and digest:
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO digests VALUES(?,?,?,?)",
                    (run, str(group), snapshot["cursor"], json.dumps(digest, ensure_ascii=False)))
        return str(reference)

    def close(self):
        if self.database.exists():
            with self.connect() as db:
                db.execute("UPDATE runs SET ended=COALESCE(ended,?) WHERE id=?", (time.time(), self.id))

    def clear(self, group=None):
        if self.database.exists():
            with self.connect() as db:
                if group is None:
                    db.execute("DELETE FROM messages")
                    db.execute("DELETE FROM digests")
                else:
                    db.execute("DELETE FROM messages WHERE grp=?", (str(group),))
                    db.execute("DELETE FROM digests WHERE grp=?", (str(group),))
        prefix = "group_" + hashlib.sha256(str(group).encode()).hexdigest()[:24] if group is not None else "group_*"
        for path in self.root.glob("*/" + prefix + ".*"):
            if path.is_file() and path.suffix in (".json", ".jsonl"):
                path.unlink(missing_ok=True)
