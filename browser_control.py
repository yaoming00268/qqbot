"""Chromium page capture/input on the bot's private desktop, with local profiles."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

from PIL import Image


BROWSER_NAMES = {"chrome.exe", "msedge.exe", "chromium.exe", "brave.exe", "vivaldi.exe"}
SOFTWARE_KEYS = {
    "pageup": ("PageUp", 0x21), "pagedown": ("PageDown", 0x22), "home": ("Home", 0x24),
    "end": ("End", 0x23), "delete": ("Delete", 0x2E), "a": ("a", 0x41), "f": ("f", 0x46),
    "k": ("k", 0x4B), "j": ("j", 0x4A), "m": ("m", 0x4D), "n": ("n", 0x4E),
    "p": ("p", 0x50), "t": ("t", 0x54), "w": ("w", 0x57),
    "ctrl+a": ("a", 0x41), "ctrl+f": ("f", 0x46), "ctrl+l": ("l", 0x4C),
    "ctrl+z": ("z", 0x5A), "ctrl+k": ("k", 0x4B),
    "shift+tab": ("Tab", 0x09), "alt+left": ("ArrowLeft", 0x25), "alt+right": ("ArrowRight", 0x27),
}


class BrowserError(ValueError):
    def __init__(self, code, message, *, input_posted=None):
        super().__init__(message)
        self.code = code
        self.input_posted = input_posted
        self.hint = "保留专用登录目录；先重新截图核对页面，不重放结果不明的输入。"


def browser_executable(path):
    return Path(path).name.casefold() in BROWSER_NAMES


def checked_url(value):
    if not isinstance(value, str) or any(ord(char) < 33 for char in value):
        raise ValueError("网址必须是完整的 http/https 地址。")
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("只支持不含账号密码的 http/https 地址。")
    return value


def installed_browser(configured=""):
    """Use a configured browser or a known installation, never shell lookup."""
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file() or not browser_executable(path):
            raise BrowserError("BROWSER_EXECUTABLE_INVALID", "配置的默认浏览器不存在或不是受支持的 Chromium 浏览器。")
        return path
    candidates = []
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(name)
        if root:
            candidates.extend(Path(root) / relative for relative in (
                "Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe",
                "BraveSoftware/Brave-Browser/Application/brave.exe", "Chromium/Application/chromium.exe",
                "Vivaldi/Application/vivaldi.exe"))
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise BrowserError("BROWSER_NOT_INSTALLED", "未找到可用浏览器；请在控制台设置默认浏览器 exe 路径。")


class BrowserBridge:
    # Read labels, geometry and focus, never input values or credential contents.
    INPUT_STATE_SCRIPT = r"""(() => {
        const textTypes = new Set(['text', 'search', 'email', 'url', 'tel', 'password', 'number']);
        const editable = el => !!el && !el.disabled && !el.readOnly &&
            el.getAttribute('aria-disabled') !== 'true' &&
            el.getAttribute('aria-readonly') !== 'true' && !el.closest('[inert]') &&
            (el.isContentEditable || el.tagName === 'TEXTAREA' ||
             (el.tagName === 'INPUT' && textTypes.has(el.type)));
        const describe = el => ({
            tag: el ? el.tagName.toLowerCase() : '',
            type: el && el.tagName === 'INPUT' ? el.type : '',
            editable: editable(el),
            label: el ? (el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                         el.getAttribute('name') || el.id || '').slice(0, 100) : ''
        });
        let active = document.activeElement;
        while (active && active.shadowRoot && active.shadowRoot.activeElement)
            active = active.shadowRoot.activeElement;
        // Follow same-origin frames for focus validation. Opaque frames cannot
        // be confirmed from this document and require a new observation.
        for (let depth = 0; active && active.tagName === 'IFRAME' && depth < 8; depth++) {
            try {
                const child = active.contentDocument;
                if (!child) break;
                active = child.activeElement;
                while (active && active.shadowRoot && active.shadowRoot.activeElement)
                    active = active.shadowRoot.activeElement;
            } catch (_) { break; }
        }
        const fields = [];
        const collect = root => {
            for (const el of root.querySelectorAll('input, textarea, [contenteditable]')) {
                if (fields.length >= 8) break;
                if (el.tagName === 'INPUT' && !textTypes.has(el.type)) continue;
                const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
                if (!rect.width || !rect.height || style.visibility === 'hidden' ||
                    style.display === 'none' || style.opacity === '0' ||
                    rect.right <= 0 || rect.bottom <= 0 ||
                    rect.left >= innerWidth || rect.top >= innerHeight) continue;
                fields.push({...describe(el), bounds: [rect.x, rect.y, rect.width, rect.height]});
            }
        };
        collect(document);
        if (active && active.getRootNode() instanceof ShadowRoot) collect(active.getRootNode());
        return {focus: describe(active), fields};
    })()"""

    def __init__(self, target):
        self.target = target
        self.profile = Path(target["profile_path"]).resolve()

    def endpoint(self):
        try:
            port = int((self.profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0])
            if not 1 <= port <= 65535:
                raise ValueError()
            return "http://127.0.0.1:" + str(port)
        except (OSError, ValueError, IndexError) as error:
            raise BrowserError("BROWSER_NOT_READY", "浏览器尚未建立页面控制连接。") from error

    def pages(self, *, include_internal=False):
        try:
            # The endpoint is always loopback, regardless of environment proxies.
            opener = build_opener(ProxyHandler({}))
            with opener.open(self.endpoint() + "/json/list", timeout=3) as response:
                pages = json.loads(response.read(2 * 1024 * 1024))
            if not isinstance(pages, list):
                raise ValueError()
            result = []
            expected = urlsplit(self.endpoint()).netloc
            for page in pages:
                socket = urlsplit(page.get("webSocketDebuggerUrl", ""))
                url = page.get("url", "")
                user_page = urlsplit(url).scheme in ("http", "https") or url == "about:blank"
                if (page.get("type") == "page" and socket.scheme == "ws" and socket.netloc == expected
                        and (include_internal or user_page)):
                    result.append(page)
            return result
        except BrowserError:
            raise
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise BrowserError("BROWSER_NOT_READY", "浏览器页面暂不可用，请稍后重新截图。") from error

    def launch_arguments(self):
        try:
            active = bool(self.pages(include_internal=True))
        except BrowserError:
            active = False
        if active:
            raise BrowserError("BROWSER_PROFILE_BUSY", "该登录目录已被另一个浏览器占用，请关闭该实例或选择另一个专用目录。")
        self.profile.mkdir(parents=True, exist_ok=True)
        return ["--user-data-dir=" + str(self.profile), "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding", "--window-size=1280,800", "--force-device-scale-factor=1",
                self.target.get("launch_url") or "about:blank"]

    def page(self, identity=""):
        pages = self.pages()
        if identity:
            pages = [page for page in pages if page["id"] == identity]
        if not pages:
            raise BrowserError("BROWSER_PAGE_CHANGED", "观察中的页面已关闭或尚未就绪，请重新截图。")
        return pages[0]

    @staticmethod
    @contextmanager
    def connection(page):
        from websockets.sync.client import connect
        try:
            with connect(page["webSocketDebuggerUrl"], open_timeout=4, close_timeout=1, max_size=24 * 1024 * 1024) as socket:
                yield socket
        except BrowserError:
            raise
        except Exception as error:
            raise BrowserError("BROWSER_CONNECTION_FAILED", "浏览器连接中断，操作结果未能确认；请重新截图，不重放输入。") from error

    @staticmethod
    def command(socket, identity, method, params=None):
        socket.send(json.dumps({"id": identity, "method": method, "params": params or {}}))
        deadline = time.monotonic() + 6
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError("BROWSER_TIMEOUT", "浏览器未确认操作结果，不自动重发。")
            result = json.loads(socket.recv(timeout=remaining))
            if result.get("id") == identity:
                if result.get("error"):
                    raise BrowserError("BROWSER_COMMAND_FAILED", "浏览器未接受页面操作。")
                return result.get("result", {})

    def input_state(self, socket, identity):
        result = self.command(socket, identity, "Runtime.evaluate", {
            "expression": self.INPUT_STATE_SCRIPT, "returnByValue": True, "silent": True, "timeout": 1000})
        state = result.get("result", {}).get("value")
        if result.get("exceptionDetails") or not isinstance(state, dict) or not isinstance(state.get("focus"), dict):
            raise BrowserError("BROWSER_INPUT_STATE_UNAVAILABLE", "暂时无法确认页面输入框，请重新截图。")
        return state

    @staticmethod
    def input_metadata(state, size, viewport):
        width, height = size
        scale_x, scale_y = width / viewport["clientWidth"], height / viewport["clientHeight"]
        offset_x, offset_y = viewport.get("offsetX", 0), viewport.get("offsetY", 0)
        fields = []
        for field in state.get("fields", []):
            x, y, w, h = field["bounds"]
            left, top = max(0, (x - offset_x) * scale_x), max(0, (y - offset_y) * scale_y)
            right, bottom = min(width, (x + w - offset_x) * scale_x), min(height, (y + h - offset_y) * scale_y)
            if right > left and bottom > top:
                fields.append({**field, "bounds": [round(left, 2), round(top, 2), round(right - left, 2), round(bottom - top, 2)],
                               "center": [round((left + right) / 2, 2), round((top + bottom) / 2, 2)]})
        return {"input_focus": state["focus"], "input_fields": fields}

    def capture(self):
        page = self.page()
        with self.connection(page) as socket:
            self.command(socket, 1, "Page.bringToFront")
            metrics = self.command(socket, 2, "Page.getLayoutMetrics")
            viewport = metrics.get("cssVisualViewport") or metrics["visualViewport"]
            data = self.command(socket, 3, "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
            state = self.input_state(socket, 4)
        image = Image.open(io.BytesIO(base64.b64decode(data["data"]))).convert("RGB")
        return image, {"browser_page": page["id"], "page_url": page.get("url", ""),
                       "title": page.get("title", ""), "browser_viewport": [viewport["clientWidth"], viewport["clientHeight"]],
                       **self.input_metadata(state, image.size, viewport)}

    def act(self, observed, action, *, key="", x=None, y=None, button="left", text="", delta=0, url=""):
        page = self.page(observed["browser_page"])
        if page.get("url", "") != observed.get("page_url", ""):
            raise BrowserError("BROWSER_PAGE_CHANGED", "页面已跳转，请重新截图后操作。")
        with self.connection(page) as socket:
            if action == "type":
                try:
                    state = self.input_state(socket, 1)
                except BrowserError as error:
                    error.input_posted = False
                    raise
                if not state["focus"].get("editable"):
                    error = BrowserError("BROWSER_INPUT_NOT_FOCUSED",
                        "当前没有聚焦可编辑输入框，未输入文字。", input_posted=False)
                    error.hint = "按新截图 input_fields 的 center 像素坐标点击目标框，确认 input_focus.editable=true 后再 type；不要盲按 Tab。"
                    raise error
                self.command(socket, 2, "Input.insertText", {"text": text})
            elif action == "navigate":
                result = self.command(socket, 1, "Page.navigate", {"url": checked_url(url)})
                if result.get("errorText"):
                    raise BrowserError("BROWSER_NAVIGATION_FAILED", "浏览器页面未加载成功，请重新截图核对。")
            elif action == "key":
                basics = {"enter": ("Enter", 13), "space": (" ", 32), "up": ("ArrowUp", 38), "down": ("ArrowDown", 40),
                          "left": ("ArrowLeft", 37), "right": ("ArrowRight", 39), "escape": ("Escape", 27),
                          "s": ("s", 83), "l": ("l", 76), "tab": ("Tab", 9), "backspace": ("Backspace", 8), "f5": ("F5", 116)}
                name, code = (SOFTWARE_KEYS | basics)[key]
                modifiers = (2 if "ctrl+" in key else 0) | (1 if "alt+" in key else 0) | (8 if "shift+" in key else 0)
                physical = ("Key" + name.upper() if len(name) == 1 and name.isalpha()
                            else "Space" if name == " " else name)
                params = {"key": name, "code": physical, "windowsVirtualKeyCode": code, "modifiers": modifiers}
                # Enter needs its character event for native form submission.
                # Ctrl/Alt shortcuts must not insert printable characters.
                character = {"Enter": "\r", "Tab": "\t"}.get(name, name if len(name) == 1 else "")
                if modifiers & ~8:
                    character = ""
                down = {**params, "type": "keyDown" if character else "rawKeyDown"}
                if character:
                    down.update(text=character, unmodifiedText=character)
                try:
                    self.command(socket, 1, "Input.dispatchKeyEvent", down)
                finally:
                    self.command(socket, 2, "Input.dispatchKeyEvent", {**params, "type": "keyUp"})
            else:
                width, height = observed["browser_viewport"]
                params = {"x": x * width, "y": y * height}
                if action == "scroll":
                    self.command(socket, 1, "Input.dispatchMouseEvent", {**params, "type": "mouseWheel", "deltaX": 0, "deltaY": delta})
                else:
                    params.update(button=button, clickCount=1)
                    try:
                        self.command(socket, 1, "Input.dispatchMouseEvent", {**params, "type": "mousePressed"})
                    finally:
                        self.command(socket, 2, "Input.dispatchMouseEvent", {**params, "type": "mouseReleased"})

