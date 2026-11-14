"""Runs inside Ren'Py. Only frames and bounded game events, no shell/network."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import re
import threading
import time


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


class Adapter:
    def __init__(self, renpy, config):
        self.renpy, self.config = renpy, config
        self.root = Path(config["run"])
        self.pid = os.getpid()
        self.stopped = threading.Event()

    def dialogue_snapshot(self):
        """Read only current public screen variables, never future script/history."""
        get_screen = getattr(self.renpy.exports, "get_screen", None)
        if not callable(get_screen):
            return None
        try:
            for name in ("main_menu", "game_menu", "save", "load", "preferences", "confirm"):
                if get_screen(name) is not None:
                    return {"source": "renpy", "kind": "menu", "text": ""}
            choice = get_screen("choice")
            if choice is not None:
                items = getattr(choice, "scope", {}).get("items", [])
                labels = [self._plain(getattr(item, "caption", "")) for item in items[:20]]
                return {"source": "renpy", "kind": "choice", "text": "\n".join(labels)[:6000]}
            say = get_screen("say")
            if say is None:
                return {"source": "renpy", "kind": "unknown", "text": ""}
            scope = getattr(say, "scope", {})
            text, who = self._plain(scope.get("what", "")), self._plain(scope.get("who", ""))
            combined = (who + "：" if who else "") + text
            return {"source": "renpy", "kind": "dialogue" if text else "unknown",
                    "text": combined[:6000], **({"truncated": True} if len(combined) > 6000 else {})}
        except Exception:
            # A custom screen does not break screenshots or input support.
            return None

    @staticmethod
    def _plain(value):
        return re.sub(r"\{[^{}]*\}", "", str(value or "")).strip()

    def validate(self, request):
        if not isinstance(request, dict):
            raise ValueError("Invalid adapter request")
        identity = request.get("id", "")
        if (not isinstance(identity, str) or len(identity) != 32 or any(char not in "0123456789abcdef" for char in identity)
                or request.get("pid") != self.pid or not isinstance(request.get("token"), str)
                or not secrets.compare_digest(request["token"], self.config["token"])):
            raise ValueError("Invalid adapter identity")
        created, expires = request.get("created"), request.get("expires")
        if (type(created) not in (int, float) or type(expires) not in (int, float)
                or not math.isfinite(created) or not math.isfinite(expires)
                or not created <= time.time() < expires <= created + 5):
            raise ValueError("Expired adapter request")
        operation, params = request.get("operation"), request.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("Invalid adapter parameters")
        if operation == "key":
            if params.get("key") not in ("enter", "space", "up", "down", "left", "right", "escape", "s", "l", "tab", "backspace", "f5"):
                raise ValueError("Invalid adapter key")
        elif operation == "click":
            if (type(params.get("x")) not in (int, float) or type(params.get("y")) not in (int, float)
                    or not 0 <= params["x"] < 1 or not 0 <= params["y"] < 1
                    or params.get("button") not in ("left", "right", "middle")):
                raise ValueError("Invalid adapter click")
        elif operation != "capture":
            raise ValueError("Unsupported adapter operation")

    def perform(self, request):
        identity = request["id"]
        response = self.root / "outbox" / (identity + ".json")
        result = {"id": identity, "pid": self.pid, "token": self.config["token"]}
        try:
            # Recheck expiry after scheduling onto the engine main thread.
            self.validate(request)
            operation, params = request["operation"], request.get("params") or {}
            if not self.renpy.game.interface or not self.renpy.game.interface.started:
                raise ValueError("The renderer is still loading")
            if operation == "capture":
                raw = self.renpy.exports.screenshot_to_bytes(None)
                if not raw or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("The renderer did not return a PNG")
                image = response.with_suffix(".png")
                temporary = image.with_suffix(".tmp")
                temporary.write_bytes(raw)
                temporary.replace(image)
                result.update(sha256=hashlib.sha256(raw).hexdigest())
                reading = self.dialogue_snapshot()
                if reading is not None:
                    result["game_reading"] = reading
            else:
                pygame = self.renpy.pygame
                # Engine events affect only this game's event queue, including
                # focus flags. They never acquire Windows input focus.
                pygame.event.post(pygame.event.Event(pygame.ACTIVEEVENT, gain=1, state=6))
                if operation == "key":
                    key = params["key"]
                    name = {"enter": "RETURN", "space": "SPACE", "escape": "ESCAPE", "tab": "TAB", "backspace": "BACKSPACE", "f5": "F5"}.get(key, key if len(key) == 1 else key.upper())
                    code = getattr(pygame, "K_" + name)
                    text = {"enter": "\r", "space": " ", "tab": "\t", "backspace": "\b", "s": "s", "l": "l"}.get(params["key"], "")
                    try:
                        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=code, mod=0, unicode=text, repeat=False))
                    finally:
                        pygame.event.post(pygame.event.Event(pygame.KEYUP, key=code, mod=0, unicode=text, repeat=False))
                else:
                    width, height = self.renpy.exports.get_physical_size()
                    point = (int(params["x"] * width), int(params["y"] * height))
                    button = {"left": 1, "middle": 2, "right": 3}[params["button"]]
                    pygame.event.post(pygame.event.Event(pygame.MOUSEMOTION, pos=point, rel=(0, 0), buttons=(0, 0, 0)))
                    try:
                        pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=point, button=button))
                    finally:
                        pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONUP, pos=point, button=button))
            result["ok"] = True
        except Exception as error:
            result.update(ok=False, code="RENPY_RENDER_NOT_READY", error=str(error))
        try:
            write_json(response, result)
        except OSError:
            # A mailbox failure must not propagate into the game's event loop.
            pass

    def serve(self):
        while not self.stopped.is_set():
            for path in sorted((self.root / "inbox").glob("*.json"))[:8]:
                try:
                    if path.stat().st_size > 4096:
                        path.unlink()
                        continue
                    request = json.loads(path.read_text(encoding="utf-8"))
                    self.validate(request)
                    if path.stem != request["id"]:
                        raise ValueError("Adapter filename mismatch")
                    if not getattr(self.renpy.game, "interface", None) or not self.renpy.game.interface.started:
                        # Imports/scripts may still be loading. Keep the request
                        # until the engine accepts main-thread scheduling.
                        continue
                    # Claim before scheduling; a request is never queued twice.
                    path.unlink()
                    self.renpy.exports.invoke_in_main_thread(self.perform, request)
                except (OSError, ValueError, TypeError):
                    path.unlink(missing_ok=True)
                except Exception as error:
                    write_json(self.root / "outbox" / (request["id"] + ".json"), {
                        "id": request["id"], "pid": self.pid, "token": self.config["token"],
                        "ok": False, "code": "RENPY_RENDER_NOT_READY", "error": str(error)})
                    path.unlink(missing_ok=True)
            time.sleep(.05)


def start(renpy, config_path):
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if config.get("version") != 1 or Path(config["run"]).resolve() != Path(config_path).resolve().parent:
        raise ValueError("Invalid Ren'Py adapter configuration")
    adapter = Adapter(renpy, config)
    write_json(adapter.root / "ready.json", {"pid": adapter.pid, "token": config["token"]})
    threading.Thread(target=adapter.serve, name="qqbot-renpy-frames", daemon=True).start()
