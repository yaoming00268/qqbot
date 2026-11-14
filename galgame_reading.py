"""Local, audited dialogue reading. Only exceptional frames go to vision.

OCR and engine text are untrusted fictional content. They never authorize an
action; the usual session, direction, observation and input guards still apply.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
import unicodedata

from PIL import Image, ImageChops, ImageFilter, ImageStat

from config import GalgameReadingConfig
from vm_protocol import file_lock


_ocr_engine = None
_ocr_lock = threading.Lock()
_QUICK = {"skip", "auto", "save", "load", "qsave", "qload", "menu", "prefs", "settings", "history", "back",
          "快进", "自动", "存档", "读档", "设置", "回想", "历史", "菜单", "スキップ", "オート", "セーブ", "ロード"}
_MENU = re.compile(r"^(?:start|new\s*game|continue|load(?:\s*game)?|save(?:\s*game)?|settings|preferences|quit|exit|"
                   r"开始(?:游戏)?|新(?:的)?游戏|继续(?:游戏)?|读取(?:存档)?|保存(?:存档)?|读档|存档|"
                   r"返回(?:标题|主菜单)|结束游戏|环境设置|选项|选择|请选择.*|選択.*|はじめから|つづきから|"
                   r"コンフィグ|タイトル.*|終了)$", re.I)
_OPTION = re.compile(r"^\s*(?:[1-9][.、)）:：]|[①-⑳]|[A-D][.、)）]|[●○□■▶►])")


def normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text))).strip()


def local_ocr(path):
    """Bundled CPU models; lazy singleton, no download or cloud request."""
    global _ocr_engine
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    with Image.open(path) as original:
        picture = original.convert("RGB")
        picture.thumbnail((1600, 1000))
        width, height = picture.size
        pixels = np.asarray(picture)[:, :, ::-1].copy()
    with _ocr_lock:
        if _ocr_engine is None:
            _ocr_engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1, print_verbose=False)
        detected, _ = _ocr_engine(pixels, use_cls=False)
    lines = []
    for box, text, score in detected or []:
        xs, ys = [float(point[0]) / width for point in box], [float(point[1]) / height for point in box]
        lines.append({"text": str(text).strip(), "confidence": float(score),
                      "box": [min(xs), min(ys), max(xs), max(ys)]})
    return sorted(lines, key=lambda line: (round(line["box"][1], 2), line["box"][0]))


def read_frame(frame, settings):
    native = frame.get("game_reading")
    if isinstance(native, dict) and native.get("source") == "renpy":
        kind = native.get("kind", "unknown")
        text = str(native.get("text", "")).strip()[:6000]
        reason = "" if kind == "dialogue" and text else kind if kind in ("choice", "menu") else "no_dialogue"
        if native.get("truncated") or len(str(native.get("text", ""))) > 6000:
            reason = "text_too_long"
        return {"text": text, "source": "renpy", "confidence": 1.0, "reason": reason}
    try:
        lines = local_ocr(frame["image_path"])
    except Exception:
        # Missing dependency/model/inference errors must never become an excuse
        # to press on. The model can still inspect the original screenshot.
        return {"text": "", "source": "local_ocr", "confidence": 0, "reason": "ocr_unavailable"}
    dialogue, outside = [], []
    for line in lines:
        text = line["text"]
        if not text:
            continue
        left, top, right, bottom = line["box"]
        if top > .92 and normalized(text).casefold() in _QUICK:
            continue
        if top < settings.dialogue_top - .02:
            outside.append(line)
        else:
            dialogue.append(line)
    text = "\n".join(line["text"] for line in dialogue)
    confidence = min((line["confidence"] for line in dialogue), default=0)
    reason = ""
    if outside:
        reason = "choice_or_overlay"
    elif any(_OPTION.match(line["text"]) for line in dialogue):
        reason = "choice_or_overlay"
    elif len(dialogue) >= 2 and all(
            dialogue[index + 1]["box"][1] - dialogue[index]["box"][3] > .04
            for index in range(len(dialogue) - 1)):
        reason = "choice_or_overlay"
    elif any(_MENU.fullmatch(line["text"].strip()) for line in dialogue):
        reason = "menu"
    elif len(re.sub(r"[\W_]", "", text)) < 6:
        reason = "no_dialogue"
    elif not math.isfinite(confidence) or confidence < .85:
        reason = "low_confidence"
    # Uncertain/central text is included for interpretation, never silently
    # dropped because it falls outside the presumed dialogue box.
    if outside:
        text = "\n".join(line["text"] for line in lines)
    if len(text) > 6000:
        reason = "text_too_long"
    return {"text": text[:6000], "source": "local_ocr", "confidence": round(confidence, 3), "reason": reason}


def scene_change(first, second, dialogue_top):
    """Compare above the dialogue box; blur filters tiny animation/cursor noise."""
    def sample(path):
        with Image.open(path) as image:
            upper = image.convert("RGB").crop((0, 0, image.width, max(1, int(image.height * dialogue_top))))
            return upper.resize((96, 54)).filter(ImageFilter.GaussianBlur(1.2))
    difference = ImageChops.difference(sample(first), sample(second))
    return sum(ImageStat.Stat(difference).mean) / (3 * 255)


class DialogueReader:
    def __init__(self, control):
        self.control, self.runtime = control, control.runtime
        self.settings = GalgameReadingConfig.model_validate(
            (control.context.get("sandbox_config") or {}).get("galgame_reading") or {})
        self.context, self.broker, self.token = control.context, control.broker, control.token
        self._analyses = {}

    def _reading(self, frame):
        cache_key = (frame["sha256"], json.dumps(frame.get("game_reading"), sort_keys=True))
        if cache_key not in self._analyses:
            self._analyses[cache_key] = read_frame(frame, self.settings)
        reading = self._analyses[cache_key]
        # Keep local text evidence even if a later frame/input fails.
        with self.runtime.connect() as db:
            row = db.execute("SELECT data FROM observations WHERE id=?", (frame["id"],)).fetchone()
            if row:
                data = json.loads(row["data"])
                data["local_reading"] = reading
                db.execute("UPDATE observations SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), frame["id"]))
        return reading

    def _frame(self, sid, oid):
        with self.runtime.connect() as db:
            row = db.execute("SELECT * FROM observations WHERE session=? AND id=?", (sid, oid)).fetchone()
        if not row:
            raise ValueError("请先 screenshot，并传当前任务的 observation_id。")
        frame = json.loads(row["data"])
        if hashlib.sha256(Path(frame["image_path"]).read_bytes()).hexdigest() != frame["sha256"]:
            raise ValueError("观察截图已变更，请重新 screenshot。")
        return row, frame

    def _capture(self, sid, pid):
        self.control.session(sid)
        frame = self.broker.sandbox(self.token, "observe", session_id=sid)
        self.control.check_pid(frame, pid)
        return frame

    def _stable(self, sid, frame, pid, anchor, deadline):
        """Wait for two matching text samples. Never use an input to finish text."""
        seen = self._reading(frame)
        for _ in range(4):
            if seen["reason"]:
                return frame, seen
            if time.monotonic() + self.settings.settle_ms / 1000 > deadline:
                return frame, {**seen, "reason": "time_budget"}
            time.sleep(self.settings.settle_ms / 1000)
            after = self._capture(sid, pid)
            if any(after.get(name) != frame.get(name) for name in ("pid", "hwnd", "process_started", "size")):
                return after, {**self._reading(after), "reason": "window_changed"}
            current = self._reading(after)
            if current["reason"]:
                return after, current
            if scene_change(anchor["image_path"], after["image_path"], self.settings.dialogue_top) >= self.settings.scene_threshold:
                return after, {**current, "reason": "scene_changed"}
            if normalized(current["text"]) == normalized(seen["text"]):
                return after, current
            frame, seen = after, current
        return frame, {**seen, "reason": "text_unstable"}

    def run(self, *, observation_id, session_id="", target_id="", pid=None, max_steps=None, key="enter"):
        if not self.settings.enabled:
            raise ValueError("本地对白阅读已关闭；使用 screenshot/key/click。")
        if max_steps is None:
            max_steps = self.settings.batch_lines
        if type(max_steps) is not int or not 0 <= max_steps <= 20 or key not in ("enter", "space"):
            raise ValueError("max_steps 必须为 0–20；对白推进只支持 enter/space。")
        row, target = self.control.session(session_id, target_id, pid)
        if target["kind"] != "game":
            raise ValueError("本地对白阅读只用于已授权游戏；通用软件继续使用单次截图操作。")
        sid = row["id"]
        identity = hashlib.sha256(json.dumps([sid, observation_id, bool(max_steps)]).encode()).hexdigest()
        with file_lock(self.runtime.root / "sessions" / sid / "desktop.lock", timeout=12):
            self.control.session(sid)
            observed, frame = self._frame(sid, observation_id)
            self.control.check_pid(frame, pid)
            revision = self.runtime.direction(self.context, sid)["revision"]
            if frame.get("direction_revision", 0) != revision:
                raise ValueError("观察属于旧阅读方向；请重新 screenshot。")
            with self.runtime.connect() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS reading_batches(id TEXT PRIMARY KEY,session TEXT,result TEXT);
                    CREATE TABLE IF NOT EXISTS reading_states(session TEXT PRIMARY KEY,data TEXT);
                """)
                cached = db.execute("SELECT result FROM reading_batches WHERE id=?", (identity,)).fetchone()
                if cached:
                    value = json.loads(cached["result"])
                    if value:
                        if value.get("image_required"):
                            try:
                                self._frame(sid, value["observation_id"])
                            except (OSError, ValueError):
                                return {"ok": False, "state": "unknown", "image_required": False,
                                        "stop_reason": "evidence_changed", "hint": "缓存图片不可用；请 screenshot，不重放输入。"}
                        return {**value, "replayed": False, "cached": True}
                    return {"ok": False, "state": "unknown", "advanced": None, "image_required": False,
                            "stop_reason": "interrupted", "hint": "上一批结果未确认；只 screenshot 重新观察，不能重放。"}
                latest = db.execute("SELECT id FROM observations WHERE session=? ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
                if observed["used"] or latest["id"] != observation_id or time.time() - observed["created"] > 60:
                    raise ValueError("截图已使用、过期或存在更新观察；请先 screenshot。")
                state_row = db.execute("SELECT data FROM reading_states WHERE session=?", (sid,)).fetchone()
                state = json.loads(state_row["data"]) if state_row else {}
                # Reserve before any input. A crash never restarts the batch.
                db.execute("INSERT INTO reading_batches VALUES(?,?,?)", (identity, sid, "{}"))
            anchor = frame
            if state.get("anchor") and state.get("revision") == revision:
                try:
                    _, anchor = self._frame(sid, state["anchor"])
                except (ValueError, OSError):
                    pass
            try:
                value, state = self._read(sid, frame, pid, anchor, state, max_steps, key)
            except Exception as error:
                # Known per-step receipts remain in the regular action ledger.
                # A partial batch is deliberately not resumable from its start.
                value = {"ok": False, "state": "unknown", "advanced": None, "image_required": False,
                         "stop_reason": getattr(error, "code", "reading_interrupted"),
                         "error": str(error), "hint": "先 screenshot 核对当前画面，不重放本批输入。"}
            with self.runtime.connect() as db:
                db.execute("UPDATE reading_batches SET result=? WHERE id=?", (json.dumps(value, ensure_ascii=False), identity))
                if state:
                    db.execute("INSERT OR REPLACE INTO reading_states VALUES(?,?)", (sid, json.dumps(state)))
            return value

    def _read(self, sid, frame, pid, anchor, state, max_steps, key):
        from console_tools.desktop import frame_receipt
        deadline, dialogue, advanced = time.monotonic() + 25, [], 0
        since_image = state.get("since_image", 0) if state.get("revision") == frame.get("direction_revision", 0) else 0
        previous_text, action = "", None
        last_emitted = state.get("last_text", "") if state.get("revision") == frame.get("direction_revision", 0) else ""
        reason = "batch_complete"
        while True:
            self.control.session(sid)
            if self.runtime.control(self.context, sid):
                reason = "stopped"
                break
            frame, reading = self._stable(sid, frame, pid, anchor, deadline)
            if reading["text"] and normalized(reading["text"]) != last_emitted:
                dialogue.append({"observation_id": frame["id"], "text": reading["text"],
                                 "source": reading["source"], "confidence": reading["confidence"]})
                last_emitted = normalized(reading["text"])
            if reading["reason"]:
                reason = reading["reason"]
                break
            if previous_text and normalized(reading["text"]) == previous_text:
                reason = "stalled"
                break
            if since_image >= self.settings.visual_interval:
                reason = "visual_checkpoint"
                break
            if advanced >= max_steps:
                reason = "inspect_complete" if max_steps == 0 else "batch_complete"
                break
            if sum(len(item["text"]) for item in dialogue) >= 10000:
                reason = "text_budget"
                break
            if time.monotonic() >= deadline:
                reason = "time_budget"
                break
            if self.runtime.max_actions > 0 and self.runtime.session(self.context, sid)["actions"] >= self.runtime.max_actions:
                reason = "action_limit"
                break
            previous_text = normalized(reading["text"])
            action = self.control.run("key", session_id=sid, pid=pid, observation_id=frame["id"],
                                      key=key, wait_ms=self.settings.settle_ms, _lock_held=True)
            if action.get("input_posted") is True:
                advanced += 1
                since_image += 1
            if action.get("observation_id"):
                _, frame = self._frame(sid, action["observation_id"])
            if not action.get("ok") or not action.get("observation_id"):
                reason = "input_or_capture_failed"
                break
            if action.get("window_changed"):
                reason = "window_changed"
                break
        image_required = reason not in ("batch_complete", "inspect_complete", "text_budget")
        # Text mode still leaves the original PNG on disk for notes and manual
        # inspection; the MCP layer omits the image block when it is unneeded.
        result = {**frame_receipt(frame, sid), "state": "read", "dialogue": dialogue,
                  "advanced": advanced, "stop_reason": reason, "image_required": image_required,
                  "hint": ("查看返回画面后决定；禁止自动选项或重放未确认输入。" if image_required else
                           "逐句阅读 dialogue 并保存剧情/感受；继续用本次 observation_id。需要细看时用 screenshot。")}
        if action and not action.get("ok"):
            result.update(ok=False, last_action=action)
            if not action.get("observation_id"):
                # Do not present the consumed pre-input picture as a new frame.
                result.update(image_required=False, hint="输入或后续截图未确认；先 screenshot 核对当前画面，不重放输入。")
        return result, {"anchor": frame["id"] if image_required else anchor["id"],
                        "since_image": 0 if image_required else since_image,
                        "revision": frame.get("direction_revision", 0), "last_text": last_emitted}
