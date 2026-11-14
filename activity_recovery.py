"""Durable recovery requests; filtered content is skipped, never replayed."""
from __future__ import annotations

from runtime_control import finish_thread

import hashlib
import json
import time
import uuid
from urllib.parse import urlsplit, urlunsplit


def recovery(runtime, context, sid):
    runtime.session(context, sid)
    with runtime.connect() as db:
        row = db.execute("SELECT * FROM session_recoveries WHERE session=?", (sid,)).fetchone()
    return dict(row) if row else {"revision": 0, "applied_revision": 0, "action": "", "url": "",
        "code": "", "blocked_urls": "[]", "consecutive": 0, "state": "idle"}


def latest_frame(runtime, sid):
    with runtime.connect() as db:
        row = db.execute("SELECT data FROM observations WHERE session=? ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
    return json.loads(row[0]) if row else {}


def request_recovery(runtime, context, sid, action, *, url="", source_token="", automatic=False):
    runtime.require_owner(context)
    session = runtime.session(context, sid)
    if action not in ("skip", "restart") or session["state"] not in ("running", "paused", "failed", "interrupted"):
        raise ValueError("只可跳过或重启正在运行、暂停或失败的任务。")
    if context.get("sandbox_session_id") and not context.get("activity_controller"):
        raise PermissionError("执行分支不能自行重启；恢复由控制台执行。")
    target = runtime.game(context, session["target"])
    current = recovery(runtime, context, sid)
    blocked = json.loads(current["blocked_urls"])
    frame_url = latest_frame(runtime, sid).get("page_url", "")
    if automatic and frame_url and frame_url not in blocked:
        blocked.append(frame_url)
    from browser_control import browser_executable, checked_url
    browser = browser_executable(target["executable"])
    if url:
        if not browser:
            raise ValueError("恢复网址只适用于已授权浏览器。")
        url = checked_url(url)
    elif action == "skip" or current["code"] == "CONTENT_BLOCKED":
        if browser:
            parsed = urlsplit(frame_url or target["launch_url"])
            url = urlunsplit((parsed.scheme, parsed.netloc, "/", "", "")) if parsed.scheme in ("http", "https") else ""
        if not url or url in blocked:
            return _blocked_pause(runtime, context, sid, current, blocked,
                "需要其他阅读入口；用 sandbox skip --url 指定新页面后继续。")
    if url in blocked:
        raise ValueError("该页面已触发内容拦截，请选择其他入口；不能重放受阻内容。")
    if action == "skip" and url == frame_url:
        raise ValueError("跳过入口与当前页面相同，请用 --url 指定其他页面。")
    consecutive = current["consecutive"] + 1 if automatic else 0
    if automatic and consecutive > 1:
        return _blocked_pause(runtime, context, sid, current, blocked,
            "跳过后仍连续触发拦截且没有新阅读记录，已暂停；请选择其他入口后继续。")
    receipt_key = hashlib.sha256(json.dumps([sid, source_token or uuid.uuid4().hex, action, url]).encode()).hexdigest()
    from vm_protocol import file_lock
    with file_lock(runtime.root / "sessions" / sid / "desktop.lock", timeout=12):
        with runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT revision FROM recovery_receipts WHERE id=?", (receipt_key,)).fetchone()
            if previous:
                return {"session_id": sid, "recovery_revision": previous[0], "repeated": True, "state": current["state"]}
            active = db.execute("SELECT state FROM sessions WHERE id=?", (sid,)).fetchone()
            if active[0] not in ("running", "paused", "failed", "interrupted"):
                raise ValueError("任务已停止，未恢复或重放操作。")
            fresh = db.execute("SELECT * FROM session_recoveries WHERE session=?", (sid,)).fetchone()
            if fresh:
                blocked = list(dict.fromkeys([*json.loads(fresh["blocked_urls"]), *blocked]))
            revision = (fresh["revision"] if fresh else 0) + 1
            db.execute("INSERT OR REPLACE INTO session_recoveries VALUES(?,?,?,?,?,?,?,?,?,?)",
                (sid, revision, fresh["applied_revision"] if fresh else 0, action, url, "CONTENT_BLOCKED" if automatic else current["code"],
                 json.dumps(blocked, ensure_ascii=False), consecutive, "requested", time.time()))
            db.execute("INSERT INTO recovery_receipts VALUES(?,?)", (receipt_key, revision))
            # Revokes every previous worker context before a clean branch starts.
            db.execute("UPDATE session_directions SET revision=revision+1,url='',updated=? WHERE session=?", (time.time(), sid))
            db.execute("DELETE FROM session_controls WHERE session=?", (sid,))
    return {"session_id": sid, "recovery_revision": revision, "state": "recovery_requested", "url": url,
        "hint": "只表示恢复已请求；以 activities.recovery.state=applied 和新截图确认实际执行。"}


def _blocked_pause(runtime, context, sid, current, blocked, reason):
    with runtime.connect() as db:
        db.execute("INSERT OR REPLACE INTO session_recoveries VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sid, current["revision"], current["applied_revision"], "skip", "", "CONTENT_BLOCKED",
             json.dumps(blocked, ensure_ascii=False), current["consecutive"] + 1, "paused", time.time()))
        db.execute("INSERT OR REPLACE INTO session_controls VALUES(?,?,?)", (sid, "pause", reason))
        db.execute("UPDATE session_directions SET revision=revision+1,updated=? WHERE session=?", (time.time(), sid))
        db.execute("UPDATE games SET autoplay=0 WHERE id=(SELECT target FROM sessions WHERE id=?)", (sid,))
    return {"session_id": sid, "state": "paused", "code": "CONTENT_BLOCKED", "error": reason,
        "hint": "使用 sandbox skip --session-id 当前会话 --url 其他入口，或在新页面就绪后 restart。"}


