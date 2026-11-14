"""Authenticated file transport for a Ren'Py-owned render/input adapter."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import time
import uuid

from PIL import Image
from runtime_paths import resource_path


class RenpyBridgeError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.hint = ("后台模式保持不变；Ren'Py 适配需在下一次经控制台启动游戏时加载，不自行重启或切换前台。"
                     if code == "RENPY_BRIDGE_RESTART_REQUIRED" else
                     "保持后台模式，停止输入并重新 observe；无法取得有效画面时停止，不重放输入或切换前台。")


def is_renpy(executable):
    root = Path(executable).resolve().parent
    return (root / "renpy/__init__.py").is_file() and (root / "game").is_dir()


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


class RenpyBridge:
    def __init__(self, root, target, desktop):
        self.root = Path(root) / "renpy" / desktop
        self.target, self.desktop = target, desktop
        self.active = self.root / "active.json"

    def prepare(self):
        """Overlay only our search path. Never modify the game or its saves."""
        run = self.root / uuid.uuid4().hex
        overlay = run / "overlay"
        overlay.mkdir(parents=True)
        (run / "inbox").mkdir()
        (run / "outbox").mkdir()
        source = resource_path("renpy_bridge_agent.py")
        shutil.copyfile(source, overlay / "qqbot_adapter.py")
        script = (
            "init -999 python hide:\n"
            "    import os as _qq_os\n"
            "    import importlib.util as _qq_util\n"
            "    import renpy as _qq_engine\n"
            "    _qq_source = _qq_os.environ.get('QQBOT_RENPY_ADAPTER')\n"
            "    _qq_config = _qq_os.environ.get('QQBOT_RENPY_CONFIG')\n"
            "    if _qq_source and _qq_config:\n"
            "        _qq_spec = _qq_util.spec_from_file_location('_qqbot_render_adapter', _qq_source)\n"
            "        _qq_module = _qq_util.module_from_spec(_qq_spec)\n"
            "        _qq_spec.loader.exec_module(_qq_module)\n"
            "        _qq_module.start(_qq_engine, _qq_config)\n"
        )
        (overlay / "zz_qqbot_render_adapter.rpy").write_text(script, encoding="utf-8")
        config = {"version": 1, "token": secrets.token_urlsafe(48), "target_id": self.target["id"],
                  "desktop": self.desktop, "executable": str(Path(self.target["executable"]).resolve()),
                  "run": str(run.resolve()), "created": time.time()}
        config_path = run / "config.json"
        write_json(config_path, config)
        write_json(self.active, {"config": str(config_path.resolve())})
        environment = os.environ.copy()
        existing = environment.get("RENPY_SEARCHPATH", "")
        environment.update(QQBOT_RENPY_ADAPTER=str((overlay / "qqbot_adapter.py").resolve()),
                           QQBOT_RENPY_CONFIG=str(config_path.resolve()),
                           RENPY_SEARCHPATH=str(overlay.resolve()) + ("::" + existing if existing else ""))
        return environment

    def config(self):
        if not self.active.is_file():
            return None
        config_path = Path(json.loads(self.active.read_text(encoding="utf-8"))["config"]).resolve()
        if not config_path.is_relative_to(self.root.resolve()):
            raise RenpyBridgeError("RENPY_BRIDGE_INVALID", "Ren'Py 适配配置路径不匹配。")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if (config["target_id"] != self.target["id"] or config["desktop"] != self.desktop
                or config["executable"] != str(Path(self.target["executable"]).resolve())
                or Path(config["run"]).resolve() != config_path.parent):
            raise RenpyBridgeError("RENPY_BRIDGE_INVALID", "Ren'Py 适配不属于当前游戏。")
        return config

    def ready(self, window):
        config = self.config()
        if not config:
            return False
        path = Path(config["run"]) / "ready.json"
        if not path.is_file():
            return False
        ready = json.loads(path.read_text(encoding="utf-8"))
        return ready.get("pid") == window["pid"] and secrets.compare_digest(ready.get("token", ""), config["token"])

    def call(self, window, operation, params=None, *, timeout=5):
        if not self.ready(window):
            raise RenpyBridgeError("RENPY_BRIDGE_RESTART_REQUIRED", "当前 Ren'Py 游戏未加载原生取图适配；需在允许重启后经控制台重新启动。")
        config = self.config()
        run = Path(config["run"])
        identity = uuid.uuid4().hex
        request = run / "inbox" / (identity + ".json")
        response = run / "outbox" / (identity + ".json")
        png = response.with_suffix(".png")
        created = time.time()
        write_json(request, {"id": identity, "token": config["token"], "pid": window["pid"],
                             "operation": operation, "params": params or {}, "created": created,
                             "expires": created + min(timeout, 5)})
        deadline = time.monotonic() + timeout
        try:
            while not response.is_file():
                if time.monotonic() >= deadline:
                    raise RenpyBridgeError("RENPY_BRIDGE_TIMEOUT", "Ren'Py 未及时返回操作结果；不重放输入，请先重新观察。")
                time.sleep(.025)
            result = json.loads(response.read_text(encoding="utf-8"))
            if (result.get("id") != identity or result.get("pid") != window["pid"]
                    or not secrets.compare_digest(result.get("token", ""), config["token"])):
                raise RenpyBridgeError("RENPY_BRIDGE_INVALID", "Ren'Py 适配返回的进程或凭据不匹配。")
            if not result.get("ok"):
                raise RenpyBridgeError(result.get("code", "RENPY_BRIDGE_FAILED"), result.get("error", "Ren'Py 操作失败。"))
            if operation == "capture":
                if not png.is_file() or png.stat().st_size > 32 * 1024 * 1024:
                    raise RenpyBridgeError("RENPY_FRAME_INVALID", "Ren'Py 画面缺失或过大。")
                raw = png.read_bytes()
                if hashlib.sha256(raw).hexdigest() != result.get("sha256"):
                    raise RenpyBridgeError("RENPY_FRAME_INVALID", "Ren'Py 画面校验失败。")
                with Image.open(png) as image:
                    if image.format != "PNG" or not all(0 < extent <= 8192 for extent in image.size):
                        raise RenpyBridgeError("RENPY_FRAME_INVALID", "Ren'Py 画面格式或尺寸无效。")
                    picture = image.convert("RGB")
                # The engine frame has no title bar. Reject aspect changes
                # instead of stretching a letterboxed viewport into bad clicks.
                width, height = window["size"]
                if abs(picture.width / picture.height - width / height) > .01:
                    raise RenpyBridgeError("RENPY_VIEWPORT_CHANGED", "游戏画面与客户区比例不同，请先调整窗口后重新观察。")
                if picture.size != (width, height):
                    picture = picture.resize((width, height), Image.Resampling.LANCZOS)
                if all(low == high for low, high in picture.getextrema()):
                    raise RenpyBridgeError("BACKGROUND_CAPTURE_BLANK", "游戏渲染器尚未输出有效画面，请稍后观察。")
                if isinstance(result.get("game_reading"), dict):
                    picture.info["game_reading"] = result["game_reading"]
                return picture
            return result
        finally:
            request.unlink(missing_ok=True)
            response.unlink(missing_ok=True)
            png.unlink(missing_ok=True)
