"""Small native clients for the public VTube Studio API and OBS WebSocket v5."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import aiohttp


class VTuberProtocolError(RuntimeError):
    pass


def private_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def create_private_json(path: Path, data: dict):
    """Create once atomically; another process's credentials always win a race."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    try:
        try: os.link(temp, path)
        except FileExistsError: pass
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("api_token"):
            return result
        # Also support a hand-created file containing only an OBS password.
        lock_path = path.with_name(path.name + ".lock")
        deadline = time.monotonic() + 3
        while True:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Credentials initialization is locked by another process")
                time.sleep(0.02)
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            if not result.get("api_token"):
                result["api_token"] = data["api_token"]
                private_json(path, result)
            return result
        finally:
            os.close(descriptor)
            lock_path.unlink(missing_ok=True)
    finally:
        temp.unlink(missing_ok=True)


class SocketClient:
    def __init__(self, url):
        self.url = url
        self.session = None
        self.ws = None
        self.lock = asyncio.Lock()
        self.connected = False

    async def open(self):
        if self.ws is None or self.ws.closed:
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
            self.ws = await self.session.ws_connect(self.url, heartbeat=20, timeout=5, max_msg_size=4 * 1024 * 1024)
        return self.ws

    async def receive(self):
        msg = await self.ws.receive(timeout=15)
        if msg.type != aiohttp.WSMsgType.TEXT:
            self.connected = False
            raise VTuberProtocolError("Control WebSocket disconnected")
        return json.loads(msg.data)

    async def close(self):
        self.connected = False
        if self.ws is not None:
            await self.ws.close()
        if self.session is not None:
            await self.session.close()


class VTubeStudioClient(SocketClient):
    PLUGIN = {"pluginName": "QQBot Live2D", "pluginDeveloper": "QQBot"}

    def __init__(self, url, token_file):
        super().__init__(url)
        self.token_file = Path(token_file)
        self.authenticated = False

    async def request(self, message_type, data=None):
        async with self.lock:
            await self.open()
            request_id = uuid.uuid4().hex
            await self.ws.send_json({"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",
                                     "requestID": request_id, "messageType": message_type, "data": data or {}})
            async with asyncio.timeout(20):
                while True:
                    response = await self.receive()
                    if response.get("requestID") != request_id:
                        continue
                    if response.get("messageType") == "APIError":
                        raise VTuberProtocolError(str(response.get("data", {}).get("message", "VTube Studio API error")))
                    self.connected = True
                    return response.get("data", {})

    async def authenticate(self, request_new=False):
        token = ""
        if self.token_file.is_file() and not request_new:
            token = json.loads(self.token_file.read_text(encoding="utf-8")).get("authenticationToken", "")
        if not token:
            response = await self.request("AuthenticationTokenRequest", self.PLUGIN)
            token = response["authenticationToken"]
        response = await self.request("AuthenticationRequest", {**self.PLUGIN, "authenticationToken": token})
        self.authenticated = bool(response.get("authenticated"))
        if not self.authenticated:
            raise VTuberProtocolError("VTube Studio rejected authentication; approve the plugin in VTS and reconnect with request_new=true")
        private_json(self.token_file, {"authenticationToken": token})
        return {"connected": True, "authenticated": True}

    async def mouth(self, value):
        if not self.authenticated:
            return {"skipped": "VTube Studio is not authenticated"}
        return await self.request("InjectParameterDataRequest", {"faceFound": True, "mode": "set",
            "parameterValues": [{"id": "MouthOpen", "value": max(0.0, min(1.0, float(value))), "weight": 1.0}]})

    async def close(self):
        self.authenticated = False
        await super().close()


class OBSClient(SocketClient):
    def __init__(self, url, password=""):
        super().__init__(url)
        self.password = password
        self.identified = False

    @staticmethod
    def authentication(password, salt, challenge):
        secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
        return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()

    async def connect(self):
        await self.open()
        if self.identified:
            return {"connected": True}
        hello = await self.receive()
        if hello.get("op") != 0:
            raise VTuberProtocolError("OBS did not send WebSocket v5 Hello")
        data = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = hello.get("d", {}).get("authentication")
        if auth:
            if not self.password:
                raise VTuberProtocolError("OBS requires a password; set QQBOT_OBS_PASSWORD or private credentials obs_password")
            data["authentication"] = self.authentication(self.password, auth["salt"], auth["challenge"])
        await self.ws.send_json({"op": 1, "d": data})
        response = await self.receive()
        if response.get("op") != 2:
            raise VTuberProtocolError("OBS authentication failed")
        self.identified = self.connected = True
        return {"connected": True, "rpc_version": response.get("d", {}).get("negotiatedRpcVersion", 1)}

    async def request(self, request_type, data=None):
        async with self.lock:
            if self.ws is None or self.ws.closed:
                self.identified = False
            await self.connect()
            request_id = uuid.uuid4().hex
            await self.ws.send_json({"op": 6, "d": {"requestType": request_type, "requestId": request_id, "requestData": data or {}}})
            async with asyncio.timeout(20):
                while True:
                    response = await self.receive()
                    body = response.get("d", {})
                    if response.get("op") != 7 or body.get("requestId") != request_id:
                        continue
                    result = body.get("requestStatus", {})
                    if not result.get("result"):
                        raise VTuberProtocolError(f"OBS {request_type}: {result.get('code')} {result.get('comment', '')}")
                    return body.get("responseData", {})

    async def close(self):
        self.identified = False
        await super().close()
