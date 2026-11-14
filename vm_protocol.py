"""Local QEMU monitor and authenticated Windows guest transport, no host input."""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import socket
import time
import uuid


class VMError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.hint = "使用 vm_control.py status 查看状态；系统未配置时不会回退到主机桌面。"


@contextmanager
def file_lock(path, timeout=5):
    """A process-wide lock also shared by separate CLI and MCP workers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    if path.stat().st_size == 0:
        stream.write(b"0")
        stream.flush()
    locked = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise VMError("VM_BUSY", "虚拟机正在执行另一个操作，请稍后重新观察。") from None
                time.sleep(.025)
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


class QMP:
    def __init__(self, port, expected_name, timeout=4):
        self.port, self.name, self.timeout = int(port), expected_name, timeout

    @contextmanager
    def connection(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            with sock.makefile("rwb") as stream:
                self.stream = stream
                greeting = self._read()
                if "QMP" not in greeting:
                    raise VMError("VM_MONITOR_INVALID", "端口不是 QEMU 控制通道。")
                self._request("qmp_capabilities")
                if self._request("query-name").get("name") != self.name:
                    raise VMError("VM_IDENTITY_CHANGED", "虚拟机标识不一致，未操作其他进程。")
                yield self

    def _read(self):
        line = self.stream.readline(4 * 1024 * 1024 + 1)
        if not line or len(line) > 4 * 1024 * 1024:
            raise VMError("VM_MONITOR_DISCONNECTED", "虚拟机控制通道已断开。")
        return json.loads(line)

    def _request(self, command, arguments=None):
        identity = uuid.uuid4().hex
        self.stream.write(json.dumps({"execute": command, "arguments": arguments or {}, "id": identity}).encode() + b"\n")
        self.stream.flush()
        for _ in range(200):
            reply = self._read()
            if reply.get("id") != identity:
                continue
            if "error" in reply:
                raise VMError("VM_COMMAND_FAILED", str(reply["error"].get("desc", "QEMU command failed")))
            return reply.get("return", {})
        raise VMError("VM_MONITOR_INVALID", "控制通道未返回本次命令结果。")

    def execute(self, command, arguments=None):
        with self.connection():
            return self._request(command, arguments)


class GuestClient:
    def __init__(self, port, token, name, timeout=10):
        self.port, self.token, self.name, self.timeout = int(port), token, name, timeout

    def call(self, operation, payload=None, file=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            body = file.open("rb") if file else json.dumps(payload or {}).encode()
            headers = {"Authorization": "Bearer " + self.token, "X-QQBot-VM": self.name,
                       "Content-Type": "application/zip" if file else "application/json"}
            if file:
                headers["Content-Length"] = str(file.stat().st_size)
            try:
                connection.request("POST", "/" + operation, body=body, headers=headers)
                response = connection.getresponse()
                raw = response.read(1024 * 1024 + 1)
            finally:
                if file:
                    body.close()
            if len(raw) > 1024 * 1024:
                raise VMError("GUEST_PROTOCOL_ERROR", "来宾响应过大。")
            value = json.loads(raw)
            if response.status != 200 or not value.get("ok"):
                raise VMError(str(value.get("code") or "GUEST_ERROR"), str(value.get("error") or "Guest request failed"))
            if value.get("vm_id") != self.name:
                raise VMError("GUEST_IDENTITY_CHANGED", "来宾标识不一致。")
            return value
        except (OSError, http.client.HTTPException) as error:
            raise VMError("GUEST_NOT_READY", "Windows 来宾服务尚未就绪；先安装系统并在来宾内运行引导盘 Setup.ps1。") from error
        finally:
            connection.close()
