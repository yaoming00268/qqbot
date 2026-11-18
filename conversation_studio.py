"""Local Studio discovery and revision-checked conversation settings."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from conversation_routing import BASE_RE, ConversationRoute, local_persona, root_for, routed_persona


def accounts(config):
    return [{"id": "onebot:" + key, "label": item.name or key, "transport": "onebot"}
            for key, item in config.onebot.accounts.items()] + [
                {"id": "official:" + (config.qq.appid or "default"), "label": "QQ 官方通道", "transport": "official"}]


def client_for(config, personas, account):
    if account not in {item["id"] for item in accounts(config)}:
        raise ValueError("账号不存在，请重新载入。")
    client = SimpleNamespace(app_config=config, personas=personas)
    if account.startswith("onebot:"):
        client.send_action = None
        client.account_key = account.split(":", 1)[1]
    return client


def catalog(home, config, personas):
    home = Path(home)
    known = {item["id"]: set(config.bot.conversations.get(item["id"], {})) for item in accounts(config)}
    errors = []
    for account in known:
        prefix = "onebot_group_" if account.startswith("onebot:") else "group_"
        known[account].update(prefix + str(group) for group in config.bot.group_settings)
        name = "sessions_" + account.split(":", 1)[1] + ".json" if account.startswith("onebot:") else "sessions.json"
        path = home / "data" / name
        try:
            if path.is_file():
                sessions = json.loads(path.read_text(encoding="utf-8"))
                for key in sessions if isinstance(sessions, dict) else ():
                    base = key.split("::persona:", 1)[0]
                    if BASE_RE.fullmatch(base) and base.startswith("onebot_") == account.startswith("onebot:"):
                        known[account].add(base)
        except (OSError, ValueError):
            errors.append("部分历史会话索引不可读，可手动添加对话。")
    runtime = Path(config.bot.runtime_database_path)
    runtime = runtime if runtime.is_absolute() else home / runtime
    database = runtime.resolve().parent / "main_chat/console.sqlite3"
    if database.is_file():
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                for account, group, target in db.execute(
                    "SELECT DISTINCT namespace,json_extract(payload,'$.is_group'),"
                    "json_extract(payload,'$.conversation_id') FROM contexts "
                    "WHERE json_extract(payload,'$.conversation_id') IS NOT NULL"):
                    if account in known:
                        prefix = ("onebot_group_" if group else "onebot_private_") if account.startswith("onebot:") else ("group_" if group else "c2c_")
                        base = prefix + str(target)
                        if BASE_RE.fullmatch(base):
                            known[account].add(base)
        except sqlite3.Error:
            errors.append("部分本机对话记录不可读，可手动添加对话。")
    rows = []
    for account, keys in known.items():
        mapping = config.bot.conversations.get(account, {})
        for rule in mapping.values():
            if rule.target:
                keys.add(rule.target)
        client = client_for(config, personas, account)
        for source in sorted(keys):
            rule = mapping.get(source, ConversationRoute())
            root = root_for(mapping, source)
            try:
                persona = routed_persona(client, source)
                name, effective_id, error = persona.name, persona.id, ""
            except ValueError as problem:
                name, effective_id, error = "人设已删除，请重新选择", "", str(problem)
            rows.append({"account": account, "source": source, **rule.model_dump(exclude={"history_persona_id"}),
                         "separate_persona_id": rule.history_persona_id or rule.persona_id,
                         "primary": root, "effective_persona": name, "effective_persona_id": effective_id,
                         "is_group": source.startswith(("onebot_group_", "group_")), "error": error})
    return {"accounts": accounts(config), "items": rows, "errors": sorted(set(errors)),
            "conversations": len(rows), "contexts": len({(item["account"], item["primary"]) for item in rows})}


def update(config, personas, values, *, remove=False):
    allowed = {"account", "source", "label", "mode", "persona_id", "target", "revision"}
    if set(values) - allowed:
        raise ValueError("对话设置包含未知字段。")
    account, source = str(values.get("account", "")), str(values.get("source", ""))
    client = client_for(config, personas, account)
    if not BASE_RE.fullmatch(source) or source.startswith("onebot_") != account.startswith("onebot:"):
        raise ValueError("请选择有效的群聊或私聊标识。")
    mapping = config.bot.conversations.setdefault(account, {})
    if remove:
        mapping.pop(source, None)
    else:
        old = mapping.get(source)
        historical = old.history_persona_id if old and values.get("mode", "independent") != "independent" else ""
        if values.get("mode", "independent") != "independent" and not historical:
            try:
                historical = routed_persona(client, source).id
            except ValueError:
                historical = ""
        new = ConversationRoute.model_validate({key: values[key] for key in (
            "label", "mode", "persona_id", "target") if key in values} | {"history_persona_id": historical})
        if new.persona_id and not any(item.id == new.persona_id for item in personas.presets.values()):
            raise ValueError("所选人设不存在，请重新载入。")
        mapping[source] = new
    from conversation_routing import validate_routes
    validate_routes(config.bot.conversations)
    return config.bot.conversations
