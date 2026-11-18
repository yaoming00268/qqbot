"""Actor capabilities exposed through the existing bound CLI/MCP context."""
from __future__ import annotations

from agent_core.store import ActorStore, digest
from agent_core.web import ActionGateway


def operate(broker, token, operation, *, account_id="", url="", text="", draft_id="",
            draft_kind="publish_post", request_id="", request_key="", query="", limit=8):
    context = broker.context(token)
    if (not context.get("main_agent") or not context.get("actor_id") or not context.get("actor_database")
            or any(context.get(key) for key in ("agent_worker_id", "sandbox_session_id", "parent_request_id", "activity_controller"))):
        raise PermissionError("当前上下文没有角色账号能力。")
    store = ActorStore(context["actor_database"])
    actor_id, scope = context["actor_id"], context.get("scope", "")
    if operation == "accounts":
        return {"actor_id": actor_id, "accounts": store.accounts(actor_id)}
    if operation == "state":
        return {"actor_id": actor_id, **store.life(actor_id)}
    if operation == "memories":
        return {"memories": store.recall(actor_id, scope=scope, query=query, limit=limit)}
    if operation == "status":
        return store.action(request_id, actor_id)
    def guard():
        broker.context(token)
        with broker.connect() as db:
            broker._require_active_turn(db, token)
        if not store.actor(actor_id)["enabled"]:
            raise PermissionError("当前角色已停用。")
        if context.get("actor_lease_owner") and not store.owns_model_lease(actor_id, context["actor_lease_owner"]):
            raise PermissionError("当前角色活动的执行租约已失效。")
    guard()
    if operation in ("remember", "feedback"):
        if not text.strip() or len(text) > 600:
            raise ValueError("个人记录为 1–600 字。")
        identity = store.remember(actor_id, text.strip(), kind="feedback" if operation == "feedback" else "reflection",
            scope=scope, visibility="actor" if context.get("actor_event") else "private",
            source_id=context.get("source_message_id", ""))
        return {"id": identity, "state": "saved"}
    if not account_id:
        raise ValueError("请使用 accounts 返回的 account_id。")
    account = store.account(account_id, actor_id)
    if account["platform"] == "bilibili" and account["config"].get("adapter") == "broadcast":
        if not account["enabled"]:
            raise PermissionError("B站账号已停用。")
        if operation == "draft" and draft_kind == "publish_post" and not url:
            # Compatibility for existing turns that still choose actor draft.
            # Reuse the native authority/lease checks; drafts stay local.
            from broadcast_commands import operate as broadcast_operate
            return broadcast_operate(broker, token, "draft", broadcast_kind="dynamic", text=text)
        raise ValueError("B站账号使用原生 action=broadcast。草稿用 operation=draft，发布需 draft_id 和 revision，"
                         "阅读用 feed/search/video/comments；无需更改 actions。")
    if operation == "read":
        kind, payload = "read_content", {"url": url}
    elif operation == "draft":
        kind, payload = "create_draft", {"text": text, "draft_kind": draft_kind, "target_url": url}
    elif operation in ("publish", "comment"):
        if not (context.get("actor_event") or context.get("is_master")):
            raise PermissionError("网站发布需要角色自主运营上下文或主人的当前对话。")
        kind, payload = ("publish_post" if operation == "publish" else "reply_comment"), {"draft_id": draft_id}
    else:
        raise ValueError("未知角色操作。")
    identity = request_key or digest([context["namespace"], context["session_key"], context.get("generation", 0),
        context.get("source_message_id", ""), kind, payload])
    return ActionGateway(store).execute(actor_id, account_id, kind, payload, identity, guard=guard)
