"""Read-only local QQ screenshots. Models only receive the saved image file."""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import time
import uuid

from PIL import Image, ImageStat

from media_downloader import MediaError

logger = logging.getLogger("QQCapture")


@dataclass(frozen=True)
class QQWindow:
    hwnd: int
    pid: int
    title: str
    rect: tuple
    owner: int = 0
    executable: str = "QQ.exe"


@dataclass(frozen=True)
class AccessibleNode:
    name: str = ""
    automation_id: str = ""
    control_type: str = ""
    rect: tuple = (0, 0, 0, 0)
    children: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class QQCaptureTarget:
    window: QQWindow
    box: tuple
    account_id: str
    conversation_id: str
    conversation_kind: str
    message_id: str
    image_index: int
    mode: str
    evidence: str


def checked_box(value, size):
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(type(v) not in (int, float) for v in value):
        raise MediaError("QQ 本地定位没有返回有效区域。")
    try:
        left, top, right, bottom = map(int, value)
    except (ValueError, OverflowError):
        raise MediaError("QQ 本地定位区域无效。")
    if not (0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]):
        raise MediaError("QQ 本地定位区域超出了当前窗口。")
    return left, top, right, bottom


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _is_message(node):
    return node.control_type == "ListItemControl" or bool(re.search(
        r"(?:^|[\s:;/_-])(?:message|msg)(?:[_-]?id)?[:=_-]", node.automation_id, re.I))


def _headers(node):
    """Message bubbles/sidebar rows cannot supply the login or chat identity."""
    yield node
    for child in node.children:
        if not _is_message(child) and child.name not in ("聊天记录", "消息列表", "聊天消息"):
            yield from _headers(child)


def _id(node, kind):
    aliases = {"account": "account(?:[_-]?id)?|uin", "group": "group(?:[_-]?id)?",
               "private": "(?:friend|peer|user)(?:[_-]?id)?", "message": "(?:message|msg)(?:[_-]?id)?"}
    match = re.search(r"(?:^|[\s:;/])(?:" + aliases[kind] + r")[:=_-]([A-Za-z0-9_-]+)(?=$|[\s:;/])",
                      node.automation_id, re.I)
    if match:
        return match[1]
    if kind == "message":
        # User-authored bubble text can say "消息ID：..."; that is not metadata.
        return ""
    labels = {"account": r"(?:当前登录|当前|登录|我的|本机)(?:QQ号?|QQ号码|账号|帐号)",
              "group": r"(?:群号|群号码|群ID)", "private": r"(?:好友QQ号|好友账号|对方QQ号|用户ID)",
              "message": r"(?:消息ID|消息编号)"}
    match = re.fullmatch(labels[kind] + r"\s*[:：]\s*([A-Za-z0-9_-]+)", node.name.strip(), re.I)
    return match[1] if match else ""


def _local_box(node, window):
    left, top, right, bottom = node.rect
    return checked_box((left - window.rect[0], top - window.rect[1],
                        right - window.rect[0], bottom - window.rect[1]),
                       (window.rect[2] - window.rect[0], window.rect[3] - window.rect[1]))


