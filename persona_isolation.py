"""Account/persona identities shared by chat sessions and memory storage."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger("persona_isolation")

_bound = ContextVar("qqbot_persona", default=None)
MARKER = "::persona:"


def base_session(key):
    return str(key).split(MARKER, 1)[0]


def context_persona(context):
    return str(context.get("session_key", "")).partition(MARKER)[2]


def session_group(key):
    key = base_session(key).removeprefix("__work__:")
    return key.split("group_", 1)[1] if key.startswith(("group_", "onebot_group_")) else ""


def namespace(client):
    return (f"onebot:{getattr(client, 'account_key', getattr(client, 'self_id', 'default'))}" if hasattr(client, "send_action")
            else f"official:{getattr(getattr(client, 'app_config', None), 'qq', None) and client.app_config.qq.appid or 'default'}")



def identity(client, persona):
    pid = getattr(persona, "id", "") or getattr(persona, "name", "")
    return hashlib.sha256((namespace(client) + "\n" + pid).encode()).hexdigest()[:24]


def effective_persona(client, group=""):
    return (client.personas.get_persona(client.app_config.bot.get_persona_name(str(group)))
            or client.personas.get_active_persona())


def persona_for(client, group="", key=""):
    bound = _bound.get()
    if bound and bound[0] is client:
        if (MARKER in key and scoped_session(client, base_session(key), bound[2]) == key
                or MARKER not in key and str(session_group(key) if key else group) == bound[1]):
            return bound[2]
    if MARKER in key:
        wanted = key.split(MARKER, 1)[1]
        for preset in client.personas.presets.values():
            if identity(client, preset) == wanted:
                return preset
            legacy_identity = hashlib.sha256((namespace(client) + "\n" + preset.name).encode()).hexdigest()[:24]
            if legacy_identity == wanted:
                return preset
        raise ValueError("该会话所属人设已不存在，不能载入其他人设。")
    return effective_persona(client, group)


def scoped_session(client, key, persona=None):
    if MARKER in key:
        return key
    return key + MARKER + identity(client, persona or persona_for(client, session_group(key)))


def session_is_active(client, key):
    return (MARKER in key and key == scoped_session(client, base_session(key),
                                                   effective_persona(client, session_group(key))))


@contextmanager
def persona_scope(client, group="", *, key="", persona=None):
    selected = persona or persona_for(client, group, key)
    token = _bound.set((client, str(group), selected.model_copy(deep=True)))
    try:
        yield selected
    finally:
        _bound.reset(token)


def persona_bound(kind):
    """Capture one preset for the entire async handler, including to_thread."""
    def decorate(function):
        @wraps(function)
        async def wrapped(self, *args, **kwargs):
            item = args[0] if args else kwargs.get("session_key" if kind == "core" else
                "group" if kind == "proactive" else "data", kwargs.get("message"))
            client = self.client if kind == "proactive" else self
            client.sync_system_prompt()
            key = item if kind == "core" else ""
            group = (str(item) if kind == "proactive" else session_group(key) if key else
                     str(item.get("group_id", "")) if isinstance(item, dict) else
                     str(getattr(item, "group_openid", "")))
            from conversation_runtime import current_event
            event = current_event.get()
            preset = getattr(event, "persona", None) if event else None
            with persona_scope(client, group, key=key, persona=preset):
                return await function(self, *args, **kwargs)
        return wrapped
    return decorate


class PersonaMemory:
    """Route the existing memory API to separate account/persona directories.

    Legacy files stay in the original directory and are never auto-imported.
    """
    def __init__(self, client, original):
        self.client = client
        self.managers = {}
        while isinstance(original, PersonaMemory):
            if "managers" in original.__dict__:
                self.managers.update(original.managers)
            original = original.original
        self.original = original
        self.storage_dir = getattr(original, "storage_dir", "data/memories")
        self.max_entries = getattr(original, "max_entries", 2000)

    def manager(self, group="", *, persona=None):
        from group_memory import GroupMemoryManager
        preset = persona or persona_for(self.client, str(group))
        key = identity(self.client, preset)
        if key not in self.managers:
            root = Path(self.storage_dir) / "profiles" / key
            old_name_key = hashlib.sha256((namespace(self.client) + "\n" + preset.name).encode()).hexdigest()[:24]
            old_root = Path(self.storage_dir) / "profiles" / old_name_key
            if not root.exists() and old_root.exists() and old_name_key != key:
                try:
                    old_root.rename(root)
                    logger.info("Migrated persona profile directory from %s to %s", old_name_key, key)
                except Exception as e:
                    logger.warning("Failed to migrate persona profile directory: %s", e)
            manager = GroupMemoryManager(str(root), self.max_entries)
            manager.session_namespace = MARKER + key
            from bot_identity import archive_ready
            manager.archive_observer = lambda group, path, owner=key: archive_ready(self.client, owner, group, path)
            self.managers[key] = manager
            (root / "profile.json").write_text(json.dumps({"account": namespace(self.client),
                "persona": preset.name, "persona_id": getattr(preset, "id", ""), "identity": key}, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.managers[key]

    @property
    def _session_messages(self):
        self._load_existing_managers()
        return {f"{key}:{group}": rows for key, manager in self.managers.items()
                for group, rows in manager._session_messages.items()}

    async def summarize_and_save_session(self, *args, **kwargs):
        self._load_existing_managers()
        saved = {}
        for key, manager in list(self.managers.items()):
            saved.update({f"{key}:{group}": path for group, path in
                          (await manager.summarize_and_save_session(*args, **kwargs)).items()})
        return saved

    def has_pending(self):
        self._load_existing_managers()
        return any(manager.has_pending() for manager in self.managers.values())

    def close_run(self):
        self._load_existing_managers()
        for manager in list(self.managers.values()):
            manager.close_run()

    def _load_existing_managers(self):
        # Startup and timers must see durable work before a preset receives its
        # first message. Derive paths from this account's configured identities;
        # never sweep unrelated profiles or import the mixed legacy root.
        profiles = Path(self.storage_dir) / "profiles"
        for preset in self.client.personas.presets.values():
            key = identity(self.client, preset)
            old_key = hashlib.sha256((namespace(self.client) + "\n" + preset.name).encode()).hexdigest()[:24]
            if key not in self.managers and ((profiles / key).is_dir() or (profiles / old_key).is_dir()):
                self.manager(persona=preset)

    def _all_managers(self):
        # Only this bot account's presets; do not clear another account's data.
        return [self.manager(persona=p) for p in self.client.personas.presets.values()]

    def clear_all_memories(self):
        count = sum(manager.clear_all_memories() for manager in self._all_managers())
        from bot_identity import store_for
        for preset in self.client.personas.presets.values():
            store_for(self.client).clear(identity(self.client, preset))
        return count

    def get_all_memories_stat(self):
        return [dict(row, persona=p.name) for p in self.client.personas.presets.values()
                for row in self.manager(persona=p).get_all_memories_stat()]

    def preload_recent_memories(self, *args, **kwargs):
        return sum(m.preload_recent_memories(*args, **kwargs) for m in self._all_managers())

    def __getattr__(self, name):
        if hasattr(self.original, name):
            attr = getattr(self.original, name)
            if not callable(attr):
                return attr
        def call(*args, **kwargs):
            group = args[0] if args else kwargs.get("group_openid", kwargs.get("group_id", ""))
            if name == "save_group_memory":
                group = group.group_openid
            result = getattr(self.manager(group), name)(*args, **kwargs)
            if name in ("set_participant_nickname", "record_session_message", "clear_group_memory", "clear_memory"):
                from bot_identity import store_for, scope_for
                store = store_for(self.client)
                owner, scope = identity(self.client, persona_for(self.client, str(group))), scope_for(self.client, group)
                if name in ("clear_group_memory", "clear_memory"):
                    store.clear(owner, scope)
                elif name == "set_participant_nickname":
                    user = str(args[1] if len(args) > 1 else kwargs["user_openid"])
                    label = args[2] if len(args) > 2 else kwargs["nickname"]
                    store.observe(owner, scope, user, label)
                elif not (args[4] if len(args) > 4 else kwargs.get("is_bot", False)):
                    user = args[1] if len(args) > 1 else kwargs.get("author_id", "")
                    label = args[2] if len(args) > 2 else kwargs.get("author_name", "")
                    store.observe(owner, scope, user, label)
            return result
        return call


class PersonaBuffer:
    def __init__(self, client, original):
        self.client = client
        self.buffers = {}
        while isinstance(original, PersonaBuffer):
            if "buffers" in original.__dict__:
                self.buffers.update(original.buffers)
            original = original.original
        self.original = original

    @property
    def max_buffer_size(self) -> int:
        orig = getattr(self, "original", None)
        size = getattr(orig, "max_buffer_size", 50)
        if callable(size):
            size = 50
        try:
            return int(size)
        except Exception:
            return 50

    @property
    def _buffers(self):
        merged = {}
        for buf in self.buffers.values():
            if hasattr(buf, "_buffers"):
                merged.update(buf._buffers)
        return merged

    def buffer(self, group=""):
        from group_memory import GroupContextBuffer
        key = identity(self.client, persona_for(self.client, str(group)))
        if key not in self.buffers:
            size = getattr(self.original, "max_buffer_size", 50)
            if callable(size):
                size = 50
            try:
                size = int(size)
            except Exception:
                size = 50
            self.buffers[key] = GroupContextBuffer(size)
        return self.buffers[key]

    def clear(self, group_openid=None):
        if group_openid is None:
            for buffer in self.buffers.values():
                buffer.clear()
        else:
            self.buffer(group_openid).clear(group_openid)

    def __getattr__(self, name):
        if hasattr(self.original, name):
            attr = getattr(self.original, name)
            if not callable(attr):
                return attr
        def call(*args, **kwargs):
            group = args[0] if args else kwargs.get("group_openid", kwargs.get("group_id", ""))
            return getattr(self.buffer(group), name)(*args, **kwargs)
        return call
