"""Authenticated localhost service shared by Studio, Core and CLI processes."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading

from aiohttp import web
import requests

_hosts = {}
_lock = threading.RLock()


def settings(config_path):
    from config import AppConfig
    path = Path(config_path).resolve()
    return path, AppConfig.load(str(path)).bilibili


def token_file(config_path, config):
    path = Path(config.api_token_file)
    return (path if path.is_absolute() else config_path.parent / path).resolve()


def token(config_path, config, *, create=False):
    path = token_file(config_path, config)
    if create:
        from vtuber_clients import create_private_json
        create_private_json(path, {"api_token": secrets.token_urlsafe(32)})
    return json.loads(path.read_text(encoding="utf-8")).get("api_token", "") if path.is_file() else ""


def configuration_id(config_path):
    return hashlib.sha256(str(config_path).casefold().encode()).hexdigest()[:24]


def request(config_path, operation, params=None, *, timeout=8):
    path, config = settings(config_path)
    host = "[::1]" if config.host == "::1" else config.host
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(f"http://{host}:{config.port}/api/execute", json={"operation": operation, "params": params or {}},
                                headers={"Authorization": "Bearer " + token(path, config)}, timeout=timeout, allow_redirects=False)
        if response.status_code == 401:
            raise RuntimeError("运营端口已被另一份配置占用，请为此工作区选择不同端口。")
        response.raise_for_status()
        data = response.json()
    if data.get("configuration_id") != configuration_id(path):
        raise RuntimeError("运营服务不属于当前配置，拒绝操作。")
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "运营服务操作失败。"))
    return data["data"]


def create_app(service, api_token):
    @web.middleware
    async def authentication(req, handler):
        origin = req.headers.get("Origin", "")
        if origin or not hmac.compare_digest(req.headers.get("Authorization", "").encode(), ("Bearer " + api_token).encode()):
            return web.json_response({"ok": False, "error": "Authentication required"}, status=401)
        return await handler(req)
    app = web.Application(middlewares=[authentication], client_max_size=2 * 1024 * 1024)
    async def execute(req):
        try:
            data = await req.json()
            if not isinstance(data, dict) or not isinstance(data.get("params", {}), dict):
                raise ValueError("请求参数必须为对象。")
            result = await service.execute(data.get("operation", ""), data.get("params", {}))
            return web.json_response({"ok": True, "configuration_id": configuration_id(service.config_path), "data": result})
        except (ValueError, PermissionError) as error:
            return web.json_response({"ok": False, "configuration_id": configuration_id(service.config_path), "error": str(error)})
        except Exception:
            # Third-party exceptions may contain cookies. Keep their internals private.
            return web.json_response({"ok": False, "configuration_id": configuration_id(service.config_path), "error": "运营接口未完成，请检查依赖、登录状态和本机连接。"})
    app.router.add_post("/api/execute", execute)
    async def cleanup(_): await service.close()
    app.on_cleanup.append(cleanup)
    return app


class BroadcastHost:
    def __init__(self, path, config):
        self.path, self.config = path, config
        self.ready = threading.Event()
        self.error = None
        self.loop = None
        self.stop_event = None
        self.thread = threading.Thread(target=self.run, name="qqbot-broadcast", daemon=True)

    def run(self):
        async def serve():
            from broadcast_service import BroadcastService
            self.loop = asyncio.get_running_loop()
            self.stop_event = asyncio.Event()
            runner = None
            try:
                service = BroadcastService(self.path)
                runner = web.AppRunner(create_app(service, token(self.path, self.config, create=True)), access_log=None)
                await runner.setup()
                await web.TCPSite(runner, self.config.host, self.config.port).start()
                service.store.recover()
                self.ready.set()
                await self.stop_event.wait()
            except Exception as error:
                self.error = error
                self.ready.set()
            finally:
                if runner:
                    await runner.cleanup()
        asyncio.run(serve())

    def stop(self):
        if self.loop and self.stop_event and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.stop_event.set)
            self.thread.join(5)
        return not self.thread.is_alive()


def ensure_server(config_path, *, timeout=8):
    path, config = settings(config_path)
    try:
        return {"running": True, "started": False, "status": request(path, "status", timeout=.6)}
    except requests.ConnectionError:
        pass
    except requests.Timeout as error:
        raise RuntimeError("运营端口被占用且未响应。") from error
    key = str(path).casefold()
    with _lock:
        host = _hosts.get(key)
        if not host or not host.thread.is_alive():
            token(path, config, create=True)
            host = BroadcastHost(path, config)
            _hosts[key] = host
            host.thread.start()
    if not host.ready.wait(timeout):
        raise RuntimeError("运营服务启动超时。")
    if host.error:
        # Another process may have won the bind race; authenticate before reusing it.
        try:
            return {"running": True, "started": False, "status": request(path, "status", timeout=1)}
        except Exception:
            raise RuntimeError("运营服务未能启动，请检查本机端口和配置。") from None
    return {"running": True, "started": True, "status": request(path, "status")}


def execute(config_path, operation, params=None, *, ensure=True):
    if ensure:
        ensure_server(config_path)
    _, config = settings(config_path)
    timeout = max(config.request_timeout_seconds + 5, 190) if operation in {"publish", "speak"} else 15
    if operation == "speak":
        from config import AppConfig
        voice = AppConfig.load(str(config_path)).broadcast_voice
        timeout = max(timeout, voice.timeout_seconds + voice.max_duration_seconds + 30)
    return request(config_path, operation, params, timeout=timeout)


def stop_server(config_path):
    path = Path(config_path).resolve()
    with _lock:
        host = _hosts.pop(str(path).casefold(), None)
    return {"stopped": host.stop() if host else False, "owned": host is not None}
