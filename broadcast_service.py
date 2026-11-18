"""Bilibili drafts/listener and the agent -> TTS -> Live2D/OBS route."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator
from bilibili_adapter import BilibiliAdapter
from broadcast_store import BroadcastStore, digest


class DraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="", max_length=80)
    text: str = Field(default="", max_length=16000)
    tags: list[str] = Field(default_factory=list, max_length=10)
    video_path: str = ""
    cover_path: str = ""
    category_id: int = Field(default=0, ge=0)
    copyright: int = Field(default=1, ge=1, le=2)
    source: str = Field(default="", max_length=2000)
    video_sha256: str = ""
    cover_sha256: str = ""

    @model_validator(mode="after")
    def tag_values(self):
        if any(not t.strip() or "," in t or len(t) > 20 for t in self.tags):
            raise ValueError("标签需为1–20字，不能包含英文逗号，最多10项。")
        return self


ACTOR_OPERATIONS = {"status", "events", "speak", "perform", "avatar"}
ACTOR_CUES = {"text", "emotion", "motion", "expression", "reset", "speech_stop", "vts.hotkey", "vts.expression"}
AVATAR_CUES = ACTOR_CUES | {"vts.connect", "vts.disconnect", "vts.models", "vts.hotkeys", "vts.expressions",
    "obs.connect", "obs.disconnect", "obs.status", "obs.scenes", "obs.inputs", "obs.scene", "obs.browser_source",
    "obs.audio_monitor", "obs.start_stream", "obs.stop_stream", "obs.start_record", "obs.stop_record",
    "obs.virtualcam_status", "obs.virtualcam_start", "obs.virtualcam_stop"}


def normalize_live_event(packet, room_id):
    """Discard protocol internals. Audience text is untrusted event data."""
    data = packet.get("data", {})
    command = str(packet.get("type") or data.get("cmd", "")).split(":")[0]
    if command == "DANMU_MSG":
        info = data.get("info", [])
        if len(info) < 3 or not isinstance(info[2], list) or len(info[2]) < 2:
            return None
        header = info[0] if isinstance(info[0], list) else []
        event = {"kind": "chat", "room_id": room_id, "text": str(info[1])[:2000],
                 "uid": str(info[2][0]), "name": str(info[2][1])[:100],
                 "timestamp": header[4] if len(header) > 4 else packet.get("timestamp", time.time())}
    else:
        details = data.get("data", {})
        if command not in {"SEND_GIFT", "GUARD_BUY", "SUPER_CHAT_MESSAGE", "INTERACT_WORD"} or not isinstance(details, dict):
            return None
        event = {"kind": {"SEND_GIFT": "gift", "GUARD_BUY": "guard", "SUPER_CHAT_MESSAGE": "super_chat", "INTERACT_WORD": "interaction"}[command],
                 "room_id": room_id, "uid": str(details.get("uid", "")), "name": str(details.get("uname", details.get("username", "")))[:100],
                 "text": str(details.get("message", details.get("giftName", "")))[:2000],
                 "count": details.get("num", 1), "timestamp": details.get("timestamp", details.get("start_time", packet.get("timestamp", time.time())))}
    event["event_key"] = "bili:" + digest(event)
    return event


class BroadcastService:
    def __init__(self, config_path, *, adapter=None):
        from config import AppConfig
        self.config_path = Path(config_path).resolve()
        self.home = self.config_path.parent
        self.app_config = AppConfig.load(str(self.config_path))
        self.config = self.app_config.bilibili
        self.store = BroadcastStore(self.path(self.config.database_path))
        self.adapter = adapter or BilibiliAdapter(self.path(self.config.credentials_file))
        self.listener_task = self.flush_task = None
        self.last_error = ""
        self._manually_stopped = False
        self._listener_room = self.config.room_id
        self.speech_lock = asyncio.Lock()
        self._last_flush = time.monotonic()
        self._autonomy_error = ""

    def path(self, value):
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.home / path).resolve()

    def refresh(self):
        from config import AppConfig
        latest = AppConfig.load(str(self.config_path))
        if self.path(latest.bilibili.database_path) != self.store.path:
            raise ValueError("修改运营数据库路径后需重启本机运营服务。")
        changed = any(getattr(self.config, key) != getattr(latest.bilibili, key) for key in ("enabled", "room_id", "actor_id", "auto_reply", "auto_listen", "credentials_file"))
        if self.config.auto_listen != latest.bilibili.auto_listen or self.config.enabled != latest.bilibili.enabled:
            self._manually_stopped = False
        self.app_config, self.config = latest, latest.bilibili
        if isinstance(self.adapter, BilibiliAdapter):
            self.adapter.credentials_file = self.path(self.config.credentials_file)
        return changed

    def status(self):
        from broadcast_tts import broadcast_voice_context
        voice = broadcast_voice_context(self.app_config.broadcast_voice)
        from voice_engine import backend_endpoint
        voice["shared_backend_with_qq"] = backend_endpoint(self.app_config.broadcast_voice) == backend_endpoint(self.app_config.voice)
        return {"enabled": self.config.enabled, **self.adapter.status(), "room_id": self.config.room_id,
                "listener": "running" if self.listener_task and not self.listener_task.done() else "stopped",
                "auto_reply": self.config.auto_reply, "actor_id": self.config.actor_id,
                "allow_publish": self.config.allow_publish, "last_error": self.last_error,
                "drafts": self.store.drafts(30), "voice": voice,
                "autonomy": {**self.config.autonomy.model_dump(), "last_error": self._autonomy_error}}

    def media(self, value, *, suffixes):
        path = self.path(value)
        roots = [self.path(r) for r in self.config.media_roots]
        roots += [self.path(self.app_config.voice.cache_dir), self.path(self.app_config.voice.singing.cache_dir),
                  self.path(self.app_config.broadcast_voice.cache_dir)]
        if not any(path.is_relative_to(r) for r in roots) or not path.is_file() or path.suffix.lower() not in suffixes:
            raise PermissionError("媒体必须位于授权媒体目录中，并为支持的文件类型。")
        return str(path)

    def validate_body(self, kind, values):
        body = DraftBody.model_validate(values).model_dump()
        if kind == "dynamic":
            if not body["text"].strip():
                raise ValueError("动态正文不能为空。")
        elif kind == "video":
            if not body["title"].strip() or not body["tags"] or not body["category_id"]:
                raise ValueError("投稿需要标题、标签和分区 ID。")
            body["video_path"] = self.media(body["video_path"], suffixes={".mp4", ".mkv", ".mov", ".flv", ".webm"})
            body["cover_path"] = self.media(body["cover_path"], suffixes={".png", ".jpg", ".jpeg", ".webp"})
            for role in ("video", "cover"):
                with Path(body[role + "_path"]).open("rb") as stream:
                    signature = hashlib.file_digest(stream, "sha256").hexdigest()
                if body.get(role + "_sha256") and body[role + "_sha256"] != signature:
                    raise ValueError("草稿媒体内容已变化，请重新导入、保存并预览。")
                body[role + "_sha256"] = signature
            if body["copyright"] == 2 and not body["source"].strip():
                raise ValueError("转载投稿需要填写来源。")
        else:
            raise ValueError("草稿类型无效。")
        return body

    async def on_event(self, packet, room_id=None):
        event = normalize_live_event(packet, self._listener_room if room_id is None else room_id)
        if event:
            self.store.ingest(event["kind"], event, event["event_key"])

    async def flush_events(self):
        if not self.config.auto_reply or not self.config.actor_id:
            return
        actor_id = self.config.actor_id
        actor = self.app_config.agent.actors.get(actor_id)
        if not self.app_config.agent.enabled or not actor or not actor.enabled:
            self.last_error = "弹幕自动回复需绑定已启用的持续角色。"
            return
        from agent_core.store import ActorStore
        path = self.app_config.agent.database_path or str(Path(self.app_config.bot.runtime_database_path).parent / "actors.sqlite3")
        store = ActorStore(self.path(path))
        # Core owns the actor identity/persona. The listener only supplies audience data.
        try:
            store.actor(actor_id)
        except ValueError:
            self.last_error = "请先启动 Bot 核心以初始化绑定角色。"
            return
        account_id = "bilibili:room:" + str(self.config.room_id)
        store.upsert_account(account_id, actor_id, "bilibili", {"adapter": "broadcast", "actions": [], "room_id": self.config.room_id}, True)
        with store.connect() as db:
            queued = db.execute("SELECT COUNT(*) FROM actor_events WHERE actor_id=? AND account_id=? AND state='pending' AND kind='broadcast.audience_batch'", (actor_id, account_id)).fetchone()[0]
        if queued >= 2:
            return
        pending = self.store.events(limit=200, pending=True)
        if not pending:
            return
        # Keep chat storms from repeatedly waking a model for stale material.
        with self.store.connect() as db:
            rows = db.execute("SELECT seq FROM broadcast_events WHERE routed=0 ORDER BY seq DESC LIMIT -1 OFFSET ?", (self.config.max_pending_events,)).fetchall()
        if rows:
            self.store.mark_routed([r["seq"] for r in rows])
            pending = self.store.events(limit=200, pending=True)
        stale = [r["seq"] for r in pending if r["body"].get("room_id") != self.config.room_id]
        if stale:
            self.store.mark_routed(stale)
            pending = [r for r in pending if r["seq"] not in stale]
        batch = pending[:self.config.batch_limit]
        if batch:
            store.ingest(actor_id, account_id, "broadcast.audience_batch",
                         {"room_id": self.config.room_id, "events": [r["body"] for r in batch], "source": "bilibili_live", "untrusted_audience": True},
                         source_id="bili-batch:" + digest([r["event_key"] for r in batch]), scope=account_id)
            self.store.mark_routed([r["seq"] for r in batch])

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(self.config.batch_seconds)
            if not self.config.enabled:
                return
            try:
                await self.flush_events()
            except Exception:
                self.last_error = "弹幕事件归档暂未完成，事件已保留。"

    async def start_listener(self, *, automatic=False):
        if not self.config.enabled or not self.config.room_id:
            raise ValueError("请先启用运营服务并填写直播间号。")
        if self.listener_task and not self.listener_task.done():
            return {"listener": "running", "reused": True}
        if automatic and self._manually_stopped:
            return {"listener": "stopped", "manually_stopped": True}
        await self.stop_listener()
        if not automatic:
            self._manually_stopped = False
        self.last_error = ""
        self._listener_room = self.config.room_id
        room_id = self._listener_room
        async def receive(packet):
            await self.on_event(packet, room_id)
        async def run():
            try:
                await self.adapter.listen(room_id, receive)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = "弹幕连接失败；请核对本机登录态、直播间号及网络。"
        self.listener_task = asyncio.create_task(run())
        self.flush_task = asyncio.create_task(self._flush_loop())
        return {"listener": "connecting"}

    async def stop_listener(self):
        try:
            await asyncio.wait_for(self.adapter.disconnect(), 2)
        except Exception:
            pass
        finally:
            tasks = [t for t in (self.listener_task, self.flush_task) if t and not t.done()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return {"listener": "stopped"}

    async def external(self, operation, params):
        if not self.config.enabled or not self.config.allow_publish:
            raise PermissionError("请在控制台启用 B 站运营和实际发布接口。")
        draft_id = params.get("draft_id", "") if operation == "publish" else ""
        payload = {**{key: value for key, value in params.items() if not key.startswith("_")}, "room_id": self.config.room_id, "area_id": self.config.area_id}
        if draft_id:
            draft = self.store.draft(draft_id)
            if draft["revision"] != params.get("revision"):
                raise ValueError("草稿已修改，请按最新版本重新预览。")
            body = self.validate_body(draft["kind"], draft["body"])
            payload["draft_digest"] = digest(body)
        elif operation == "live_start" and not self.config.area_id:
            raise ValueError("开播需要分区 ID。")
        elif operation == "danmaku" and not str(params.get("text", "")).strip():
            raise ValueError("弹幕正文不能为空。")
        # Validate credentials before claiming; subsequent network errors are unknown.
        self.adapter.credential()
        key = params.get("request_key", "")
        limits = None
        if params.get("_account_actor"):
            from bilibili_operations import quotas
            cfg = self.config.autonomy
            allowed = cfg.allow_dynamics if draft_id and draft["kind"] == "dynamic" else cfg.allow_video_uploads
            if operation != "publish" or not allowed:
                raise PermissionError("该类型的自主发布权限尚未开启。")
            limits = quotas(cfg, "publish", draft_id, body["text"])
        if not self.store.claim(operation, payload, key, draft_id=draft_id, revision=params.get("revision", 0), limits=limits):
            return self.store.receipt(key)
        try:
            async with asyncio.timeout(self.config.request_timeout_seconds):
                if operation == "publish":
                    result = await (self.adapter.publish_dynamic(body) if draft["kind"] == "dynamic" else self.adapter.publish_video(body))
                elif operation == "live_start": result = await self.adapter.start_live(self.config.room_id, self.config.area_id)
                elif operation == "live_stop": result = await self.adapter.stop_live(self.config.room_id)
                elif operation == "danmaku": result = await self.adapter.send_danmaku(self.config.room_id, params["text"])
                else: raise ValueError("无效的外发操作。")
            return self.store.complete(key, "accepted", result=result, draft_id=draft_id)
        except asyncio.CancelledError:
            self.store.complete(key, "unknown", error="调用中断，须核对平台，不自动重发。", draft_id=draft_id)
            raise
        except Exception:
            # SDK exceptions can include cookies/URLs. Never forward them to an agent.
            return self.store.complete(key, "unknown", error="未取得可确认的平台回执。请核对平台；本请求不会自动重发。", draft_id=draft_id)

    async def avatar(self, params):
        from vtuber_host import execute
        cue = params.get("cue", "")
        if cue not in AVATAR_CUES:
            raise ValueError("不支持的角色/OBS动作。")
        if cue in {"obs.start_stream", "obs.stop_stream"} and not self.config.allow_publish:
            raise PermissionError("请先启用实际发布接口，再显式控制 OBS 推流。")
        body = {k: v for k, v in params.items() if k not in ("cue", "request_key")}
        if "." not in cue:
            body["type"] = cue
            if cue == "expression" and "expression" in body:
                body["name"] = body.pop("expression")
            action = "event"
        else:
            action = cue
        return await asyncio.to_thread(execute, action, body, self.app_config.vtuber)

    async def speak(self, params):
        from broadcast_speech import speak
        async with self.speech_lock:
            config = self.app_config.model_copy(deep=True)
            for key in ("catalog_path", "cache_dir", "engine_python", "engine_directory", "engine_config"):
                setattr(config.broadcast_voice, key, str(self.path(getattr(config.broadcast_voice, key))))
            config.broadcast_voice.voicestudio.source_directory = str(self.path(config.broadcast_voice.voicestudio.source_directory))
            return await speak(config, params)

    async def execute(self, operation, params=None):
        changed = self.refresh()
        if changed:
            active = self.listener_task is not None and not self.listener_task.done()
            await self.stop_listener()
            if self.config.enabled and (active or self.config.auto_listen) and not self._manually_stopped:
                await self.start_listener(automatic=True)
        p = params or {}
        from bilibili_operations import READ_OPERATIONS, WRITE_OPERATIONS, require_actor, schedule
        if p.get("_account_actor"):
            require_actor(self, p["_account_actor"])
        if self.config.enabled and self.config.autonomy.enabled and operation != "autonomy_run":
            try:
                schedule(self)
                self._autonomy_error = ""
            except Exception:
                self._autonomy_error = "自主运营尚未就绪，请核对角色绑定并启动 Bot 核心。"
        if operation == "status": return self.status()
        if operation == "autonomy_run": return schedule(self, force=True)
        if operation in READ_OPERATIONS:
            from bilibili_operations import read
            return await read(self, operation, p)
        if operation in WRITE_OPERATIONS:
            from bilibili_operations import write
            return await write(self, operation, p)
        if operation == "account": return await self.adapter.account()
        if operation == "room": return await self.adapter.room_info(self.config.room_id)
        if operation == "listener":
            if p.get("mode") not in {"start", "stop"}:
                raise ValueError("mode 必须为 start 或 stop。")
            if p["mode"] == "stop":
                self._manually_stopped = True
                return await self.stop_listener()
            return await self.start_listener(automatic=bool(p.get("automatic", False)))
        if operation == "events":
            rows = self.store.events(p.get("after", 0), p.get("limit", 50))
            return {"events": rows, "cursor": rows[-1]["seq"] if rows else p.get("after", 0)}
        if operation == "drafts": return {"drafts": self.store.drafts(p.get("limit", 50))}
        if operation == "draft_show": return self.store.draft(p["draft_id"])
        if operation == "draft_delete": return self.store.delete_draft(p["draft_id"], p["revision"])
        if operation == "draft":
            values = {k: v for k, v in p.items() if k not in {"broadcast_kind", "draft_id", "revision"} and not k.startswith("_")}
            body = self.validate_body(p.get("broadcast_kind", "dynamic"), values)
            return self.store.save_draft(p.get("broadcast_kind", "dynamic"), body, draft_id=p.get("draft_id", ""), revision=p.get("revision", 0))
        if operation == "receipt": return self.store.receipt(p["request_key"]) or {"state": "missing"}
        if operation in {"publish", "live_start", "live_stop", "danmaku"}: return await self.external(operation, p)
        if operation == "avatar": return await self.avatar(p)
        if operation == "speak": return await self.speak(p)
        if operation == "perform":
            from vtuber_host import execute
            path = self.media(p["path"], suffixes={".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".webm"})
            return await asyncio.to_thread(execute, "audio", {"path": path, "text": p.get("text", ""), "emotion": p.get("avatar_emotion", "neutral"), "interrupt": False}, self.app_config.vtuber)
        raise ValueError("未知的直播运营操作。")

    async def close(self):
        await self.stop_listener()
