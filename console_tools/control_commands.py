"""Permission-checked settings and local feedback; no worker can send QQ text."""
from __future__ import annotations

import re
import time
import uuid

from console_tools.files import ConsoleFileError


MASTER_COMMANDS = {"botname", "nickname", "length", "files", "frequency"}
ADMIN_COMMANDS = {"file-size", "bug"}


def authorize(command, context):
    if command in MASTER_COMMANDS and not context.get("is_master"):
        raise PermissionError("此命令仅限配置的主人账号。")
    if command in ADMIN_COMMANDS and not (context.get("is_admin") or context.get("is_master")):
        raise PermissionError("此命令仅限管理员或主人账号。")


def _save(client, section, field, value):
    old = getattr(section, field)
    setattr(section, field, value)
    try:
        client.app_config.save(getattr(client, "config_path", "config.yaml"))
    except BaseException:
        setattr(section, field, old)
        raise
    if section is client.app_config.media:
        client._get_media_service()


def _integer(argument, minimum, maximum, description):
    if not re.fullmatch(r"[0-9]+", argument) or not minimum <= int(argument) <= maximum:
        raise ConsoleFileError("COMMAND_ARGUMENT_INVALID", f"{description}需要 {minimum}–{maximum} 的整数。")
    return int(argument)


def execute_setting(coordinator, command, argument, ctx, metadata):
    client, cfg = coordinator.client, coordinator.client.app_config.bot
    authorize(command, {"is_master": cfg.is_master(ctx.conversation_id if ctx.is_group else "", ctx.sender_id),
        "is_admin": cfg.is_admin(ctx.conversation_id if ctx.is_group else "", ctx.sender_id,
                                 metadata.get("sender_role", ""))})
    if command == "botname":
        if not argument:
            return "机器人名称：" + (cfg.bot_name or client.personas.get_active_persona().name)
        if not 1 <= len(argument) <= 40 or re.search(r"[\x00-\x1f\[\]<>]", argument):
            raise ConsoleFileError("COMMAND_ARGUMENT_INVALID", "机器人名称需要 1–40 个字符，不能包含控制字符或提醒标签。")
        _save(client, cfg, "bot_name", argument)
        client.sync_system_prompt()
        return "机器人名称已修改为：" + argument
    if command == "length":
        if argument:
            _save(client, cfg, "reply_char_limit", _integer(argument, 0, 100000, "字数上限"))
        return (f"普通聊天每轮最多 {cfg.reply_char_limit} 字。" if cfg.reply_char_limit else "普通聊天总字数不设上限。") + "命令完整结果和文件报告保留全文。"
    if command == "files":
        media = client.app_config.media
        if argument:
            _save(client, media, "delivery_batch_size", _integer(argument, 1, 1000, "文件直发数量"))
        return f"每批最多直接发送 {media.delivery_batch_size} 个文件，超出自动打包 ZIP，保留全部成果。"
    if command == "file-size":
        media = client.app_config.media
        if argument:
            match = re.fullmatch(r"([0-9]+)\s*(MB|MiB|兆|GB|GiB|G|吉)?", argument, re.I)
            if not match:
                raise ConsoleFileError("COMMAND_ARGUMENT_INVALID", "文件大小使用整数 MB，例如 /file-size 100MB 或 /文件大小 1GB。")
            value = int(match[1]) * (1024 if (match[2] or "").lower() in ("gb", "gib", "g", "吉") else 1)
            _save(client, media, "delivery_max_file_mb", _integer(str(value), 1, 1024, "文件大小 MB"))
        return f"单个交付文件上限：{media.delivery_max_file_mb} MB。超过上限的文件保留在本地并报告原因。"
    if command == "frequency":
        if argument:
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(?:s|秒)?", argument, re.I)
            if not match or not 0 <= float(match[1]) <= 300:
                raise ConsoleFileError("COMMAND_ARGUMENT_INVALID", "回复间隔使用 0–300 秒，例如 /frequency 5 或 /频次 2秒。")
            _save(client, cfg, "chat_buffer_seconds", float(match[1]))
            runtime = client._get_runtime()
            runtime.buffer_seconds = cfg.chat_buffer_seconds
            for changed in runtime.inbox_changed.values():
                changed.set()
        return f"群聊消息归集与回复间隔：{cfg.chat_buffer_seconds:g} 秒；明确的命令直接排队执行。"
    if command == "nickname":
        if not ctx.is_group:
            raise ConsoleFileError("COMMAND_ARGUMENT_INVALID", "成员昵称在群聊内设置，例如 /昵称 @成员 新称呼。")
        target = None
        match = re.fullmatch(r"(?:\[CQ:at,qq=([^,\]]+)\]|<@!?([^>]+)>|@?([A-Za-z0-9_.+=/-]+))\s+(.{1,40})", argument)
        targets = metadata.get("mentioned_user_ids") or []
        if len(targets) > 1:
            raise ConsoleFileError("COMMAND_ARGUMENT_INVALID", "请一次只指定一位成员。")
        if targets:
            target, name = targets[0], argument.strip()
            if match and (match[1] or match[2] or match[3] == target):
                selected = next(value for value in match.groups()[:3] if value is not None)
                if selected != target:
                    raise ConsoleFileError("MENTION_TARGET_INVALID", "正文目标与原生 @ 的成员不一致。")
                name = match[4].strip()
            elif name.startswith("@") and " " in name:
                name = name.split(maxsplit=1)[1].strip()
        elif match:
            target, name = next(value for value in match.groups()[:3] if value is not None), match[4].strip()
        else:
            raise ConsoleFileError("COMMAND_ARGUMENT_INVALID", "请指定一位成员和新称呼，例如 /nickname @成员 小明。")
        known = {row["sender_id"] for row in coordinator.broker.messages(metadata["command_token"], 100)}
        known.update(metadata.get("mentioned_user_ids") or [])
        if target not in known or target in ("all", metadata.get("bot_id")):
            raise ConsoleFileError("MENTION_TARGET_INVALID", "目标必须是当前群已确认的成员 ID 或原生 @成员。")
        if not 1 <= len(name) <= 40 or re.search(r"[\x00-\x1f\[\]<>]", name):
            raise ConsoleFileError("COMMAND_ARGUMENT_INVALID", "称呼需要 1–40 个字符，不能包含提醒标签或控制字符。")
        client.memory_mgr.set_participant_nickname(ctx.conversation_id, target, name)
        from bot_identity import set_name
        set_name(client, metadata["session_key"], ctx.scope, target, name, ctx.message_id)
        if hasattr(client, "_cache_member_name"):
            client._cache_member_name(ctx.conversation_id, target, name)
        return "已记录该成员的称呼：" + name
    if command in ("bug", "sug"):
        if not argument or len(argument) > 10000:
            raise ConsoleFileError("COMMAND_ARGUMENT_INVALID", "请提供 1–10000 字的问题或建议内容。")
        namespace = metadata["namespace"]
        with coordinator.broker.connect() as db:
            db.execute("INSERT OR IGNORE INTO feedback VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex[:16],
                namespace, ctx.scope, ctx.sender_id, ctx.message_id, command, argument, time.time()))
            row = db.execute("SELECT id FROM feedback WHERE namespace=? AND scope=? AND source_message_id=? AND kind=?",
                             (namespace, ctx.scope, ctx.message_id, command)).fetchone()
        return ("问题" if command == "bug" else "建议") + "已记录到本地反馈库，编号：" + row["id"]
    raise ValueError("命令尚未实现。")
