"""Structured account tools and durable autonomous actor wakeups."""
from __future__ import annotations

import asyncio
import re
import time

from broadcast_store import digest

READ_OPERATIONS = {"feed", "search", "video", "comments", "notifications", "operations"}
WRITE_OPERATIONS = {"comment", "like", "profile_update"}
ACCOUNT_OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS | {"status", "account", "drafts", "draft_show", "draft", "publish", "receipt"}


def account_capabilities(config):
    """Native operations, not generic browser recipe permissions."""
    operations = READ_OPERATIONS | {"status", "account", "drafts", "draft_show", "draft", "receipt"}
    if config.allow_publish:
        cfg = config.autonomy
        operations |= {op for op, allowed in (("comment", cfg.allow_comments), ("like", cfg.allow_likes),
                       ("profile_update", cfg.allow_profile_update), ("publish", cfg.allow_dynamics or cfg.allow_video_uploads)) if allowed}
    return {"adapter": "broadcast", "actions": sorted(operations), "tool": {"action": "broadcast"},
            "instruction": "B站使用 action=broadcast 的原生 operation；draft 保存本机动态或视频草稿，不需要外发权限。不要修改网页账号 actions。"}


def bounded_int(value, name, low=1, high=10000000000000000000):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(name + " 超出允许范围。")
    return value


def bvid(value):
    if not isinstance(value, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", value):
        raise ValueError("请输入完整的 BV 视频编号。")
    return value


def text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"正文需为 1–{maximum} 字。")
    return value.strip()


def resource(params):
    kind = params.get("resource_type", "video")
    if kind not in {"video", "dynamic"}:
        raise ValueError("仅支持视频或动态评论。")
    return bounded_int(params.get("oid"), "资源 ID"), kind


def quotas(config, kind, target, body=""):
    return {"hourly": config.max_actions_per_hour, "interval": config.min_write_interval_seconds,
            "daily_comments": config.max_comments_per_day, "thread_replies": config.max_thread_replies,
            "kind": kind, "target": target,
            "text_hash": digest("".join(body.split()).casefold()) if body else ""}


def require_actor(service, actor_id):
    cfg = service.config.autonomy
    actor = service.app_config.agent.actors.get(actor_id)
    if (not service.config.enabled or not cfg.enabled or cfg.actor_id != actor_id
            or not service.app_config.agent.enabled or not actor or not actor.enabled):
        raise PermissionError("账号自主运营授权已关闭或绑定角色已变更。")


async def read(service, operation, params):
    if not service.config.enabled:
        raise PermissionError("请先启用 B站运营服务。")
    limit = bounded_int(params.get("limit", 10), "每页数量", high=20)
    page = bounded_int(params.get("page", 1), "页码", high=100)
    async with asyncio.timeout(min(60, service.config.request_timeout_seconds)):
        if operation == "feed":
            result = await service.adapter.feed(limit)
        elif operation == "search":
            result = await service.adapter.search(text(params.get("query"), 100), page, limit)
        elif operation == "video":
            result = await service.adapter.video_info(bvid(params.get("bvid")))
        elif operation == "comments":
            oid, kind = resource(params)
            root = bounded_int(params.get("root", 0), "根评论 ID", low=0)
            result = await service.adapter.comments(oid, kind, page, limit, root)
        elif operation == "notifications":
            result = await service.adapter.notifications(limit)
        elif operation == "operations":
            return {"operations": service.store.operations(limit)}
        else:
            raise ValueError("未知的 B站读取操作。")
    return {**result, "untrusted_content": True}


async def write(service, operation, params):
    if not service.config.enabled or not service.config.allow_publish:
        raise PermissionError("请先开启 B站运营及实际外发。")
    cfg = service.config.autonomy
    p = {key: value for key, value in params.items() if not key.startswith("_")}
    if operation == "comment":
        if not cfg.allow_comments:
            raise PermissionError("评论与回复权限未开启。")
        oid, kind = resource(p)
        p = {"oid": oid, "resource_type": kind, "text": text(p.get("text"), 1000),
             "root": bounded_int(p.get("root", 0), "根评论 ID", low=0),
             "parent": bounded_int(p.get("parent", 0), "回复目标 ID", low=0)}
        if p["parent"] and not p["root"]:
            raise ValueError("回复楼中楼需要同时填写 root 与 parent。")
        target = f"{kind}:{oid}:{p['root']}"
    elif operation == "like":
        if not cfg.allow_likes:
            raise PermissionError("点赞权限未开启。")
        if type(p.get("active", True)) is not bool:
            raise ValueError("active 必须为布尔值。")
        p = {"bvid": bvid(p.get("bvid")), "active": p.get("active", True)}
        target = p["bvid"]
    elif operation == "profile_update":
        if not cfg.allow_profile_update:
            raise PermissionError("个性签名更新权限未开启。")
        p = {"text": text(p.get("text"), 70)}
        target = "self"
    else:
        raise ValueError("未知的 B站写入操作。")
    service.adapter.credential()
    key = params.get("request_key", "")
    limits = quotas(cfg, operation, target, p.get("text", ""))
    if not service.store.claim(operation, p, key, limits=limits):
        return service.store.receipt(key)
    try:
        async with asyncio.timeout(service.config.request_timeout_seconds):
            if operation == "comment":
                result = await service.adapter.send_comment(**p)
            elif operation == "like":
                result = await service.adapter.like_video(p["bvid"], p["active"])
            else:
                result = await service.adapter.update_signature(p["text"])
        return service.store.complete(key, "accepted", result=result)
    except asyncio.CancelledError:
        service.store.complete(key, "unknown", error="调用中断，请核对平台；不会自动重发。")
        raise
    except Exception:
        return service.store.complete(key, "unknown", error="未取得平台回执；请核对平台，禁止自动重发。")


def schedule(service, *, force=False):
    cfg = service.config.autonomy
    require_actor(service, cfg.actor_id)
    from agent_core.store import ActorStore
    path = service.app_config.agent.database_path or str(service.path(service.app_config.bot.runtime_database_path).parent / "actors.sqlite3")
    store = ActorStore(service.path(path))
    try:
        store.actor(cfg.actor_id)
    except ValueError:
        return {"scheduled": False, "reason": "请先启动 Bot 核心初始化运营角色。"}
    account_id = "bilibili:account:operator"
    with store.connect() as db:
        pending = db.execute("SELECT 1 FROM actor_events WHERE actor_id=? AND account_id=? AND state IN ('pending','processing') LIMIT 1",
                             (cfg.actor_id, account_id)).fetchone()
    if pending:
        return {"scheduled": False, "reason": "上轮活动仍在队列或处理中。"}
    if force and not service.store.schedule_due("autonomy-manual:" + cfg.actor_id, 60):
        return {"scheduled": False, "reason": "手动执行请间隔至少一分钟。"}
    if not service.store.schedule_due("autonomy:" + cfg.actor_id, cfg.interval_minutes * 60, force=force):
        return {"scheduled": False, "reason": "尚未到下一次活动时间。"}
    store.upsert_account(account_id, cfg.actor_id, "bilibili", account_capabilities(service.config), True)
    event = store.ingest(cfg.actor_id, account_id, "bilibili.account_cycle",
        {"source": "local_scheduler", "goal": cfg.goal, "interests": cfg.interests,
         "promotion_text": cfg.promotion_text, "untrusted_content": False},
        source_id="bilibili-cycle:" + str(time.time_ns()), scope=account_id)
    return {"scheduled": True, "actor_id": cfg.actor_id, "event_id": event}
