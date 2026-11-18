"""Refresh saved voice settings without discarding the conversation that owns a reply."""
from __future__ import annotations

import hashlib
from functools import lru_cache
import json
import os
from pathlib import Path
import time
import uuid


def voice_revision(config):
    return hashlib.sha256(json.dumps(config.model_dump(mode="json"), sort_keys=True,
        ensure_ascii=False).encode("utf-8")).hexdigest()


def reload_config(client, *, force=False):
    """Called by the control watcher; accept replacements and backwards timestamps too."""
    path = Path(getattr(client, "config_path", "config.yaml")).resolve()
    if not path.is_file():
        return False
    stat = path.stat()
    signature = (str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
    previous = getattr(client, "_saved_config_signature", None)
    if not force and previous == signature:
        return False
    # The constructor has already loaded its config. Establish its baseline once.
    if previous is not None or force or hasattr(client, "_config_mtime"):
        from config import AppConfig
        fresh = AppConfig.load(str(path))
        client.app_config.bot = fresh.bot
        client.app_config.agent = fresh.agent
        client.app_config.voice = fresh.voice
    client._saved_config_signature = signature
    config = client.app_config.voice
    record = {"pid": os.getpid(), "config_path": str(path), "applied_at": time.time(),
              "revision": voice_revision(config), "character": config.character,
              "enabled": config.enabled, "language": config.default_language}
    target = path.parent / "data/voice/applied.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        # A local status receipt is advisory; never fail an otherwise valid reply.
        pass
    finally:
        temporary.unlink(missing_ok=True)
    return previous is not None or force


def current_voice(client, fallback=None):
    """A queued agent reply must use the saved character at synthesis time."""
    from voice_config import VoiceConfig
    if fallback and not hasattr(client, "_saved_config_signature") and not getattr(client, "config_path", None):
        return VoiceConfig.model_validate(fallback)
    path = Path(getattr(client, "config_path", "config.yaml"))
    if path.is_file():
        reload_config(client, force=not hasattr(client, "_saved_config_signature"))
    value = getattr(getattr(client, "app_config", None), "voice", None)
    return value.model_copy(deep=True) if value is not None else VoiceConfig.model_validate(fallback or {})


def applied_status(home, config, pid=None):
    try:
        data = json.loads((Path(home) / "data/voice/applied.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    confirmed = bool(pid and data.get("pid") == pid and data.get("revision") == voice_revision(config))
    return {"state": "applied" if confirmed else "pending" if pid else "stopped",
            "saved_character": config.character, "enabled": config.enabled,
            "character": data.get("character") if pid and data.get("pid") == pid else None,
            "applied_at": data.get("applied_at") if confirmed else None}


def refresh_binding(context):
    path = context.get("voice_config_path")
    if path and Path(path).is_file():
        from voice_service import voice_context
        resolved = Path(path).resolve()
        stat = resolved.stat()
        signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        config = _saved_voice(str(resolved), signature)
        context["voice_config"] = config.model_dump()
        context["voice"] = voice_context(config)
    return context


@lru_cache(maxsize=32)
def _saved_voice(path, signature):
    from config import AppConfig
    return AppConfig.load(path, create=False).voice
