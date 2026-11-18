"""Explicit, account-local conversation sharing; transport grants stay at source."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BASE_RE = re.compile(r"(?:onebot_(?:group|private)|group|c2c)_[A-Za-z0-9_-]{1,160}\Z")


class ConversationRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="", max_length=80)
    mode: Literal["independent", "merge", "follow"] = "independent"
    persona_id: str = Field(default="", max_length=160)
    target: str = Field(default="", max_length=200)
    history_persona_id: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def check_mode(self):
        if self.mode == "independent":
            if self.target:
                raise ValueError("独立对话不能指定主对话。")
        elif not BASE_RE.fullmatch(self.target) or self.persona_id:
            raise ValueError("共享模式需要有效的主对话，并沿用主对话的人设。")
        return self


def root_for(mapping, source):
    visited = set()
    current = source
    while current in mapping and mapping[current].mode != "independent":
        if current in visited or len(visited) >= 64:
            raise ValueError("对话关联存在循环或链条超过 64 层。")
        visited.add(current)
        current = mapping[current].target
    return current


def validate_routes(value):
    for account, mapping in value.items():
        if not re.fullmatch(r"(?:onebot|official):[A-Za-z0-9_.-]{1,160}", account):
            raise ValueError("对话设置需要有效的账号标识。")
        for source, rule in mapping.items():
            for key in (source, rule.target) if rule.target else (source,):
                if (not BASE_RE.fullmatch(key)
                        or key.startswith("onebot_") != account.startswith("onebot:")):
                    raise ValueError("对话标识与账号通道不匹配，不能跨账号关联。")
            root_for(mapping, source)
    return value


def routes(client):
    from persona_isolation import namespace
    return getattr(client.app_config.bot, "conversations", {}).get(namespace(client), {})


def source_base(client, conversation_id, is_group):
    if hasattr(client, "send_action"):
        return "onebot_" + ("group_" if is_group else "private_") + str(conversation_id)
    return ("group_" if is_group else "c2c_") + str(conversation_id)


def canonical_base(client, key):
    from persona_isolation import base_session
    prefix = "__work__:" if str(key).startswith("__work__:") else ""
    base = base_session(key).removeprefix("__work__:")
    return prefix + root_for(routes(client), base)


def local_persona(client, base):
    from persona_isolation import session_group
    rule = routes(client).get(base)
    if rule and rule.persona_id:
        for preset in client.personas.presets.values():
            if preset.id == rule.persona_id:
                return preset
        raise ValueError("此对话绑定的人设已不存在，请重新选择。")
    return (client.personas.get_persona(client.app_config.bot.get_persona_name(session_group(base)))
            or client.personas.get_active_persona())


def routed_persona(client, key):
    return local_persona(client, canonical_base(client, key).removeprefix("__work__:"))


def binding(client, key):
    base = str(key).split("::persona:", 1)[0].removeprefix("__work__:")
    root = canonical_base(client, base)
    rule = routes(client).get(base)
    members = sorted({root, *(item for item in routes(client) if canonical_base(client, item) == root)})
    return {"source": base, "primary": root, "mode": rule.mode if rule else "independent",
            "shared": len(members) > 1, "members": members}


def source_binding(client, ctx):
    return binding(client, source_base(client, ctx.conversation_id, ctx.is_group))


def background_allowed(client, ctx):
    return source_binding(client, ctx)["mode"] != "follow"


def source_turn_state(state, name, scope, *, shared=False):
    value = state.get(name + "_by_scope", {}).get(scope)
    if value is not None:
        return value
    latest = state.get(name)
    return latest if latest and (not shared or latest.get("scope") == scope) else None


def remember_turn_state(state, name, scope, value):
    value = {**value, "scope": scope}
    state[name] = value
    state.setdefault(name + "_by_scope", {})[scope] = value


def shared_memory_manifest(coordinator, key, manifest=None):
    """Reference selected histories once; preserve primary-session read caching."""
    client = coordinator.client
    route = binding(client, key)
    if not route["shared"]:
        return manifest
    from idle_memory import IdleMemory
    from persona_isolation import MARKER, identity, session_group
    root_persona = routed_persona(client, route["primary"])
    files = list((manifest or {}).get("files", []))
    for member in route["members"]:
        rule = routes(client).get(member)
        historical = next((p for p in client.personas.presets.values()
                           if rule and p.id == rule.history_persona_id), None)
        for persona in {p.id: p for p in (historical, local_persona(client, member), root_persona) if p}.values():
            owner = identity(client, persona)
            old_key = member + MARKER + owner
            group = session_group(member)
            profile = Path(client.memory_mgr.storage_dir) / "profiles" / owner
            if group and profile.is_dir() and hasattr(client.memory_mgr, "manager"):
                manager = client.memory_mgr.manager(group, persona=persona)
                previous = manager.get_memory_file_manifest(group, history_count=client.app_config.bot.memory_history_count)
                files.extend({**item, "source_conversation": member} for item in previous["files"])
            directory = coordinator.root / hashlib.sha256((coordinator.namespace + ":" + old_key).encode()).hexdigest()[:24]
            old_session = client.ai._sessions.get(old_key)
            generation = old_session.generation if old_session else 0
            memory = IdleMemory(directory, generation)
            try:
                reference = memory.reference()
                if reference and old_key != key:
                    files.append({**reference, "source_conversation": member})
                if memory.journal.is_file() and old_key != key:
                    stat = memory.journal.stat()
                    files.append({"role": "archive_messages", "path": str(memory.journal),
                                  "version": f"{stat.st_mtime_ns}:{stat.st_size}", "source_conversation": member})
            except (OSError, ValueError):
                continue
    unique = {str(Path(item["path"]).resolve()).casefold(): item for item in files}
    files = sorted(unique.values(), key=lambda item: str(item["path"]).casefold())
    return {**(manifest or {}), "group_openid": session_group(route["primary"]) or route["primary"],
            "files": files, "shared_conversations": route["members"],
            "fingerprint": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:20]}
