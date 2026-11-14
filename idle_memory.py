"""Fixed files and durable source cursors for idle conversation summaries."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

from memory_digest import write_memory_json
from conversation_records import dialogue_records

SCHEMA_VERSION = 2


class IdleMemory:
    def __init__(self, directory, generation):
        self.root = Path(directory).resolve()
        self.generation = generation
        self.journal = self.root / "events.jsonl"
        self.path = self.root / "memory.json"
        self.progress = self.root / "memory-progress.json"

    @staticmethod
    def read(path):
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("记忆检查点不是对象")
        return value

    def signature(self):
        if not self.journal.exists():
            return {}
        stat = self.journal.stat()
        return {"generation": self.generation, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "schema_version": SCHEMA_VERSION}

    def append(self, messages):
        messages = dialogue_records(messages)
        if not messages:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"generation": self.generation, "messages": messages}, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def pending(self):
        signature = self.signature()
        return bool(signature.get("size") and self.read(self.progress).get("signature") != signature)

    def begin(self):
        if not self.pending():
            return None
        signature = self.signature()
        data = self.journal.read_bytes()
        previous = self.read(self.path)
        if previous.get("schema_version") != SCHEMA_VERSION:
            previous = {}
        cursor = previous.get("cursor_bytes", 0) if previous.get("generation") == self.generation else 0
        if (type(cursor) is not int or cursor < 0 or cursor > len(data) or
                hashlib.sha256(data[:cursor]).hexdigest() != previous.get("prefix_sha256")):
            cursor, previous = 0, {}
        records = []
        for line in data[cursor:].splitlines():
            if line.strip():
                record = json.loads(line)
                if isinstance(record, dict) and record.get("generation", self.generation) == self.generation:
                    records.append(record)
        records = dialogue_records(records)
        snapshot = {"signature": signature, "schema_version": SCHEMA_VERSION, "cursor_bytes": len(data),
                    "prefix_sha256": hashlib.sha256(data).hexdigest(), "status": "running", "updated": time.time()}
        write_memory_json(self.root / "memory-input.json", {**snapshot, "records": records})
        write_memory_json(self.progress, snapshot)
        return snapshot, previous, records

    def finish(self, snapshot, digest, conversation_id):
        payload = {**digest, **snapshot, "status": "complete", "summary_complete": True,
                   "source": "main_chat", "generation": self.generation,
                   "conversation_id": conversation_id, "journal": str(self.journal), "updated": time.time()}
        write_memory_json(self.path, payload)
        write_memory_json(self.progress, {**snapshot, "status": "complete"})
        return payload

    def fail(self, snapshot, status="failed"):
        write_memory_json(self.progress, {**snapshot, "status": status, "updated": time.time()})

    def reference(self):
        value = self.read(self.path)
        if (value.get("generation") != self.generation or not value.get("summary_complete")
                or value.get("schema_version") != SCHEMA_VERSION):
            return None
        stat = self.path.stat()
        return {"role": "archive_summary", "path": self.path.as_posix(), "size": stat.st_size,
                "version": hashlib.sha256(self.path.read_bytes()).hexdigest()[:16]}
