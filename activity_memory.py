"""Cumulative semantic memories of observed software and fictional game experiences."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from memory_digest import summarize_records, write_memory_json
from persona_isolation import context_persona

logger = logging.getLogger("ActivityMemory")
_locks = {}
_attempts = {}


def recovery_signature(db, target):
    rows = db.execute("SELECT r.session,r.updated,r.blocked_urls FROM session_recoveries r "
        "JOIN sessions s ON s.id=r.session WHERE s.target=? AND r.blocked_urls<>'[]' ORDER BY r.session", (target,))
    return json.dumps([list(row) for row in rows])


def _visible_notes_sql():
    return ("NOT EXISTS (SELECT 1 FROM session_recoveries r WHERE r.session=n.session "
            "AND r.blocked_urls<>'[]' AND n.created<r.updated)")


def _source_urls(value):
    urls = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("url", "page_url", "current_url", "document_url") and isinstance(item, str) and item.startswith(("http://", "https://")):
                urls.append(item)
            elif isinstance(item, (dict, list)):
                urls.extend(_source_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_source_urls(item))
    return urls


def _snapshot(runtime, context, sid):
    session = runtime.session(context, sid)
    target = runtime.game(context, session["target"])
    with runtime.connect() as db:
        recovery = recovery_signature(db, target["id"])
        saved = db.execute("SELECT * FROM activity_digests WHERE target=?", (target["id"],)).fetchone()
        saved = dict(saved) if saved and saved["recovery"] == recovery else None
        cursor = saved["cursor"] if saved else 0
        notes = db.execute("SELECT n.rowid AS seq,n.* FROM notes n WHERE n.target=? AND n.rowid>? AND "
            + _visible_notes_sql() + " ORDER BY n.rowid", (target["id"], cursor)).fetchall()
        records, urls = [], []
        for row in notes:
            record = {key: row[key] for key in ("seq", "text", "kind", "created", "session")}
            record["evidence"] = json.loads(row["evidence"])
            record_urls = []
            for identity in record["evidence"]:
                observation = db.execute("SELECT data FROM observations WHERE id=? AND session=?", (identity, row["session"])).fetchone()
                if observation:
                    record_urls.extend(_source_urls(json.loads(observation[0])))
            record_urls.extend(re.findall(r"https?://[^\s<>\"，。；]+", row["text"]))
            record["sources"] = list(dict.fromkeys(record_urls))
            urls.extend(record_urls)
            records.append(record)
    previous = json.loads(saved["data"]) if saved else None
    return session, target, saved, recovery, records, previous, list(dict.fromkeys(urls))


async def summarize_activity(runtime, ai, context, sid, *, force=False, retry=False):
    """Only summarized notes advance the cursor; failures remain retryable on disk."""
    try:
        session = runtime.session(context, sid)
        key = (str(runtime.root), session["target"])
    except Exception:
        logger.debug("经历上下文尚不可用，保留原始错误与已有笔记", exc_info=True)
        return False
    async with _locks.setdefault(key, asyncio.Lock()):
        try:
            session, target, saved, recovery, records, previous, urls = _snapshot(runtime, context, sid)
            directory = runtime.root / "memories"
            path = directory / (target["id"] + ".json")
            if not records and not previous:
                if recovery:
                    with runtime.connect() as db:
                        db.execute("DELETE FROM activity_digests WHERE target=?", (target["id"],))
                return False
            if not records and saved and saved["session"] == sid and previous.get("state") == session["state"]:
                if not path.is_file():
                    write_memory_json(path, previous)
                return True
            if not force and (len(records) < 8 or time.monotonic() - _attempts.get(key, 0) < 60):
                return False
            _attempts[key] = time.monotonic()
            source = "software_observation" if target["kind"] == "software" else "fictional_game"
            checkpoint = {"kind": "operation_state", "state": session["state"], "goal": target["goal"],
                          "checkpoint": session["summary"], "instruction": "进度不是已读事实，内容结论仅来自观察笔记。"}
            summary_key = "activity_summary_" + target["id"]
            if records:
                write_memory_json(directory / (target["id"] + ".sources.json"),
                    {"target_id": target["id"], "session": sid, "recovery": recovery, "source": source,
                     "records": records, "checkpoint": checkpoint})
                folded = await summarize_records(ai, summary_key, [*records, checkpoint], previous=previous, source=source,
                    checkpoint_path=directory / (target["id"] + ".work.json"), retry=retry)
                covered = [row["seq"] for row in folded["processed"] if "seq" in row
                           and (not row.get("fragments") or row.get("fragment") == row["fragments"])]
                if not folded["digest"] or not covered:
                    return False
                digest = folded["digest"]
                complete = folded["complete"]
            else:
                digest = previous  # A changed operation state needs no new model call.
                complete = previous.get("summary_complete", True)
            cursor = max(covered) if records else saved["cursor"]
            payload = {**digest, "target_id": target["id"], "title": target["title"],
                       "goal": target["goal"], "source": source, "state": session["state"], "checkpoint": session["summary"][:1200],
                       "sources": list(dict.fromkeys([*((previous or {}).get("sources") or []), *urls]))[-30:],
                       "summary_complete": complete, "updated": time.time()}
            directory.mkdir(exist_ok=True)
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            with runtime.connect() as db:
                if recovery_signature(db, target["id"]) != recovery:
                    temporary.unlink(missing_ok=True)
                    return False
                os.replace(temporary, path)
                db.execute("INSERT OR REPLACE INTO activity_digests VALUES(?,?,?,?,?,?)",
                    (target["id"], cursor, json.dumps(payload, ensure_ascii=False), recovery, payload["updated"], sid))
            # Reuse this target's organizer across note batches and play
            # sessions; the digest and original observations are already saved.
            return complete
        except Exception:
            logger.exception("经历概括未完成，已保留观察与上次摘要供重试")
            return False


def activity_memories(runtime, context, query="", limit=3):
    """Return bounded summaries within the existing account/group/owner/persona scope."""
    with runtime.connect() as db:
        rows = db.execute("SELECT g.*,d.cursor,d.data,d.recovery,d.session FROM games g LEFT JOIN activity_digests d ON d.target=g.id "
            "WHERE g.namespace=? AND g.scope=? AND g.owner=? AND g.persona=? "
            + ("AND g.id=(SELECT target FROM sessions WHERE id=?) " if context.get("sandbox_session_id") else "")
            + "ORDER BY g.last_run DESC", (context["namespace"], context["scope"], context.get("sender_id", ""), context_persona(context))
            + ((context["sandbox_session_id"],) if context.get("sandbox_session_id") else ())).fetchall()
        results = []
        for row in rows:
            payload = json.loads(row["data"]) if row["data"] and row["recovery"] == recovery_signature(db, row["id"]) else {}
            if query and not any(word in json.dumps(payload, ensure_ascii=False).casefold() for word in query.casefold().split()):
                continue
            pending = db.execute("SELECT COUNT(*) FROM notes n WHERE n.target=? AND n.rowid>? AND "
                + _visible_notes_sql(), (row["id"], row["cursor"] if payload else 0)).fetchone()[0]
            item = {"target_id": row["id"], "title": row["title"], "source": "software_observation" if row["kind"] == "software" else "fictional_game",
                    "summary_complete": bool(payload.get("summary_complete")), "pending": bool(pending)}
            if payload:
                item["memory_file"] = str((runtime.root / "memories" / (row["id"] + ".json")).resolve())
            for name in ("summary", "state", "checkpoint", "reflection", "sources", "key_topics", "conclusions", "open_questions", "preferences"):
                if name in payload:
                    value = payload[name]
                    item[name] = value[:4] if isinstance(value, list) else value[:1200] if isinstance(value, str) else value
            if pending:
                item["pending_notes"] = pending
            results.append(item)
            if len(results) >= min(20, max(1, int(limit))):
                break
    return results


async def flush_activity_memories(console):
    """Persist pending experiences at startup/stop without retrying failed batches."""
    cfg = console.client.app_config.sandbox
    if not (Path(cfg.directory) / "sandbox.sqlite3").exists():
        return
    from sandbox_runtime import SandboxRuntime
    runtime = SandboxRuntime(cfg.directory)
    with runtime.connect() as db:
        rows = db.execute("SELECT s.id,s.target,g.source_token FROM sessions s JOIN games g ON g.id=s.target "
                          "WHERE s.namespace=? ORDER BY s.created DESC", (console.namespace,)).fetchall()
    seen = set()
    for row in rows:
        if row["target"] in seen:
            continue
        seen.add(row["target"])
        try:
            context = console.broker.context(row["source_token"], allow_expired=True)
            await summarize_activity(runtime, console.client.ai, context, row["id"], force=True)
        except (OSError, ValueError, PermissionError):
            logger.warning("一项经历摘要尚无有效上下文，保留原始观察")
