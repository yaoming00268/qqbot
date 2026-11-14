"""Optional work admission policy, shared by slash commands and agent submits."""
from __future__ import annotations

from console_tools.contracts import TASK_TYPES


def current_work_context(config, context):
    """Keep identity and policy from the same current configuration snapshot."""
    group = context.get("conversation_id", "") if context.get("is_group") else ""
    sender = context.get("sender_id", "")
    return {**context, "work_permissions": config.get_work_permissions(group).model_dump(),
            "is_master": config.is_master(group, sender),
            "is_admin": config.is_admin(group, sender, context.get("sender_role", ""))}


def authorize_work(kind, context, payload=None):
    # A new result page can start another agent-backed network search.
    if kind == "media_command" and (payload or {}).get("command") == "next":
        kind = "search"
    if kind not in TASK_TYPES:
        return
    policy = context.get("work_permissions") or {}
    access = (policy.get("task_access") or {}).get(kind, policy.get("access", "all"))
    member = str(context.get("sender_id") or "")
    allowed = access == "all" or (access != "disabled" and (
        context.get("is_master") or
        access == "admin" and context.get("is_admin") or
        member and member in policy.get("allowed_user_ids", [])))
    if access not in ("all", "admin", "master", "allowlist", "disabled") or not allowed:
        raise PermissionError("当前成员无权发起此类后台工作；请联系主人调整工作权限。")
