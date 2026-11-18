"""Owner/actor-scoped CLI for the shared broadcast service, without secrets."""
from __future__ import annotations

from pathlib import Path

from broadcast_service import ACTOR_CUES, ACTOR_OPERATIONS


def context_binding(config_path, *, is_master=False, actor_id="", account_ops=False):
    from config import AppConfig
    path = Path(config_path).resolve()
    if not path.is_file():
        return {}
    config = AppConfig.load(str(path))
    if not (config.bilibili.enabled or config.vtuber.enabled):
        return {}
    if actor_id:
        permitted = (config.bilibili.autonomy.enabled and config.bilibili.autonomy.actor_id == actor_id
                     if account_ops else config.bilibili.auto_reply and config.bilibili.actor_id == actor_id)
        if (not config.agent.enabled or not config.bilibili.enabled or not permitted
                or actor_id not in config.agent.actors or not config.agent.actors[actor_id].enabled):
            return {}
    elif not is_master:
        return {}
    result = {"broadcast_config_path": str(path), "broadcast_actor": actor_id}
    if account_ops and actor_id:
        result["broadcast_account_ops"] = True
    return result


def operate(broker, token, operation, **params):
    from broadcast_host import execute
    from console_tools.contracts import capabilities
    context = broker.context(token)
    if operation not in capabilities(context).get("broadcast", []):
        raise PermissionError("本轮没有直播运营权限。")
    path = context.get("broadcast_config_path")
    if not path or not context.get("main_agent"):
        raise PermissionError("缺少已授权的运营配置绑定。")
    actor_id = context.get("broadcast_actor", "")
    account_ops = bool(context.get("broadcast_account_ops"))
    binding = context_binding(path, is_master=bool(context.get("is_master")), actor_id=actor_id, account_ops=account_ops)
    if not binding:
        raise PermissionError("直播运营授权已撤回。")
    if actor_id:
        from bilibili_operations import ACCOUNT_OPERATIONS
        allowed = ACCOUNT_OPERATIONS if account_ops else ACTOR_OPERATIONS
        if not context.get("actor_event") or operation not in allowed:
            raise PermissionError("直播角色只操作台词、表情和表演，不能发布内容或修改账号。")
        from agent_core.store import ActorStore
        if not ActorStore(context["actor_database"]).owns_model_lease(actor_id, context.get("actor_lease_owner", "")):
            raise PermissionError("角色事件租约已结束。")
        if operation == "avatar" and params.get("cue") not in ACTOR_CUES:
            raise PermissionError("观众事件不能触发开播、关播或 OBS 控制。")
        if account_ops:
            params["_account_actor"] = actor_id
    elif not context.get("is_master"):
        raise PermissionError("运营操作仅限主人主代理。")
    if operation not in {"status", "account", "room", "events", "drafts", "draft_show", "receipt", "feed", "search", "video", "comments", "notifications", "operations"}:
        with broker.connect() as db:
            broker._require_active_turn(db, token)
    if operation == "listener":
        params["mode"] = params.pop("listen_mode")
    if operation == "avatar":
        for old, new in (("avatar_emotion", "emotion"), ("motion_group", "group"), ("motion_index", "index")):
            if old in params:
                params[new] = params.pop(old)
    result = execute(path, operation, params)
    if actor_id and not account_ops and operation == "status":
        result = {key: value for key, value in result.items() if key != "drafts"}
    elif actor_id and operation == "events":
        from config import AppConfig
        room_id = AppConfig.load(path).bilibili.room_id
        result = {**result, "events": [e for e in result.get("events", []) if e.get("body", {}).get("room_id") == room_id]}
    return result


async def tick_client(client):
    """Core keeps services alive; Studio/CLI reuse the authenticated endpoints."""
    import asyncio
    import time
    from broadcast_host import ensure_server, execute
    config = client.app_config
    if getattr(client, "_broadcast_check_after", 0) > time.monotonic():
        return
    client._broadcast_check_after = time.monotonic() + 15
    path = str(Path(getattr(client, "config_path", "config.yaml")).resolve())
    if Path(path).is_file():
        from config import AppConfig
        saved = await asyncio.to_thread(AppConfig.load, path)
        config.bilibili, config.vtuber = saved.bilibili, saved.vtuber
        if saved.bilibili.actor_id or saved.bilibili.autonomy.actor_id:
            config.agent = saved.agent
    enabled = getattr(getattr(config, "bilibili", None), "enabled", False) or getattr(getattr(config, "vtuber", None), "enabled", False)
    if not enabled:
        if getattr(client, "_broadcast_signature", ""):
            import contextlib
            path = str(Path(getattr(client, "config_path", "config.yaml")).resolve())
            with contextlib.suppress(Exception):
                await asyncio.to_thread(execute, path, "status", ensure=False)
            client._broadcast_signature = ""
        return
    from broadcast_store import digest
    signature = digest([path, config.bilibili.model_dump(), config.vtuber.model_dump()])
    service = await asyncio.to_thread(ensure_server, path)
    if config.vtuber.enabled:
        from vtuber_host import ensure_server as ensure_avatar
        await asyncio.to_thread(ensure_avatar, config.vtuber)
    if config.bilibili.enabled and config.bilibili.auto_listen and (getattr(client, "_broadcast_signature", "") != signature or service["status"].get("listener") != "running"):
        await asyncio.to_thread(execute, path, "listener", {"mode": "start", "automatic": True})
    client._broadcast_signature = signature
