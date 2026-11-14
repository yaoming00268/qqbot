"""Live directions and observed reading shared with the main conversation."""
from __future__ import annotations

import json
from persona_isolation import context_persona

from sandbox_runtime import SandboxRuntime


def active_request(broker, context, target_id):
    with broker.connect() as db:
        rows = db.execute("SELECT * FROM requests WHERE namespace=? AND scope=? AND kind='sandbox_play' "
            "AND state IN ('submitted','queued','running') AND json_extract(result,'$.target_id')=? ORDER BY created DESC",
            (context["namespace"], context["scope"], target_id)).fetchall()
    for row in rows:
        owner = broker.context(row["token"], allow_expired=True)
        if owner.get("sender_id") == context.get("sender_id") and context_persona(owner) == context_persona(context):
            return dict(row)
    return None


def redirect(broker, runtime, context, request, text, url, token):
    data = json.loads(request["result"])
    if url:
        from browser_control import browser_executable, checked_url
        url = checked_url(url)
        if not browser_executable(runtime.game(context, data["target_id"])["executable"]):
            raise ValueError("网址调整只适用于浏览器任务。")
    sid = data.get("sandbox_session_id")
    if request["state"] == "running" and not sid:
        with runtime.connect() as db:
            session = db.execute("SELECT id FROM sessions WHERE request_id=? AND state='running'", (request["id"],)).fetchone()
        sid = session[0] if session else None
    if request["state"] == "running" and sid:
        receipt = runtime.steer(context, sid, text, url=url, source_token=token)
    else:
        receipt = {"direction_revision": 0}
        runtime.set_goal(context, data["target_id"], text, token)
        if url:
            with runtime.connect() as db:
                db.execute("UPDATE games SET launch_url=? WHERE id=?", (url, data["target_id"]))
    with broker.connect() as db:
        # Don't replace live worker checkpoints or a concurrent stop.
        latest = db.execute("SELECT result,state FROM requests WHERE id=?", (request["id"],)).fetchone()
        if latest["state"] not in ("submitted", "queued", "running"):
            raise ValueError("原任务已停止，未创建重复的浏览任务。")
        payload = json.loads(latest["result"])
        if url:
            payload["url"] = url
        payload["direction_revision"] = receipt["direction_revision"]
        db.execute("UPDATE requests SET task=?,result=? WHERE id=?", (text, json.dumps(payload, ensure_ascii=False), request["id"]))
    return {"id": request["id"], "state": latest["state"], "updated": True, **receipt,
        "hint": "已提交新阅读方向，后台将在同一浏览器和会话执行；以 applied_revision 确认接收，不代表已读完。"}


def snapshots(broker, context):
    cfg = context.get("sandbox_config") or {}
    if not cfg.get("enabled"):
        return []
    runtime = SandboxRuntime(cfg["directory"])
    runtime.require_owner(context)
    with runtime.connect() as db:
        bound = context.get("sandbox_session_id")
        sessions = db.execute("SELECT s.*,g.title,g.kind,g.goal,g.launch_url FROM sessions s JOIN games g ON g.id=s.target "
            "WHERE s.namespace=? AND s.scope=? AND s.owner=? AND g.persona=? "
            "AND (s.state IN ('running','paused','failed','interrupted') OR (s.state='completed' AND ?=1)) "
            + ("AND s.id=? " if bound else "") + "ORDER BY s.created DESC LIMIT 8",
            (context["namespace"], context["scope"], context["sender_id"], context_persona(context),
             int(bool(context.get("self_directed")))) + ((bound,) if bound else ())).fetchall()
        result = []
        for session in sessions:
            sid = session["id"]
            request = broker.record(session["request_id"])
            if not request:
                continue
            direction = runtime.direction(context, sid)
            frame_row = db.execute("SELECT data FROM observations WHERE session=? ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
            frame = json.loads(frame_row[0]) if frame_row else {}
            notes = [{**dict(row), "evidence": json.loads(row["evidence"])} for row in db.execute(
                "SELECT id,text,kind,evidence,created FROM notes WHERE session=? ORDER BY rowid DESC LIMIT 6", (sid,))]
            for note in notes:
                evidence = db.execute("SELECT data FROM observations WHERE id=? AND session=?", (note["evidence"][0], sid)).fetchone()
                note["direction_revision"] = json.loads(evidence[0]).get("direction_revision", 0) if evidence else 0
            payload = json.loads(request["result"])
            from activity_recovery import recovery
            repair = recovery(runtime, context, sid)
            blocked_urls = json.loads(repair["blocked_urls"])
            if frame.get("page_url") in blocked_urls:
                frame = {"page_url": frame.get("page_url"), "blocked": True}
            if blocked_urls:
                # The main agent must not inherit the filtered page through a
                # screenshot, checkpoint or a note from the same branch.
                notes = [note for note in notes if note["created"] >= repair.get("updated", float("inf"))]
            result.append({"request_id": session["request_id"], "session_id": sid, "target_id": session["target"],
                "title": session["title"], "kind": session["kind"], "state": session["state"],
                "goal": direction["text"] or session["goal"] or request["task"],
                "direction_revision": direction["revision"], "applied_revision": direction["applied_revision"],
                "page": {key: frame[key] for key in ("page_url", "title", "image_path", "id", "direction_revision", "blocked") if key in frame},
                "notes": notes, "checkpoint": "" if repair["code"] == "CONTENT_BLOCKED" else str(payload.get("checkpoint", session["summary"]))[:1800],
                "launch": payload.get("launch", {}), "startup": payload.get("startup", {}),
                "recovery": repair,
                "source": "software_observation" if session["kind"] == "software" else "fictional_game"})
    return result
