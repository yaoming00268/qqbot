"""Small, durable interests and wishes; self-directed reading uses the existing browser worker."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time


def repair_pending_browse(broker, namespace):
    """Link old, unstarted reading grants to their original acknowledgement."""
    with broker.connect() as db:
        rows = db.execute("SELECT r.id,r.token,c.payload FROM requests r JOIN contexts c ON c.token=r.token "
            "WHERE r.namespace=? AND r.kind='sandbox_play' AND r.state='submitted'", (namespace,)).fetchall()
        for row in rows:
            context = json.loads(row["payload"])
            if not (context.get("self_directed") and context.get("ack_before_work") and not context.get("ack_token")):
                continue
            candidates = db.execute("SELECT token,payload FROM contexts WHERE namespace=? AND scope=? AND token!=?",
                (namespace, context["scope"], row["token"])).fetchall()
            parents = [item["token"] for item in candidates if (lambda parent:
                parent.get("turn_managed") and not parent.get("self_directed") and parent.get("main_agent")
                and all(parent.get(key) == context.get(key) for key in ("session_key", "generation", "received_at"))
            )(json.loads(item["payload"]))]
            if len(parents) == 1:
                context["ack_token"] = parents[0]
                db.execute("UPDATE contexts SET payload=? WHERE token=?", (json.dumps(context, ensure_ascii=False), row["token"]))
            else:
                result = {"code": "ACK_CONTEXT_MISSING", "error": "旧阅读任务无法唯一关联原聊天回合，请在当前会话重新安排。"}
                db.execute("UPDATE requests SET state='failed',result=? WHERE id=?", (json.dumps(result, ensure_ascii=False), row["id"]))


def _prepare(broker):
    with broker.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS companion_life(namespace TEXT, session_key TEXT, "
                   "state TEXT, PRIMARY KEY(namespace,session_key))")
        db.execute("CREATE TABLE IF NOT EXISTS companion_browse_locks(namespace TEXT, session_key TEXT, "
                   "created REAL, PRIMARY KEY(namespace,session_key))")


def state_for(broker, namespace, session_key):
    _prepare(broker)
    with broker.connect() as db:
        binding = db.execute("SELECT payload FROM contexts WHERE namespace=? AND json_extract(payload,'$.session_key')=? "
                             "ORDER BY rowid DESC LIMIT 1",
                             (namespace, session_key)).fetchone()
        context = json.loads(binding[0]) if binding else {}
        if context.get("actor_id") and context.get("actor_database"):
            from agent_core.store import ActorStore
            return ActorStore(context["actor_database"]).life(context["actor_id"])
        row = db.execute("SELECT state FROM companion_life WHERE namespace=? AND session_key=?",
                         (namespace, session_key)).fetchone()
    return json.loads(row[0]) if row else {"reflection": "", "interests": [], "wishes": []}


def _change(broker, context, change, token):
    _prepare(broker)
    if context.get("actor_id") and context.get("actor_database"):
        with broker.connect() as db:
            broker._require_active_turn(db, token)
        from agent_core.store import ActorStore
        return ActorStore(context["actor_database"]).change_life(context["actor_id"], change)
    binding = (context["namespace"], context["session_key"])
    with broker.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        broker._require_active_turn(db, token)
        row = db.execute("SELECT state FROM companion_life WHERE namespace=? AND session_key=?", binding).fetchone()
        state = json.loads(row[0]) if row else {"reflection": "", "interests": [], "wishes": []}
        change(state)
        db.execute("INSERT INTO companion_life VALUES(?,?,?) ON CONFLICT(namespace,session_key) "
                   "DO UPDATE SET state=excluded.state", (*binding, json.dumps(state, ensure_ascii=False)))
    return state


def operate(broker, token, operation, *, text="", kind="reflection", url="", wish_id="", wish_status=""):
    context = broker.context(token)
    if (not context.get("main_agent") or context.get("sandbox_session_id")
            or context.get("parent_request_id") or context.get("activity_controller") or context.get("agent_worker_id")):
        raise PermissionError("兴趣和愿望由主对话维护，执行分支只记录实际观察。")
    if operation == "state":
        return state_for(broker, context["namespace"], context["session_key"])
    if broker.reply_control(token).get("mode") in ("wait", "finished", "reported", "sealed", "cancelled"):
        raise PermissionError("本轮已结束，请使用下一事件的上下文。")
    text = str(text).strip()
    if operation in ("remember", "wish") and (not text or len(text) > 600):
        raise ValueError("只保存一条具体兴趣、简短感受或愿望，最多 600 字。")
    if operation == "remember":
        if kind not in ("reflection", "preference"):
            raise ValueError("个人记录只使用 reflection 或 preference；事实观察留在阅读笔记。")
        def remember(state):
            if kind == "reflection":
                state["reflection"] = text
            else:
                state["interests"] = [*filter(lambda item: item != text, state["interests"]), text][-8:]
        return _change(broker, context, remember, token)
    if operation == "wish":
        identity = hashlib.sha256("".join(text.split()).casefold().encode()).hexdigest()[:16]
        def wish(state):
            if not any(item["id"] == identity for item in state["wishes"]):
                state["wishes"] = [*state["wishes"], {"id": identity, "text": text,
                    "status": "pending", "created_at": time.time()}][-12:]
        return _change(broker, context, wish, token)
    if operation == "resolve":
        if wish_status not in ("mentioned", "fulfilled", "declined"):
            raise ValueError("愿望状态只支持 mentioned、fulfilled、declined；记下愿望不会授予权限。")
        def resolve(state):
            item = next((item for item in state["wishes"] if item["id"] == wish_id), None)
            if item is None:
                raise ValueError("愿望不存在，请使用 state 中的真实 wish_id。")
            item.update(status=wish_status, updated_at=time.time())
        return _change(broker, context, resolve, token)
    if operation != "browse":
        raise ValueError("未知自主生活操作。")
    if context.get("proactive_event") or context.get("idle_event") and not (context.get("self_activity") or context.get("agent_event")):
        raise PermissionError("当前事件没有安排独立行动的权限。")
    if not (context.get("self_activity_config") or {}).get("enabled"):
        raise PermissionError("独立活动已关闭；可以保存愿望，等主人开启后再浏览。")
    cfg = context.get("sandbox_config") or {}
    if not cfg.get("enabled") or cfg.get("mode", "background") != "background":
        raise ValueError("自主浏览需要启用后台窗口模式。")
    if not text or len(text) > 600:
        raise ValueError("请说明这次具体想读什么，最多 600 字。")
    from browser_control import checked_url
    url = checked_url(url)
    # A dedicated bot owner and browser profile keep this grant separate from
    # human accounts, app registrations and explicit autoplay permissions.
    owner = "self:" + hashlib.sha256(context["session_key"].encode()).hexdigest()[:20]
    _prepare(broker)
    binding = (context["namespace"], context["session_key"])
    with broker.connect() as db:
        # The short transaction also prevents two same-session idle events from
        # spawning competing browsers. An existing task is steered via feedback.
        db.execute("BEGIN IMMEDIATE")
        broker._require_active_turn(db, token)
        existing = db.execute("SELECT r.id,r.state FROM requests r JOIN contexts c ON r.token=c.token "
            "WHERE r.namespace=? AND r.scope=? AND r.kind='sandbox_play' "
            "AND json_extract(c.payload,'$.sender_id')=? "
            "AND r.state IN ('submitted','queued','running','paused','failed') ORDER BY r.created DESC LIMIT 1",
            (context["namespace"], context["scope"], owner)).fetchone()
        if existing:
            return {**dict(existing), "existing": True,
                    "hint": "沿用已有阅读任务；暂停或失败时先处理阻碍，不反复启动浏览器。"}
        # Reserve the slot before registration (which has its own database).
        reserved = db.execute("SELECT created FROM companion_browse_locks WHERE namespace=? AND session_key=?", binding).fetchone()
        if reserved and time.time() - reserved[0] < 30:
            return {"state": "starting", "existing": True}
        db.execute("INSERT OR REPLACE INTO companion_browse_locks VALUES(?,?,?)", (*binding, time.time()))
    try:
        metadata = {**{key: value for key, value in context.items() if key not in (
            "agent_event", "autonomous_agents", "agent_notice_ids", "_agent_source_token")},
            "is_master": False, "is_admin": False, "self_directed": True,
            "sender_id": owner, "source_message_id": "", "source_text": url,
            "input_images": [], "quoted_message_id": "", "reply_sources": [], "memory_files": [],
            "proactive_event": False, "idle_event": False, "self_activity": False,
            "turn_managed": False, "work_permissions": {"access": "disabled"},
            "ack_token": context.get("ack_token") or token,
            "sandbox_config": {**cfg, "profiles": {}, "browser_profiles": {}},
            "artifact_roots": [], "output_dir": ""}
        context_file = broker.issue_context(metadata, broker.path.parent / "self_contexts")
        internal_token = json.loads(Path(context_file).read_text(encoding="utf-8"))["token"]
        receipt = broker.sandbox(internal_token, "browse", url=url, text=(
            "这是你出于自身兴趣安排的阅读：" + text +
            "。阅读实际页面并记录见闻和感受；本次具体兴趣得到满足后保存笔记并结束，不无限刷新或扩展。"
            "仅浏览公开内容；需要登录或额外能力时暂停，留给主对话决定是否提出愿望。"))
        row = broker.record(receipt["id"])
        broker.update(row["id"], row["state"], {**json.loads(row["result"]), "quiet": True})
        return receipt
    finally:
        with broker.connect() as db:
            db.execute("DELETE FROM companion_browse_locks WHERE namespace=? AND session_key=?", binding)