def resolve_accessible_target(windows, snapshots, request, item, *, index=0):
    """Bind visible accessibility data to the original event without model guesses."""
    account = str(request.get("account_id") or "")
    conversation = str(request.get("conversation_id") or "")
    message = str(request.get("message_id") or "")
    scope = str(request.get("scope") or "")
    kind = request.get("conversation_kind") or ("group" if ":group:" in scope else "private" if ":private:" in scope else "")
    if not account or not conversation or not message or kind not in ("group", "private"):
        raise MediaError("QQ 本地截图缺少账号、会话类型或原消息标识，改用后台原图识别。")
    sticker = item.get("subType", item.get("sub_type", item.get("subtype"))) in (1, "1", "sticker") or item.get("type") in ("mface", "bface")
    matches, chats = [], []
    for window in windows:
        root = snapshots.get(window.hwnd)
        if root is None or Path(window.executable).name.casefold() != "qq.exe":
            continue
        headers = list(_headers(root))
        accounts = {_id(node, "account") for node in headers} - {""}
        if accounts != {account}:
            continue
        ids = {_id(node, kind) for node in headers}
        name = str(request.get("conversation_name") or "").strip()
        # An exact group name alone cannot distinguish two identically named groups.
        titled = bool(name and re.fullmatch(re.escape(name) + r"\s*[(（]\s*" + re.escape(conversation) + r"\s*[)）]", window.title.strip()))
        ids.discard("")
        if (ids and ids != {conversation}) or (not ids and not titled):
            continue
        candidates = []
        for node in _walk(root):
            if not _is_message(node):
                continue
            nodes = list(_walk(node))
            exact_ids = {_id(part, "message") for part in nodes} - {""}
            own_id = _id(node, "message")
            if own_id == message:
                candidates.append((node, "accessibility_message_id"))
                continue
            if exact_ids:
                continue
            # Older QQ versions may expose sender/time/body but omit message IDs.
            # Accept only a unique recent match inside one message container.
            stamp = float(request.get("timestamp") or 0)
            sender = str(request.get("sender_name") or "").strip()
            body = str(request.get("text") or "").strip()
            names = [part.name.strip() for part in nodes if part.name.strip()]
            clock = re.search(r"\b(\d{2}:\d{2})(?::\d{2})?\b", str(request.get("time_display") or ""))
            same_sender = sender and any(text == sender or text.startswith(sender + " ") for text in names)
            same_body = not body or body in names
            same_time = clock and any(re.search(r"(?<!\d)" + re.escape(clock[1]) + r"(?::\d{2})?(?!\d)", text) for text in names)
            if 0 <= time.time() - stamp <= 120 and same_sender and same_body and same_time:
                candidates.append((node, "unique_recent_sender_time_text"))
        if len(candidates) != 1:
            continue
        node, evidence = candidates[0]
        images = [part for part in _walk(node) if part.control_type == "ImageControl"]
        if index >= len(images):
            continue
        chats.append((window, evidence))
        if sticker:
            matches.append(QQCaptureTarget(window, _local_box(node, window), account, conversation,
                kind, message, index, "qq_chat_screenshot", evidence))
    if not sticker:
        # A pre-opened viewer needs an OS owner and a source filename/message ID
        # that link it to the exact chat. No window is opened or clicked here.
        original_name = Path(str(item.get("file") or item.get("filename") or "").replace("\\", "/")).name
        for chat, evidence in chats:
            for viewer in windows:
                if viewer.pid != chat.pid or viewer.owner != chat.hwnd or viewer.hwnd == chat.hwnd:
                    continue
                if Path(viewer.executable).name.casefold() != "qq.exe":
                    continue
                if not re.search(r"图片|照片|image|photo", viewer.title, re.I):
                    continue
                root = snapshots.get(viewer.hwnd)
                if root is None:
                    continue
                nodes = list(_walk(root))
                viewer_accounts = {_id(part, "account") for part in _headers(root)} - {""}
                if viewer_accounts and viewer_accounts != {account}:
                    continue
                message_link = message in {_id(part, "message") for part in nodes}
                file_link = bool(original_name and any(Path(part.name.replace("\\", "/")).name == original_name for part in nodes))
                if not message_link and not file_link:
                    continue
                images = [part for part in nodes if part.control_type == "ImageControl"]
                if len(images) == 1:
                    matches.append(QQCaptureTarget(viewer, _local_box(images[0], viewer), account, conversation,
                        kind, message, index, "qq_image_viewer_screenshot", evidence + ":owned_viewer_source"))
    if len(matches) != 1:
        raise MediaError("当前 QQ 可访问性信息无法唯一核对登录账号、目标聊天及原图片消息；未截图，改用后台原图识别。")
    return matches[0]


