"""Local Live2D renderer hub, TTS/PCM lip sync and explicit OBS/VTS controls.

Importing this module has no network, playback, or Bot side effects.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import deque
import contextlib
import hmac
import importlib.util
import json
import math
import os
from pathlib import Path
import secrets
import struct
import time
from urllib.parse import quote, urlsplit
import uuid

from aiohttp import ClientSession, WSMsgType, web
from runtime_paths import application_home, resource_path
from vtuber_config import VTuberConfig, RESTART_FIELDS, config_revision
from vtuber_clients import OBSClient, VTubeStudioClient, VTuberProtocolError, create_private_json

EMOTIONS = {"neutral", "happy", "sad", "angry", "surprised", "shy", "thinking", "excited"}
MODEL_EXTENSIONS = {".json", ".moc3", ".png", ".jpg", ".jpeg", ".webp", ".wav", ".mp3"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".m4a", ".flac", ".aac", ".webm"}


def _model_references(refs):
    """Only Cubism resource fields grant access to files in the model directory."""
    for key in ("Moc", "Physics", "Pose", "UserData", "DisplayInfo"):
        if refs.get(key):
            yield refs[key]
    yield from refs.get("Textures", [])
    for expression in refs.get("Expressions", []):
        yield expression["File"]
    for motions in refs.get("Motions", {}).values():
        for motion in motions:
            yield motion["File"]
            if motion.get("Sound"):
                yield motion["Sound"]


def _local_path(value):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else application_home() / path).resolve()


def _text(value, name="text", maximum=4000):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{name} must be a string with at most {maximum} characters")
    return value


def _name(value, name):
    if not _text(value, name, 256).strip():
        raise ValueError(name + " must not be empty")
    return value


def _number(value, name, low=0, high=1):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a finite number between {low} and {high}")
    return value


def _same_token(value, expected):
    return isinstance(value, str) and hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))


def pcm_rms(pcm: bytes, sample_width=2):
    """Normalized RMS for little-endian PCM; uint8, int16, int24 and int32."""
    if sample_width not in {1, 2, 3, 4} or not pcm or len(pcm) % sample_width:
        raise ValueError("Invalid PCM sample width or incomplete sample")
    count = len(pcm) // sample_width
    if sample_width == 1:
        values = [x - 128 for x in pcm]
    elif sample_width == 2:
        values = struct.unpack("<" + "h" * count, pcm)
    elif sample_width == 4:
        values = struct.unpack("<" + "i" * count, pcm)
    else:
        values = [int.from_bytes(pcm[i:i + 3], "little", signed=True) for i in range(0, len(pcm), 3)]
    rms = math.sqrt(sum(x * x for x in values) / count) / (2 ** (8 * sample_width - 1))
    return min(1.0, rms)


class VTuberRuntime:
    def __init__(self, config: VTuberConfig | dict | None = None):
        self.config = config if isinstance(config, VTuberConfig) else VTuberConfig.model_validate(config or {})
        self._credentials = {}
        credentials = _local_path(self.config.credentials_file)
        if credentials.is_file():
            self._credentials = json.loads(credentials.read_text(encoding="utf-8"))
        self.api_token = os.environ.get(self.config.api_token_env) or self._credentials.get("api_token") or secrets.token_urlsafe(32)
        if len(self.api_token) < 24:
            raise ValueError("VTuber API token must contain at least 24 characters")
        if not os.environ.get(self.config.api_token_env) and not self._credentials.get("api_token"):
            self._credentials["api_token"] = self.api_token
            self._credentials = create_private_json(credentials, self._credentials)
            self.api_token = self._credentials["api_token"]
        self.vts = VTubeStudioClient(self.config.vts_url, _local_path(self.config.vts_token_file))
        self.obs = OBSClient(self.config.obs_url, os.environ.get(self.config.obs_password_env) or self._credentials.get("obs_password", ""))
        self.clients = set()
        self.leader = None
        self.events = deque(maxlen=64)
        self.media = {}
        self.state = {"text": "", "emotion": "neutral", "mouth": 0.0, "speaking": False,
                      "last_error": "", "model_loaded": False, "audio_blocked": False}
        self.sequence = 0
        self.last_event_at = None
        self._mouth_task = None
        self._mouth_value = 0.0
        self._mouth_updated = 0.0
        self._closed = False

    def model_file(self):
        return _local_path(self.config.model_path) if self.config.model_path else resource_path("ui/web/vtuber_assets/models/Hiyori/Hiyori.model3.json").resolve()

    def model_assets(self):
        entry = self.model_file()
        if not entry.name.endswith(".model3.json"):
            raise ValueError("Expected a Cubism .model3.json file")
        refs = json.loads(entry.read_text(encoding="utf-8"))["FileReferences"]
        root = entry.parent
        private = {_local_path(self.config.credentials_file), _local_path(self.config.vts_token_file)}
        allowed = {entry}
        for reference in _model_references(refs):
            if not isinstance(reference, str) or not reference or urlsplit(reference).scheme:
                raise ValueError("Invalid model resource reference")
            target = (root / reference).resolve()
            if (not target.is_relative_to(root) or target in private
                    or target.suffix.lower() not in MODEL_EXTENSIONS or not target.is_file()):
                raise ValueError("Model resource missing, private or outside selected model directory")
            allowed.add(target)
        return allowed

    def model_info(self):
        path = self.model_file()
        result = {"ready": False, "name": path.stem.removesuffix(".model3"), "url": "/model/" + quote(path.name),
                  "sample": not bool(self.config.model_path),
                  "attribution": "Hiyori / Live2D Inc. — official demonstration sample" if not self.config.model_path else "User supplied model"}
        try:
            if not path.name.endswith(".model3.json"):
                raise ValueError("Expected a Cubism .model3.json file")
            data = json.loads(path.read_text(encoding="utf-8"))
            refs = data["FileReferences"]
            if not isinstance(refs.get("Moc"), str) or not isinstance(refs.get("Textures"), list) or not refs["Textures"]:
                raise ValueError("Model must reference a moc3 file and at least one texture")
            self.model_assets()
            result.update(ready=True, motions={key: len(value) for key, value in refs.get("Motions", {}).items()},
                          expressions=[item["Name"] for item in refs.get("Expressions", [])])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            result["error"] = str(exc)
        return result

    def overlay_url(self, *, preview=False):
        """Contains a local bearer capability: never expose in LLM context or logs."""
        host = "127.0.0.1" if self.config.host in {"0.0.0.0", "::"} else self.config.host
        if ":" in host:
            host = "[" + host + "]"
        return f"http://{host}:{self.config.port}/overlay" + ("?preview=1" if preview else "") + "#token=" + quote(self.api_token)

    def status(self):
        assets = resource_path("ui/web/vtuber_assets/vendor")
        return {"enabled": self.config.enabled, "service": "QQBot Live2D", "host": self.config.host,
                "port": self.config.port, "config_revision": config_revision(self.config),
                "overlay_path": "/overlay", "preview_path": "/overlay?preview=1",
                "clients": len(self.clients), "state": dict(self.state), "sequence": self.sequence,
                "last_event_at": self.last_event_at, "model": self.model_info(),
                "renderer_ready": all((assets / name).is_file() for name in ("pixi.min.js", "pixi-live2d-display.min.js", "live2dcubismcore.min.js")),
                "vts": {"connected": self.vts.connected, "authenticated": self.vts.authenticated},
                "obs": {"connected": self.obs.connected, "scene": self.config.obs_scene, "source": self.config.obs_source},
                "dependencies": {key: importlib.util.find_spec(key) is not None for key in ("aiohttp", "numpy")},
                "actions": self.actions()}

    @staticmethod
    def actions():
        return ["status", "event", "audio", "pcm", "config.get", "config.set", "model.inspect", "vts.connect", "vts.disconnect",
                "vts.status", "vts.models", "vts.hotkeys", "vts.hotkey", "vts.expressions", "vts.expression", "vts.parameters",
                "vts.parameter", "vts.mouth", "obs.connect", "obs.disconnect", "obs.status", "obs.scenes", "obs.inputs",
                "obs.scene", "obs.browser_source", "obs.audio_monitor", "obs.start_stream", "obs.stop_stream",
                "obs.start_record", "obs.stop_record", "obs.virtualcam_status", "obs.virtualcam_start", "obs.virtualcam_stop"]

    async def _broadcast(self, event):
        self.sequence += 1
        self.last_event_at = time.time()
        event = {**event, "id": self.sequence, "timestamp": self.last_event_at}
        self.events.append({k: v for k, v in event.items() if k not in {"pcm_base64"}})
        dead = []
        for socket in tuple(self.clients):
            try:
                await socket.send_json(event)
            except (ConnectionError, RuntimeError):
                dead.append(socket)
        for socket in dead:
            self.clients.discard(socket)
        return {"event_id": self.sequence, "clients": len(self.clients)}

    def _queue_mouth(self, value):
        self._mouth_value = float(value)
        self._mouth_updated = time.monotonic()
        if self.vts.authenticated and (self._mouth_task is None or self._mouth_task.done()):
            self._mouth_task = asyncio.create_task(self._mouth_worker())

    async def _mouth_worker(self):
        try:
            while not self._closed and self.vts.authenticated:
                stale = time.monotonic() - self._mouth_updated > 0.3
                await self.vts.mouth(0.0 if stale else self._mouth_value)
                if stale:
                    break
                await asyncio.sleep(1 / 20)
        except (Exception, asyncio.CancelledError) as exc:
            if not isinstance(exc, asyncio.CancelledError):
                self.state["last_error"] = "VTube Studio lip sync: " + str(exc)

    async def publish_event(self, event: dict):
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        kind = event.get("type")
        normalized = {"type": kind}
        if kind in {"text", "emotion", "speech"}:
            if kind in {"text", "speech"}:
                normalized["text"] = _text(event.get("text", ""))
            if kind in {"emotion", "speech"}:
                if event.get("emotion", "neutral") not in EMOTIONS:
                    raise ValueError("Unknown emotion")
                normalized["emotion"] = event.get("emotion", "neutral")
        elif kind == "motion":
            normalized["group"] = _name(event.get("group", "Idle"), "group")
            index = event.get("index", 0)
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 999:
                raise ValueError("motion index must be an integer between 0 and 999")
            normalized["index"] = index
        elif kind == "expression":
            normalized["name"] = _name(event.get("name"), "expression name")
        elif kind == "mouth":
            normalized["value"] = _number(event.get("value"), "mouth")
            self._queue_mouth(normalized["value"])
            self.state["mouth"] = normalized["value"]
        elif kind in {"reset", "speech_stop", "pcm_end"}:
            self.state.update(mouth=0.0, speaking=False)
            self._queue_mouth(0)
            if kind == "reset":
                self.state.update(text="", emotion="neutral")
        else:
            raise ValueError("Unknown event type")
        for key in ("text", "emotion"):
            if key in normalized:
                self.state[key] = normalized[key]
        return await self._broadcast(normalized)

    async def sync_audio(self, path, text="", emotion="neutral", interrupt=True):
        audio = _local_path(_name(str(path), "path"))
        if audio.suffix.lower() not in AUDIO_EXTENSIONS or not audio.is_file():
            raise ValueError("Audio must be an existing WAV/MP3/OGG/M4A/FLAC/AAC/WebM file")
        if audio.stat().st_size > 256 * 1024 * 1024:
            raise ValueError("Audio file exceeds 256 MiB")
        _text(text)
        if emotion not in EMOTIONS or not isinstance(interrupt, bool):
            raise ValueError("Invalid emotion or interrupt")
        for key, value in list(self.media.items()):
            if time.monotonic() - value[1] > 3600:
                del self.media[key]
        media_id = uuid.uuid4().hex
        self.media[media_id] = (audio, time.monotonic())
        while len(self.media) > 128:
            del self.media[next(iter(self.media))]
        self.state.update(text=text, emotion=emotion, speaking=bool(self.clients))
        result = await self._broadcast({"type": "audio", "url": "/media/" + media_id,
                                       "text": text, "emotion": emotion, "interrupt": interrupt})
        return {**result, "media_id": media_id, "queued": bool(self.clients),
                "note": "Playback and RMS mouth tracking begin together in the OBS browser; a regular browser may require Enable audio."}

    async def push_pcm(self, pcm: bytes, sample_rate=24000, channels=1, sample_width=2, text="", emotion="neutral"):
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 192000:
            raise ValueError("sample_rate must be 8000..192000")
        if isinstance(channels, bool) or isinstance(sample_width, bool) or channels not in {1, 2} or sample_width not in {1, 2, 3, 4}:
            raise ValueError("PCM requires 1/2 channels and 1/2/3/4-byte little-endian integer samples")
        if not isinstance(pcm, bytes) or len(pcm) > sample_rate * channels * sample_width * 2 or len(pcm) % (channels * sample_width):
            raise ValueError("PCM must contain complete frames and at most two seconds per chunk")
        rms = pcm_rms(pcm, sample_width)
        _text(text)
        if emotion not in EMOTIONS:
            raise ValueError("Unknown emotion")
        self.state.update(text=text or self.state["text"], emotion=emotion, speaking=True)
        if not self.clients and self.vts.authenticated:
            # Native-VTS users may already be playing PCM externally. With a browser,
            # its analyser acknowledgements always supply the actual playback clock.
            self._queue_mouth(max(0.0, min(1.0, (rms - 0.012) * 5.5)))
        return await self._broadcast({"type": "pcm", "pcm_base64": base64.b64encode(pcm).decode(),
            "sample_rate": sample_rate, "channels": channels, "sample_width": sample_width,
            "rms": rms, "text": text, "emotion": emotion})

    async def configure(self, changes):
        if not isinstance(changes, dict):
            raise ValueError("config changes must be an object")
        # Port/auth changes require a service restart, preventing half-reconfigured listeners.
        forbidden = RESTART_FIELDS & changes.keys()
        if forbidden:
            raise ValueError("Restart required to change: " + ", ".join(sorted(forbidden)))
        updated = VTuberConfig.model_validate({**self.config.model_dump(), **changes})
        await self.vts.close()
        await self.obs.close()
        self.config = updated
        self.vts = VTubeStudioClient(updated.vts_url, _local_path(updated.vts_token_file))
        self.obs = OBSClient(updated.obs_url, os.environ.get(updated.obs_password_env) or self._credentials.get("obs_password", ""))
        self.state.update(model_loaded=False)
        await self._broadcast({"type": "config", "config": self.config.model_dump(), "model": self.model_info()})
        return self.config.model_dump()

    async def execute(self, action: str, payload: dict | None = None):
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        if action not in self.actions():
            raise ValueError("Unknown VTuber action")
        if action == "status": return self.status()
        if action == "event": return await self.publish_event(payload)
        if action == "audio":
            return await self.sync_audio(payload.get("path", ""), payload.get("text", ""), payload.get("emotion", "neutral"), payload.get("interrupt", True))
        if action == "pcm":
            try: pcm = base64.b64decode(payload.get("pcm_base64", ""), validate=True)
            except (ValueError, TypeError) as exc: raise ValueError("Invalid PCM base64") from exc
            return await self.push_pcm(pcm, payload.get("sample_rate", 24000), payload.get("channels", 1), payload.get("sample_width", 2), payload.get("text", ""), payload.get("emotion", "neutral"))
        if action == "config.get": return self.config.model_dump()
        if action == "config.set": return await self.configure(payload)
        if action == "model.inspect": return self.model_info()
        if action == "vts.connect":
            if not isinstance(payload.get("request_new", False), bool): raise ValueError("request_new must be boolean")
            return await self.vts.authenticate(payload.get("request_new", False))
        if action == "vts.disconnect":
            await self.vts.close()
            return {"connected": False}
        if action.startswith("vts."):
            if not self.vts.authenticated:
                raise VTuberProtocolError("Connect and approve VTube Studio before model control")
            calls = {"vts.status": "CurrentModelRequest", "vts.models": "AvailableModelsRequest",
                     "vts.hotkeys": "HotkeysInCurrentModelRequest", "vts.expressions": "ExpressionStateRequest",
                     "vts.parameters": "InputParameterListRequest"}
            if action in calls: return await self.vts.request(calls[action])
            if action == "vts.hotkey": return await self.vts.request("HotkeyTriggerRequest", {"hotkeyID": _name(payload.get("hotkey_id"), "hotkey_id")})
            if action == "vts.expression":
                if not isinstance(payload.get("active", True), bool): raise ValueError("active must be boolean")
                return await self.vts.request("ExpressionActivationRequest", {"expressionFile": _name(payload.get("expression"), "expression"), "active": payload.get("active", True)})
            if action == "vts.mouth": return await self.vts.mouth(_number(payload.get("value"), "value"))
            if action == "vts.parameter":
                return await self.vts.request("InjectParameterDataRequest", {"faceFound": True, "mode": "set", "parameterValues": [
                    {"id": _name(payload.get("id"), "id"), "value": _number(payload.get("value"), "value", -1000000, 1000000), "weight": 1.0}]})
        if action == "obs.connect": return await self.obs.request("GetVersion")
        if action == "obs.disconnect":
            await self.obs.close()
            return {"connected": False}
        calls = {"obs.status": "GetStreamStatus", "obs.scenes": "GetSceneList", "obs.inputs": "GetInputList",
                 "obs.virtualcam_status": "GetVirtualCamStatus", "obs.virtualcam_start": "StartVirtualCam", "obs.virtualcam_stop": "StopVirtualCam",
                 "obs.start_stream": "StartStream", "obs.stop_stream": "StopStream", "obs.start_record": "StartRecord", "obs.stop_record": "StopRecord"}
        if action in calls: return await self.obs.request(calls[action])
        if action == "obs.scene":
            return await self.obs.request("SetCurrentProgramScene", {"sceneName": _name(payload.get("scene", self.config.obs_scene), "scene")})
        if action == "obs.audio_monitor":
            mode = payload.get("mode", "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT")
            if mode not in {"OBS_MONITORING_TYPE_NONE", "OBS_MONITORING_TYPE_MONITOR_ONLY", "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"}:
                raise ValueError("Invalid OBS monitoring mode")
            return await self.obs.request("SetInputAudioMonitorType", {"inputName": _name(payload.get("source", self.config.obs_source), "source"), "monitorType": mode})
        if action == "obs.browser_source":
            scene = _name(payload.get("scene", self.config.obs_scene), "scene")
            source = _name(payload.get("source", self.config.obs_source), "source")
            scenes = await self.obs.request("GetSceneList")
            if scene not in {item["sceneName"] for item in scenes.get("scenes", [])}:
                await self.obs.request("CreateScene", {"sceneName": scene})
            settings = {"url": self.overlay_url(), "width": self.config.width, "height": self.config.height,
                "is_local_file": False, "shutdown": False, "restart_when_active": False,
                "reroute_audio": True, "css": "body { background-color: rgba(0, 0, 0, 0); margin: 0; overflow: hidden; }"}
            inputs = await self.obs.request("GetInputList")
            if source in {item["inputName"] for item in inputs.get("inputs", [])}:
                await self.obs.request("SetInputSettings", {"inputName": source, "inputSettings": settings, "overlay": True})
                items = await self.obs.request("GetSceneItemList", {"sceneName": scene})
                if source not in {item["sourceName"] for item in items.get("sceneItems", [])}:
                    await self.obs.request("CreateSceneItem", {"sceneName": scene, "sourceName": source, "sceneItemEnabled": True})
            else:
                await self.obs.request("CreateInput", {"sceneName": scene, "inputName": source, "inputKind": "browser_source", "inputSettings": settings, "sceneItemEnabled": True})
            return {"configured": True, "scene": scene, "source": source, "width": self.config.width, "height": self.config.height,
                    "audio": "OBS browser source audio; use obs.audio_monitor if headphones are needed"}
        raise ValueError("Unhandled action")

    async def close(self):
        self._closed = True
        if self._mouth_task and not self._mouth_task.done():
            self._mouth_task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await self._mouth_task
        for socket in tuple(self.clients):
            await socket.close(code=1001)
        self.clients.clear()
        await self.vts.close()
        await self.obs.close()


_runtime = None
RUNTIME_KEY = web.AppKey("vtuber_runtime", VTuberRuntime)


def get_vtuber_runtime(config: VTuberConfig | dict | None = None):
    global _runtime
    if _runtime is None or _runtime._closed:
        _runtime = VTuberRuntime(config)
    return _runtime


def create_app(runtime: VTuberRuntime | None = None):
    runtime = runtime or get_vtuber_runtime()

    @web.middleware
    async def safety(request, handler):
        host = request.host.split(":")[0] if not request.host.startswith("[") else request.host.split("]")[0] + "]"
        allowed = {"127.0.0.1", "localhost", "[::1]", runtime.config.host}
        if not runtime.config.allow_remote and host not in allowed:
            raise web.HTTPForbidden(text="Invalid local Host")
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="Cross-origin control is disabled")
        if request.path.startswith(("/api/", "/media/")):
            token = request.headers.get("Authorization", "")
            if not _same_token(token, "Bearer " + runtime.api_token):
                raise web.HTTPUnauthorized(text="Bearer token required")
        try:
            response = await handler(request)
        except (ValueError, KeyError, TypeError) as exc:
            response = web.json_response({"ok": False, "error": str(exc)}, status=400)
        except (VTuberProtocolError, OSError, asyncio.TimeoutError) as exc:
            runtime.state["last_error"] = str(exc)
            response = web.json_response({"ok": False, "error": str(exc)}, status=503)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if request.path.startswith(("/api/", "/media/")) else "no-cache"
        return response

    app = web.Application(middlewares=[safety], client_max_size=4 * 1024 * 1024)
    app[RUNTIME_KEY] = runtime

    async def overlay(request):
        return web.FileResponse(resource_path("ui/web/vtuber_overlay.html"))

    async def static(request):
        base = resource_path("ui/web").resolve()
        target = (base / request.match_info["path"]).resolve()
        if not target.is_relative_to(base) or not target.is_file() or not (target.name.startswith("vtuber_overlay.") or target.is_relative_to(base / "vtuber_assets")):
            raise web.HTTPNotFound()
        return web.FileResponse(target)

    async def model(request):
        root = runtime.model_file().parent
        target = (root / request.match_info["path"]).resolve()
        try:
            allowed = runtime.model_assets()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise web.HTTPNotFound() from None
        if target not in allowed:
            raise web.HTTPNotFound()
        return web.FileResponse(target)

    async def media(request):
        item = runtime.media.get(request.match_info["media_id"])
        if item is None or time.monotonic() - item[1] > 3600:
            raise web.HTTPNotFound()
        return web.FileResponse(item[0])

    async def status(request): return web.json_response({"ok": True, "result": runtime.status()})

    async def config(request):
        result = await runtime.configure(await request.json()) if request.method == "POST" else runtime.config.model_dump()
        return web.json_response({"ok": True, "result": result})

    async def execute(request):
        body = await request.json()
        if not isinstance(body, dict): raise ValueError("Expected JSON object")
        action = request.match_info.get("action") or body.get("action")
        payload = body if request.match_info.get("action") else body.get("payload", {})
        return web.json_response({"ok": True, "result": await runtime.execute(action, payload)})

    async def websocket(request):
        socket = web.WebSocketResponse(heartbeat=20, max_msg_size=65536)
        await socket.prepare(request)
        try:
            auth = await socket.receive_json(timeout=5)
            if not isinstance(auth, dict) or auth.get("type") != "auth" or not _same_token(auth.get("token", ""), runtime.api_token):
                await socket.close(code=1008, message=b"Authentication required")
                return socket
            runtime.clients.add(socket)
            if runtime.leader is None: runtime.leader = socket
            await socket.send_json({"type": "snapshot", "state": runtime.state, "model": runtime.model_info(), "config": {"width": runtime.config.width, "height": runtime.config.height}})
            async for message in socket:
                if message.type != WSMsgType.TEXT: continue
                try:
                    body = json.loads(message.data)
                    if not isinstance(body, dict): raise ValueError("Expected browser event object")
                    if body.get("type") == "mouth" and socket is runtime.leader:
                        value = _number(body.get("value"), "mouth")
                        runtime.state["mouth"] = value
                        runtime.state["speaking"] = value > 0.005
                        runtime._queue_mouth(value)
                    elif body.get("type") == "ready":
                        runtime.state.update(model_loaded=True, last_error="")
                    elif body.get("type") == "audio_blocked":
                        runtime.state["audio_blocked"] = True
                    elif body.get("type") == "audio_started":
                        runtime.state["audio_blocked"] = False
                        runtime.state["speaking"] = True
                    elif body.get("type") == "audio_ended" and socket is runtime.leader:
                        runtime.state.update(speaking=False, mouth=0.0)
                        runtime._queue_mouth(0)
                    elif body.get("type") == "error":
                        runtime.state["last_error"] = _text(body.get("message", "Overlay error"), maximum=1000)
                except (ValueError, TypeError):
                    await socket.send_json({"type": "error", "message": "Invalid browser event"})
        except (asyncio.TimeoutError, ValueError, ConnectionError):
            await socket.close(code=1008)
        finally:
            runtime.clients.discard(socket)
            if runtime.leader is socket:
                runtime.leader = next(iter(runtime.clients), None)
                runtime._queue_mouth(0)
            if not runtime.clients:
                runtime.state.update(model_loaded=False, speaking=False, mouth=0.0)
        return socket

    app.router.add_get("/", overlay)
    app.router.add_get("/overlay", overlay)
    app.router.add_get("/assets/{path:.*}", static)
    app.router.add_get("/model/{path:.*}", model)
    app.router.add_get("/media/{media_id}", media)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/config", config)
    app.router.add_post("/api/config", config)
    app.router.add_post("/api/execute", execute)
    app.router.add_post("/api/{action:event|audio|pcm}", execute)
    app.router.add_get("/ws", websocket)
    async def cleanup(_app): await runtime.close()
    app.on_cleanup.append(cleanup)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["serve", "status", "url", "execute"], nargs="?", default="status")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--action", default="status")
    parser.add_argument("--payload", default="{}", help="JSON object; prefer --payload-file for multiline content")
    parser.add_argument("--payload-file", type=Path)
    args = parser.parse_args()
    config = VTuberConfig.model_validate_json(args.config.read_text(encoding="utf-8")) if args.config else VTuberConfig()
    runtime = VTuberRuntime(config)
    if args.command == "url":
        print(runtime.overlay_url(preview=True))
    elif args.command == "serve":
        print(f"Live2D service: http://{config.host}:{config.port}/overlay (authenticated URL: python vtuber_service.py url)", flush=True)
        web.run_app(create_app(runtime), host=config.host, port=config.port, access_log=None, print=None)
    elif args.command == "status":
        print(json.dumps(runtime.status(), ensure_ascii=False, indent=2))
    else:
        payload = json.loads(args.payload_file.read_text(encoding="utf-8") if args.payload_file else args.payload)
        async def send():
            async with ClientSession() as session:
                async with session.post(f"http://{config.host}:{config.port}/api/execute", json={"action": args.action, "payload": payload},
                                        headers={"Authorization": "Bearer " + runtime.api_token}) as response:
                    data = await response.json()
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                    if response.status >= 400: raise SystemExit(1)
        asyncio.run(send())


if __name__ == "__main__": main()