async def apply_recovery(console, runtime, metadata, sid, worker_context, key, request, target):
    """Cancel/reset is done before observing a fresh, unblocked entry."""
    import asyncio
    from pathlib import Path
    current = recovery(runtime, metadata, sid)
    console.client.ai.reset_session(key)
    parent = console.client.ai.get_or_create_session(metadata["session_key"])
    console.client.ai.get_or_create_session(key).model = parent.model
    worker_context["activity_revision"] = runtime.direction(metadata, sid)["revision"]
    file = console.broker.issue_context(worker_context, runtime.root / "sessions" / sid / "contexts")
    token = json.loads(Path(file).read_text(encoding="utf-8"))["token"]
    observation = await finish_thread(runtime.observe, worker_context, sid)
    if current["url"]:
        receipt = await finish_thread(console.broker.desktop, token, "navigate",
            observation_id=observation["id"], url=current["url"], wait_ms=300)
        if not receipt.get("ok"):
            raise ValueError(receipt.get("error", "恢复入口导航失败，未重复提交受阻内容。"))
        observation = latest_frame(runtime, sid)
    if observation.get("page_url") in json.loads(current["blocked_urls"]):
        raise ValueError("恢复仍停留在受阻页面，已停止；请提供其他入口。")
    runtime.acknowledge_direction(worker_context, sid, worker_context["activity_revision"])
    with runtime.connect() as db:
        db.execute("UPDATE session_recoveries SET applied_revision=?,state='applied',updated=? WHERE session=? AND revision=?",
            (current["revision"], time.time(), sid, current["revision"]))
    # No old screenshot, checkpoint, note, transcript or filtered error detail.
    from companion_tasks import software_prompt, activity_rules
    from console_tools.contracts import agent_instructions
    from console_tools.desktop import frame_receipt
    clean_task = ("继续阅读普通公开内容，选择新的帖子或视频；跳过已经受阻的内容，不重复打开或提交。"
                  if current["code"] == "CONTENT_BLOCKED" else runtime.direction(metadata, sid)["text"] or request["task"])
    safe_target = {**target, "launch_url": current["url"] or observation.get("page_url", "")}
    prompt = (software_prompt(console, metadata, file, safe_target, {**request, "task": clean_task}, observation, sid, [])
        if target["kind"] == "software" else
        "使用受控桌面接口继续游玩，以当前真实截图为依据，不重放旧按键或旧模型历史。\n"
        + activity_rules(console.client.app_config.sandbox)
        + f"\n当前人设：{console.system_for(metadata['session_key'])}\n目标：{clean_task}\n"
        + f"当前上下文文件：{file}\n当前观察：{json.dumps(frame_receipt(observation, sid), ensure_ascii=False)}\n"
        + agent_instructions(file, worker=True, software=False))
    console.broker.update(request["id"], "running", {**json.loads(console.broker.record(request["id"])["result"]),
        "recovery": {"revision": current["revision"], "state": "applied", "action": current["action"],
                     "code": current["code"], "url": observation.get("page_url", "")}})
    return file, observation, prompt