class Win32QQ:
    def __init__(self):
        if os.name != "nt":
            raise MediaError("QQ 窗口截图需要本机 Windows QQ。")
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.g = ctypes.WinDLL("gdi32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = (
            (self.u.GetWindowDC, [w.HWND], w.HDC),
            (self.u.ReleaseDC, [w.HWND, w.HDC], ctypes.c_int),
            (self.u.GetWindowRect, [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            (self.u.GetWindowThreadProcessId, [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            (self.u.GetWindow, [w.HWND, w.UINT], w.HWND),
            (self.u.IsWindowVisible, [w.HWND], w.BOOL),
            (self.u.IsIconic, [w.HWND], w.BOOL),
            (self.u.GetWindowTextW, [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.u.PrintWindow, [w.HWND, w.HDC, w.UINT], w.BOOL),
            (self.g.CreateCompatibleDC, [w.HDC], w.HDC),
            (self.g.CreateCompatibleBitmap, [w.HDC, ctypes.c_int, ctypes.c_int], w.HBITMAP),
            (self.g.SelectObject, [w.HDC, w.HGDIOBJ], w.HGDIOBJ),
            (self.g.DeleteObject, [w.HGDIOBJ], w.BOOL),
            (self.g.DeleteDC, [w.HDC], w.BOOL),
            (self.g.GetDIBits, [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT], ctypes.c_int),
            (self.k.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.k.QueryFullProcessImageNameW, [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.k.CloseHandle, [w.HANDLE], w.BOOL),
        )
        for func, args, result in signatures:
            func.argtypes, func.restype = args, result

    def windows(self, *, executable_root=None, include_minimized=False):
        result = []
        callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        @callback_type
        def visit(hwnd, _):
            if not self.u.IsWindowVisible(hwnd) or self.u.IsIconic(hwnd) and not include_minimized:
                return True
            pid = w.DWORD()
            self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = self.k.OpenProcess(0x1000, False, pid.value)
            if not process:
                return True
            try:
                executable, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
                if not self.k.QueryFullProcessImageNameW(process, 0, executable, ctypes.byref(size)):
                    return True
                if (not executable_root and Path(executable.value).name.casefold() != "qq.exe"
                        or executable_root and not Path(executable.value).resolve().is_relative_to(Path(executable_root).resolve())):
                    return True
            finally:
                self.k.CloseHandle(process)
            rect = w.RECT()
            if not self.u.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            if not self.u.IsIconic(hwnd) and (rect.right - rect.left < 100 or rect.bottom - rect.top < 100):
                return True
            title = ctypes.create_unicode_buffer(1024)
            self.u.GetWindowTextW(hwnd, title, len(title))
            result.append(QQWindow(int(hwnd), pid.value, title.value,
                (rect.left, rect.top, rect.right, rect.bottom), int(self.u.GetWindow(hwnd, 4) or 0), executable.value))
            return True
        self.u.EnumWindows(visit, 0)
        return result

    def locate_target(self, request, item, *, index=0):
        try:
            import uiautomation as automation
        except ImportError:
            raise MediaError("本机没有 UIAutomation，QQBot 无法可靠定位原图片，改用后台原图识别。")
        windows = self.windows()
        if not windows:
            raise MediaError("没有可见的本机 QQ 窗口，改用后台原图识别。")
        deadline = time.monotonic() + 2
        remaining = 600
        def snapshot(control, depth=0):
            nonlocal remaining
            if remaining <= 0 or depth > 12 or time.monotonic() > deadline:
                raise MediaError("QQ 可访问性读取达到本地检查限度，改用后台原图识别。")
            remaining -= 1
            rect = control.BoundingRectangle
            children = tuple(snapshot(child, depth + 1) for child in control.GetChildren())
            return AccessibleNode(str(control.Name or ""), str(control.AutomationId or ""),
                str(control.ControlTypeName or ""), (rect.left, rect.top, rect.right, rect.bottom), children)
        try:
            with automation.UIAutomationInitializerInThread():
                snapshots = {window.hwnd: snapshot(automation.ControlFromHandle(window.hwnd)) for window in windows}
        except MediaError:
            raise
        except Exception as error:
            raise MediaError("QQ UIAutomation 无法读取原图片定位信息，改用后台原图识别。") from error
        return resolve_accessible_target(windows, snapshots, request, item, index=index)

    def capture(self, window, *, executable_root=None):
        windows = self.windows(executable_root=executable_root) if executable_root else self.windows()
        current = next((item for item in windows if item.hwnd == window.hwnd and item.pid == window.pid), None)
        if current is None or current.rect != window.rect:
            raise MediaError("QQ 窗口已关闭、移动或不可见，改用后台原图识别。")
        width, height = window.rect[2] - window.rect[0], window.rect[3] - window.rect[1]
        src = self.u.GetWindowDC(window.hwnd)
        dc = self.g.CreateCompatibleDC(src)
        bitmap = self.g.CreateCompatibleBitmap(src, width, height)
        old = self.g.SelectObject(dc, bitmap)
        class Header(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                        ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD),
                        ("image_size", w.DWORD), ("x", w.LONG), ("y", w.LONG),
                        ("used", w.DWORD), ("important", w.DWORD)]
        try:
            if not self.u.PrintWindow(window.hwnd, dc, 2):
                raise MediaError("QQ 窗口截图失败。")
            header = Header(ctypes.sizeof(Header), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
            buffer = ctypes.create_string_buffer(width * height * 4)
            self.g.SelectObject(dc, old)
            if not self.g.GetDIBits(dc, bitmap, 0, height, buffer, ctypes.byref(header), 0):
                raise MediaError("QQ 截图像素读取失败。")
            picture = Image.frombytes("RGB", (width, height), buffer.raw, "raw", "BGRX")
            if max(ImageStat.Stat(picture.resize((64, 64))).stddev) < 2:
                raise MediaError("QQ 返回了空白截图。")
            return picture
        finally:
            self.g.SelectObject(dc, old)
            self.g.DeleteObject(bitmap)
            self.g.DeleteDC(dc)
            self.u.ReleaseDC(window.hwnd, src)


class QQCaptureService:
    def __init__(self, ai=None, *, backend=None, cache_dir="data/cache/images/qq_capture"):
        # ai is accepted only for existing callers; acquisition never invokes it.
        self.backend = backend
        self.cache_dir = Path(cache_dir)
        self.lock = asyncio.Lock()

    async def capture_message(self, request, item, *, index=0):
        async with self.lock:
            return await asyncio.to_thread(self._capture, dict(request), dict(item), index)

    def _capture(self, request, item, index):
        if type(index) is not int or index < 0:
            raise MediaError("QQ 图片序号无效。")
        backend = self.backend or Win32QQ()
        target = backend.locate_target(request, item, index=index)
        if not isinstance(target, QQCaptureTarget):
            raise MediaError("本地截图算法没有提供可验证的图片定位，改用后台原图识别。")
        scope = str(request.get("scope") or "")
        kind = request.get("conversation_kind") or ("group" if ":group:" in scope else "private" if ":private:" in scope else "")
        if (not scope or not request.get("account_id") or not request.get("conversation_id") or not request.get("message_id")
            or kind not in ("group", "private") or scope.rsplit(":", 1)[-1] != str(request.get("conversation_id"))
            or target.window.hwnd <= 0 or target.window.pid <= 0
            or target.account_id != str(request.get("account_id") or "")
            or target.conversation_id != str(request.get("conversation_id") or "")
            or target.message_id != str(request.get("message_id") or "")
            or target.conversation_kind != kind or target.image_index != index
            or Path(target.window.executable).name.casefold() != "qq.exe"
            or target.mode not in ("qq_chat_screenshot", "qq_image_viewer_screenshot")):
            raise MediaError("本地 QQ 图片定位与账号、会话或原消息不一致，取消截图。")
        sticker = item.get("subType", item.get("sub_type", item.get("subtype"))) in (1, "1", "sticker") or item.get("type") in ("mface", "bface")
        if target.mode != "qq_image_viewer_screenshot" and not sticker:
            raise MediaError("普通图片未能可靠定位 QQ 图片查看器，改用后台原图识别。")
        picture = backend.capture(target.window)
        expected_size = (target.window.rect[2] - target.window.rect[0], target.window.rect[3] - target.window.rect[1])
        if picture.size != expected_size:
            raise MediaError("QQ 截图尺寸与已定位窗口不一致，改用后台原图识别。")
        box = checked_box(target.box, picture.size)
        if backend.locate_target(request, item, index=index) != target:
            raise MediaError("QQ 账号、聊天或图片位置已变化，取消本次截图。")
        result = picture.crop(box)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / (uuid.uuid4().hex + "_capture.png")
        temporary = path.with_suffix(".tmp")
        result.save(temporary, format="PNG")
        os.replace(temporary, path)
        metadata = {"scope": scope, "account_id": target.account_id,
            "conversation_id": target.conversation_id, "conversation_kind": target.conversation_kind,
            "message_id": target.message_id, "image_index": index,
            "mode": target.mode, "hwnd": target.window.hwnd, "pid": target.window.pid,
            "box": list(box), "evidence": target.evidence, "acquisition": "qqbot_local_algorithm"}
        path.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("QQBot 本地算法已截图消息 %s：%s", target.message_id, path)
        return {"file": str(path.resolve()), "url": "", "subType": "1" if sticker else "0",
            "capture_mode": target.mode, "capture_provenance": metadata,
            "summary": "QQ 聊天截图" if sticker else "QQ 图片查看器截图"}
