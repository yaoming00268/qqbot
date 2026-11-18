"""Thread-hosted singleton HTTP service shared by Studio, Core and CLI processes.

Secrets stay in this transport layer. Return values are safe for agent context;
only overlay_url() returns a bearer capability intended for a local browser UI.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from urllib.parse import quote

from aiohttp import web
import requests
from vtuber_config import VTuberConfig, RESTART_FIELDS, config_revision
from vtuber_service import VTuberRuntime, _local_path, create_app

_hosts = {}
_lock = threading.RLock()


def _config(config=None):
    return config if isinstance(config, VTuberConfig) else VTuberConfig.model_validate(config or {})


def _token(config):
    token = os.environ.get(config.api_token_env)
    if not token:
        path = _local_path(config.credentials_file)
        if path.is_file():
            token = json.loads(path.read_text(encoding="utf-8")).get("api_token")
    return token or ""


def _base(config):
    host = "127.0.0.1" if config.host in {"0.0.0.0", "::"} else config.host
    if ":" in host: host = "[" + host + "]"
    return f"http://{host}:{config.port}"


def _request(config, path, *, data=None, timeout=5):
    with requests.Session() as session:
        session.trust_env = False
        response = session.request("GET" if data is None else "POST", _base(config) + path, json=data,
            headers={"Authorization": "Bearer " + _token(config)}, timeout=timeout)
        if response.status_code == 401:
            raise RuntimeError("Live2D service credentials differ; use the same credentials_file and api_token_env across processes")
        try: result = response.json()
        except ValueError as exc: raise RuntimeError("Port is occupied by a different HTTP service") from exc
        if not response.ok or not result.get("ok"):
            raise RuntimeError(result.get("error", f"Live2D HTTP {response.status_code}"))
        return result["result"]


class VTuberServiceHost:
    def __init__(self, config):
        self.config = config
        self.ready = threading.Event()
        self.error = None
        self.loop = None
        self.stop_event = None
        self.thread = threading.Thread(target=self._thread_main, name=f"qqbot-live2d-{config.port}", daemon=True)

    def _thread_main(self):
        try: asyncio.run(self._run())
        except Exception as exc:
            self.error = exc
            self.ready.set()

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        runtime = VTuberRuntime(self.config)
        runner = web.AppRunner(create_app(runtime), access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, self.config.host, self.config.port).start()
            self.ready.set()
            await self.stop_event.wait()
        finally:
            await runner.cleanup()

    def stop(self, timeout=5):
        if self.loop and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(timeout)
        return not self.thread.is_alive()


def _sync_config(config, status):
    if status.get("config_revision") == config_revision(config):
        return status
    current = _request(config, "/api/config")
    changes = {key: value for key, value in config.model_dump(mode="json").items()
               if current.get(key) != value}
    restart = changes.keys() & RESTART_FIELDS
    if restart:
        raise RuntimeError("Restart Live2D service to change: " + ", ".join(sorted(restart)))
    if changes:
        _request(config, "/api/config", data=changes)
        return _request(config, "/api/status")
    return status


def ensure_server(config: VTuberConfig | dict | None = None, timeout=8):
    """Start only when absent, otherwise reuse the authenticated existing listener.

    Does not connect OBS/VTS, change scenes, play audio, or start a stream.
    """
    config = _config(config)
    try:
        status = _request(config, "/api/status", timeout=0.5)
        return {"running": True, "started": False, "status": _sync_config(config, status)}
    except requests.ConnectionError:
        pass
    except requests.Timeout:
        raise RuntimeError("Live2D port is occupied but not responding")
    key = (config.host, config.port)
    with _lock:
        host = _hosts.get(key)
        if host is None or not host.thread.is_alive():
            # Create credentials before spawning, so a concurrent process can authenticate.
            VTuberRuntime(config)
            host = VTuberServiceHost(config)
            _hosts[key] = host
            host.thread.start()
    if not host.ready.wait(timeout):
        raise RuntimeError("Timed out starting Live2D service")
    if host.error:
        # A different process may have won the port race. Authenticate it before reuse.
        try:
            status = _request(config, "/api/status", timeout=1)
            return {"running": True, "started": False, "status": _sync_config(config, status)}
        except Exception:
            raise RuntimeError("Cannot start Live2D service: " + str(host.error)) from host.error
    return {"running": True, "started": True, "status": _request(config, "/api/status")}


def execute(action: str, payload: dict | None = None, config: VTuberConfig | dict | None = None, *, ensure=True):
    config = _config(config)
    if ensure: ensure_server(config)
    return _request(config, "/api/execute", data={"action": action, "payload": payload or {}}, timeout=30)


def overlay_url(config: VTuberConfig | dict | None = None, *, preview=True):
    """Local GUI/browser use only; never serialize this URL into agent context."""
    config = _config(config)
    ensure_server(config)
    return _base(config) + "/overlay" + ("?preview=1" if preview else "") + "#token=" + quote(_token(config))


def stop_server(config: VTuberConfig | dict | None = None):
    """Stop this process's hosted service only; cannot stop another Bot/process."""
    config = _config(config)
    with _lock:
        host = _hosts.pop((config.host, config.port), None)
    return {"stopped": host.stop() if host else False, "owned": host is not None}
