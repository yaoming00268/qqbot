"""A bounded process input + frame loop shared by MCP and the CLI."""
from __future__ import annotations
from persona_isolation import context_persona

import json
import hashlib
import math
from pathlib import Path
import time
from contextlib import nullcontext

from console_tools.files import file_error
from sandbox_runtime import SandboxRuntime
from vm_protocol import file_lock


class DesktopError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.hint = "使用 desktop screenshot 取得新画面；按键或点击失败时不要换观察编号重放原动作。"


def frame_receipt(frame, session_id):
    """Keep transport internals in the audit database, not every model turn."""
    return {"ok": True, "state": "ready", "session_id": session_id,
            "target_id": frame.get("target_id", ""), "pid": frame["pid"],
            "title": frame.get("title", ""), "size": frame["size"],
            "coordinate_space": "pixels", "observation_id": frame["id"], "image_path": frame["image_path"],
            "direction_revision": frame.get("direction_revision", 0),
            **{key: frame[key] for key in ("page_url", "input_focus", "input_fields", "class_name", "capture_method",
                                         "capture_attempts", "window_recovery", "semantic") if key in frame}}


class DesktopControl:
    def __init__(self, broker, token):
        self.broker, self.token = broker, token
        self.context = broker.context(token)
        cfg = self.context.get("sandbox_config") or {}
        if not cfg.get("enabled"):
            raise DesktopError("DESKTOP_DISABLED", "当前上下文未启用后台操控。")
        SandboxRuntime.require_owner(self.context)
        self.runtime = SandboxRuntime(cfg["directory"], max_actions=cfg.get("max_actions", 0),
                                      vm_config=cfg.get("vm"), mode=cfg.get("mode", "background"))

    def session(self, session_id="", target_id="", pid=None):
        bound = self.context.get("sandbox_session_id")
        if bound and session_id and bound != session_id:
            raise PermissionError("当前工具已绑定另一游玩会话。")
        session_id = bound or session_id
        if not session_id:
            with self.runtime.connect() as db:
                rows = db.execute("SELECT s.id FROM sessions s JOIN games g ON g.id=s.target "
                                  "WHERE s.namespace=? AND s.scope=? AND s.owner=? AND g.persona=? AND s.state='running' "
                                  + ("AND s.target=? " if target_id else "") + "ORDER BY s.created DESC",
                                  (self.context["namespace"], self.context["scope"], self.context["sender_id"], context_persona(self.context))
                                  + ((target_id,) if target_id else ())).fetchall()
                if pid is not None:
                    candidates = []
                    for row in rows:
                        latest = db.execute("SELECT data FROM observations WHERE session=? ORDER BY rowid DESC LIMIT 1", (row["id"],)).fetchone()
                        if latest and json.loads(latest["data"]).get("pid") == pid:
                            candidates.append(row)
                    rows = candidates
            if len(rows) != 1:
                raise DesktopError("DESKTOP_TARGET_REQUIRED", "没有唯一运行任务；先用 desktop targets 选择 session_id。")
            session_id = rows[0]["id"]
        row, target = self.runtime._active(self.context, session_id)
        if target_id and target_id != target["id"]:
            raise PermissionError("所选进程不属于当前游玩目标。")
        request = self.broker.record(row["request_id"])
        if not request or request["state"] != "running":
            raise DesktopError("DESKTOP_STOPPED", "当前游玩请求已结束或停止，未继续输入。")
        return row, target

    def targets(self):
        with self.runtime.connect() as db:
            rows = db.execute("SELECT s.id,s.target FROM sessions s JOIN games g ON g.id=s.target "
                              "WHERE s.namespace=? AND s.scope=? AND s.owner=? AND g.persona=? AND s.state='running'",
                              (self.context["namespace"], self.context["scope"], self.context["sender_id"], context_persona(self.context))).fetchall()
        result = []
        for row in rows:
            if self.context.get("sandbox_session_id") and row["id"] != self.context["sandbox_session_id"]:
                continue
            try:
                _, target = self.session(row["id"])
                with self.runtime.connect() as db:
                    latest = db.execute("SELECT data FROM observations WHERE session=? ORDER BY rowid DESC LIMIT 1", (row["id"],)).fetchone()
                frame = json.loads(latest["data"]) if latest else {}
                result.append({"session_id": row["id"], "target_id": target["id"], "title": frame.get("title") or target["title"],
                               "pid": frame.get("pid"), "size": frame.get("size"), "state": "running"})
            except ValueError:
                continue
        return {"ok": True, "targets": result, "hint": "pid 来自最近观察；screenshot 会重新核对进程身份，不启动或重启程序。"}

    @staticmethod
    def check_pid(frame, pid):
        if pid is not None and (type(pid) is not int or pid <= 0 or frame["pid"] != pid):
            raise DesktopError("DESKTOP_PROCESS_CHANGED", "所选 PID 与观察画面不一致，未发送输入。")

    def run(self, operation, *, session_id="", target_id="", pid=None, observation_id="", key="",
            x=None, y=None, button="left", coordinate_space="pixels", wait_ms=None, text="", delta=0, url="",
            max_steps=None, element_id="", _lock_held=False):
        if operation not in ("targets", "screenshot", "inspect"):
            self.broker.require_effect_context(self.token)
        if operation == "targets":
            return self.targets()
        if operation == "read":
            from galgame_reading import DialogueReader
            return DialogueReader(self).run(session_id=session_id, target_id=target_id, pid=pid,
                observation_id=observation_id, max_steps=max_steps, key=key or "enter")
        if operation not in ("screenshot", "inspect", "invoke", "fill", "select", "key", "click", "type", "scroll", "navigate"):
            raise DesktopError("DESKTOP_OPERATION_INVALID", "支持 targets/screenshot/key/click/type/scroll/navigate。")
        if wait_ms is None:
            wait_ms = 0 if operation == "screenshot" else 150
        if type(wait_ms) not in (int, float) or not math.isfinite(wait_ms) or not 0 <= wait_ms <= 2000:
            raise DesktopError("DESKTOP_WAIT_INVALID", "wait_ms 必须在 0 到 2000 毫秒内。")
        row, target = self.session(session_id, target_id, pid)
        sid = row["id"]
        # One action plus its resulting frame is serialized across MCP/CLI
        # workers. The underlying window backend also retains its own lock.
        with nullcontext() if _lock_held else file_lock(self.runtime.root / "sessions" / sid / "desktop.lock", timeout=12):
            self.session(sid, target_id)
            if operation in ("screenshot", "inspect"):
                if wait_ms:
                    time.sleep(wait_ms / 1000)
                self.session(sid, target_id)
                frame = self.broker.sandbox(self.token, "observe", session_id=sid)
                self.check_pid(frame, pid)
                if operation == "inspect":
                    frame = self.runtime.inspect(self.context, sid, frame["id"])
                return frame_receipt(frame, sid)
            with self.runtime.connect() as db:
                observed = db.execute("SELECT data,used FROM observations WHERE id=? AND session=?", (observation_id, sid)).fetchone()
                latest = db.execute("SELECT id FROM observations WHERE session=? ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
            if not observed:
                raise DesktopError("DESKTOP_OBSERVATION_REQUIRED", "请先 screenshot，再传返回的 observation_id。")
            if not observed["used"] and latest["id"] != observation_id:
                raise DesktopError("DESKTOP_OBSERVATION_STALE", "已有更新画面，未使用旧观察发送输入。")
            frame = json.loads(observed["data"])
            self.check_pid(frame, pid)
            if operation in ("click", "scroll"):
                if coordinate_space not in ("pixels", "normalized"):
                    raise DesktopError("DESKTOP_COORDINATES_INVALID", "coordinate_space 必须为 pixels 或 normalized。")
                width, height = frame["size"]
                if operation == "scroll" and x is None and y is None:
                    x, y = (width / 2, height / 2) if coordinate_space == "pixels" else (.5, .5)
                if (type(x) not in (int, float) or type(y) not in (int, float)
                        or not math.isfinite(x) or not math.isfinite(y)
                        or not 0 <= x < (width if coordinate_space == "pixels" else 1)
                        or not 0 <= y < (height if coordinate_space == "pixels" else 1)):
                    raise DesktopError("DESKTOP_COORDINATES_INVALID", "鼠标坐标超出当前图片。")
                if coordinate_space == "pixels":
                    # Aim inside the pixel, so floating-point normalization
                    # cannot round an exact pixel down to its neighbour.
                    x, y = (int(x) + .5) / width, (int(y) + .5) / height
            from game_window import GameWindow
            extra = {"text": text, "delta": delta, "url": url} if operation in ("type", "scroll", "navigate") else {}
            if operation in ("invoke", "fill", "select"):
                extra = {"text": text, "element_id": element_id}
            GameWindow.validate_action(operation, key=key, x=x, y=y, button=button,
                                       software=target["kind"] == "software", **extra)
            try:
                action = self.broker.sandbox(self.token, operation, session_id=sid, observation_id=observation_id,
                                             key=key, x=x, y=y, button=button, **extra)
            except (OSError, ValueError, KeyError, TypeError) as error:
                if getattr(error, "code", "") == "ACTIVITY_DIRECTION_CHANGED":
                    return {**file_error(error), "state": "redirected", "input_posted": False}
                return {**file_error(error), "state": "unknown", "input_posted": None,
                        "hint": "未能确认输入结果；只调用 screenshot 重新观察，不换观察编号重放原动作。"}
            if action["state"] == "rejected" and action.get("input_posted") is False:
                rejected = {**action, "ok": False}
                try:
                    after = self.broker.sandbox(self.token, "observe", session_id=sid)
                    self.check_pid(after, pid)
                    return {**frame_receipt(after, sid), **rejected}
                except (OSError, ValueError, KeyError, TypeError):
                    return {**rejected, "hint": rejected.get("hint", "") +
                            " 本次新截图未取得，请先 screenshot 核对目标。"}
            if action["state"] != "posted":
                return {"ok": False, "state": action["state"], "action_id": action["action_id"],
                        "input_posted": None, "code": "DESKTOP_INPUT_UNKNOWN", "error": "输入结果不明，不自动重放。",
                        "hint": "只调用 screenshot 查看当前画面，不换观察编号重复原动作。"}
            # Repeated calls with the same observation/action return the same
            # frame when available, never a second key or a second click.
            with self.runtime.connect() as db:
                cached = db.execute("SELECT data FROM observations WHERE session=? AND json_extract(data,'$.after_action')=? "
                                    "ORDER BY rowid DESC LIMIT 1", (sid, action["action_id"])).fetchone()
            if cached:
                cached_frame = json.loads(cached["data"])
                image = Path(cached_frame["image_path"])
                if image.is_file() and hashlib.sha256(image.read_bytes()).hexdigest() == cached_frame["sha256"]:
                    try:
                        self.check_pid(cached_frame, pid)
                    except DesktopError as error:
                        return {**file_error(error), "state": "posted", "input_posted": True,
                                "action_id": action["action_id"], "replayed": False,
                                "hint": "输入已投递，后续窗口 PID 与指定目标不同；请先 screenshot 核对，禁止重发原输入。"}
                    return {**frame_receipt(cached_frame, sid), "state": "posted", "input_posted": True,
                            "action_id": action["action_id"], "replayed": False,
                            "frame_changed": cached_frame["sha256"] != frame["sha256"],
                            "window_changed": any(cached_frame.get(key) != frame.get(key) for key in ("hwnd", "pid", "process_started"))}
            try:
                if wait_ms:
                    time.sleep(wait_ms / 1000)
                self.session(sid, target_id)
                after = self.broker.sandbox(self.token, "observe", session_id=sid)
                self.check_pid(after, pid)
                return {**frame_receipt(after, sid), "state": "posted", "input_posted": True, "action_id": action["action_id"],
                        "frame_changed": after["sha256"] != frame["sha256"],
                        "window_changed": any(after.get(key) != frame.get(key) for key in ("hwnd", "pid", "process_started"))}
            except (OSError, ValueError, KeyError, TypeError) as error:
                return {**file_error(error), "state": "posted", "input_posted": True, "action_id": action["action_id"],
                        "hint": "输入已投递，后续截图失败；只调用 screenshot 重新观察，禁止重发原输入。"}
