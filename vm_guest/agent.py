"""Runs ONLY in the logged-in Windows VM. No host paths or host input APIs."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parent


def checked_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path is outside the guest game copy.")
    return path


def game_id(value):
    if not re.fullmatch(r"[0-9a-f]{24}", str(value)):
        raise ValueError("Invalid game id.")
    return value


class Desktop:
    def __init__(self, root):
        if os.name != "nt":
            raise RuntimeError("The guest bridge requires Windows.")
        self.root = root
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        signatures = [(self.u.EnumWindows, [self.callback_type, w.LPARAM], w.BOOL),
            (self.u.IsWindowVisible, [w.HWND], w.BOOL),
            (self.u.GetWindowThreadProcessId, [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            (self.u.GetWindowRect, [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            (self.u.GetClientRect, [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            (self.u.ScreenToClient, [w.HWND, ctypes.POINTER(w.POINT)], w.BOOL),
            (self.u.GetForegroundWindow, [], w.HWND),
            (self.u.GetWindowTextW, [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.u.SetForegroundWindow, [w.HWND], w.BOOL),
            (self.u.ShowWindow, [w.HWND, ctypes.c_int], w.BOOL),
            (self.u.WindowFromPoint, [w.POINT], w.HWND),
            (self.u.IsChild, [w.HWND, w.HWND], w.BOOL),
            (self.k.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.k.QueryFullProcessImageNameW, [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.k.CloseHandle, [w.HANDLE], w.BOOL)]
        for function, arguments, result in signatures:
            function.argtypes, function.restype = arguments, result
        self.u.SetProcessDPIAware()

    def path(self, pid):
        process = self.k.OpenProcess(0x1000, False, pid)
        if not process:
            return None
        try:
            buffer, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
            return Path(buffer.value).resolve() if self.k.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)) else None
        finally:
            self.k.CloseHandle(process)

    def windows(self, identity):
        root, rows = self.root / game_id(identity), []
        @self.callback_type
        def callback(hwnd, param):
            if not self.u.IsWindowVisible(hwnd):
                return True
            pid = w.DWORD()
            self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            executable = self.path(pid.value)
            if executable and executable.is_relative_to(root):
                rect, title = w.RECT(), ctypes.create_unicode_buffer(2048)
                self.u.GetWindowRect(hwnd, ctypes.byref(rect))
                self.u.GetWindowTextW(hwnd, title, len(title))
                if rect.right - rect.left > 50 and rect.bottom - rect.top > 50:
                    rows.append({"hwnd": hwnd, "pid": pid.value, "title": title.value,
                                 "rect": [rect.left, rect.top, rect.right, rect.bottom]})
            return True
        self.u.EnumWindows(callback, 0)
        return rows

    def target(self, identity, focus=False):
        rows = self.windows(identity)
        if len(rows) != 1:
            raise ValueError("Game does not have exactly one visible guest window.")
        row = rows[0]
        if focus:
            self.u.ShowWindow(row["hwnd"], 9)
            self.u.SetForegroundWindow(row["hwnd"])
            time.sleep(.1)
            rows = self.windows(identity)
            if len(rows) != 1 or rows[0]["hwnd"] != row["hwnd"]:
                raise ValueError("Guest window changed.")
            row = rows[0]
        row["foreground"] = self.u.GetForegroundWindow() == row["hwnd"]
        return row

    def validate_click(self, identity, x, y, hwnd):
        row = self.target(identity)
        if row["hwnd"] != hwnd or not row["foreground"] or type(x) is not int or type(y) is not int:
            raise ValueError("Guest window no longer owns the input.")
        screen = w.POINT(x, y)
        visible = self.u.WindowFromPoint(screen)
        if visible != hwnd and not self.u.IsChild(hwnd, visible):
            raise ValueError("Guest game is covered by another guest window.")
        client, rect = w.POINT(x, y), w.RECT()
        if (not self.u.ScreenToClient(hwnd, ctypes.byref(client)) or not self.u.GetClientRect(hwnd, ctypes.byref(rect))
                or not 0 <= client.x < rect.right or not 0 <= client.y < rect.bottom):
            raise ValueError("Click is outside the guest game client area.")
        return {"valid": True}

    def launch(self, identity):
        directory = self.root / game_id(identity)
        info = json.loads((directory / "qqbot-package.json").read_text(encoding="utf-8"))
        executable = checked_path(directory, info["executable"])
        if not executable.is_file() or executable.suffix.lower() != ".exe":
            raise ValueError("Guest executable does not exist.")
        rows = self.windows(identity)
        if rows:
            return {**self.target(identity, True), "attached": True}
        process = subprocess.Popen([str(executable)], cwd=executable.parent, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"pid": process.pid, "attached": False, "mode": "qemu"}


def unpack_game(package, games):
    with zipfile.ZipFile(package) as archive:
        info = json.loads(archive.read("qqbot-package.json"))
        identity = game_id(info["game_id"])
        destination = games / identity
        if destination.exists():
            return {"game_id": identity, "installed": True}
        files = archive.infolist()
        if len(files) > 200000 or sum(item.file_size for item in files) > 60 * 1024 ** 3:
            raise ValueError("Game copy is too large.")
        with tempfile.TemporaryDirectory(prefix="qqbot-stage-", dir=games) as temporary:
            root = Path(temporary)
            for item in files:
                relative = PurePosixPath(item.filename.replace("\\", "/"))
                if relative.is_absolute() or ".." in relative.parts or ":" in item.filename:
                    raise ValueError("Invalid package path.")
                target = checked_path(root, str(relative))
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
            if not checked_path(root, info["executable"]).is_file():
                raise ValueError("Game executable is missing.")
            # Rename only on success; never overwrite existing guest saves.
            root.rename(destination)
        return {"game_id": identity, "installed": True}


class Bridge(BaseHTTPRequestHandler):
    server_version = "QQBotGuest/1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        config, games = self.server.config, self.server.games
        if (not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + config["token"])
                or self.headers.get("X-QQBot-VM") != config["vm_id"]):
            self.respond({"ok": False, "code": "UNAUTHORIZED", "error": "Unauthorized guest request."}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 60 * 1024 ** 3:
                raise ValueError("Request is too large.")
            if self.path == "/stage":
                with tempfile.TemporaryFile() as package:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("Incomplete game upload.")
                        package.write(chunk)
                        remaining -= len(chunk)
                    package.seek(0)
                    result = unpack_game(package, games)
            else:
                if length > 1024 * 1024:
                    raise ValueError("Request is too large.")
                data = json.loads(self.rfile.read(length) or b"{}")
                identity = data.get("game_id", "")
                if self.path == "/health":
                    result = {"protocol": 1, "interactive": True}
                elif self.path == "/installed":
                    result = {"installed": (games / game_id(identity) / "qqbot-package.json").is_file()}
                elif self.path == "/launch":
                    result = self.server.desktop.launch(identity)
                elif self.path == "/target":
                    result = self.server.desktop.target(identity, data.get("focus", False))
                elif self.path == "/validate_click":
                    result = self.server.desktop.validate_click(identity, data["x"], data["y"], data["hwnd"])
                else:
                    raise ValueError("Unsupported guest operation.")
            self.respond({**result, "ok": True, "vm_id": config["vm_id"]})
        except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
            self.respond({"ok": False, "code": "GUEST_REQUEST_FAILED", "error": str(error), "vm_id": config["vm_id"]}, 400)

    def respond(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    config = json.loads((ROOT / "guest.json").read_text(encoding="utf-8-sig"))
    games = ROOT / "games"
    games.mkdir(exist_ok=True)
    # This bind is INSIDE the guest. The host exposes only its loopback forward.
    server = HTTPServer(("0.0.0.0", config["port"]), Bridge)
    server.config, server.games, server.desktop = config, games, Desktop(games)
    server.serve_forever()


if __name__ == "__main__":
    main()
