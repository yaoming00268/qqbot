"""Native game windows on an inactive Windows desktop, with no global input."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import time

from PIL import Image

from vm_protocol import file_lock


class BackgroundWindowError(ValueError):
    def __init__(self, code, message, *, details=None, input_posted=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.input_posted = input_posted
        self.retryable = code in {"BACKGROUND_WINDOW_NOT_READY", "BACKGROUND_WINDOW_LOADING", "BACKGROUND_WINDOW_MINIMIZED",
                                  "BACKGROUND_WINDOW_HUNG", "BACKGROUND_WINDOW_CHANGED", "BACKGROUND_CAPTURE_BLANK"}
        self.hint = ("窗口仍在就绪或切换，稍后重新 screenshot；不重启程序或重放输入。" if self.retryable else
                     "查看窗口候选与取图诊断；不支持后台取图时保留进程和记录，可另行配置 qemu 模式。")


class BitmapHeader(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD),
                ("image_size", w.DWORD), ("xppm", w.LONG), ("yppm", w.LONG),
                ("used", w.DWORD), ("important", w.DWORD)]


class GuiThreadInfo(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD), ("active", w.HWND),
                ("focus", w.HWND), ("capture", w.HWND), ("menu", w.HWND),
                ("move_size", w.HWND), ("caret", w.HWND), ("caret_rect", w.RECT)]


class WindowPlacement(ctypes.Structure):
    _fields_ = [("length", w.UINT), ("flags", w.UINT), ("show", w.UINT),
                ("minimum", w.POINT), ("maximum", w.POINT), ("normal", w.RECT)]


class StartupInfo(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR), ("title", w.LPWSTR),
                ("x", w.DWORD), ("y", w.DWORD), ("x_size", w.DWORD), ("y_size", w.DWORD),
                ("x_chars", w.DWORD), ("y_chars", w.DWORD), ("fill", w.DWORD), ("flags", w.DWORD),
                ("show", w.WORD), ("reserved_size", w.WORD), ("reserved_data", ctypes.c_void_p),
                ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


class Win32Background:
    """Only this thread joins the game desktop. Never switch the input desktop."""

    # Windows can create visible input-indicator overlays in the fullscreen
    # game's own process. They are neither render targets nor game dialogs.
    INPUT_INDICATORS = {"UAC_InputIndicatorOverlayWnd", "UAC Input Indicator"}

    def __init__(self):
        if os.name != "nt":
            raise BackgroundWindowError("BACKGROUND_WINDOWS_REQUIRED", "本机后台窗口模式需要 Windows。")
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.g = ctypes.WinDLL("gdi32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.enum_callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        signatures = (
            (self.u.OpenDesktopW, [w.LPCWSTR, w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.u.CreateDesktopW, [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p], w.HANDLE),
            (self.u.GetThreadDesktop, [w.DWORD], w.HANDLE),
            (self.u.SetThreadDesktop, [w.HANDLE], w.BOOL),
            (self.u.CloseDesktop, [w.HANDLE], w.BOOL),
            (self.u.EnumDesktopWindows, [w.HANDLE, self.enum_callback, w.LPARAM], w.BOOL),
            (self.u.IsWindowVisible, [w.HWND], w.BOOL),
            (self.u.IsIconic, [w.HWND], w.BOOL),
            (self.u.IsWindowEnabled, [w.HWND], w.BOOL),
            (self.u.GetWindow, [w.HWND, w.UINT], w.HWND),
            (self.u.ShowWindowAsync, [w.HWND, ctypes.c_int], w.BOOL),
            (self.u.GetWindowLongW, [w.HWND, ctypes.c_int], w.LONG),
            (self.u.IsChild, [w.HWND, w.HWND], w.BOOL),
            (self.u.GetWindowRect, [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            (self.u.GetClientRect, [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            (self.u.GetWindowPlacement, [w.HWND, ctypes.POINTER(WindowPlacement)], w.BOOL),
            (self.u.GetWindowThreadProcessId, [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            (self.u.GetWindowTextW, [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.u.GetClassNameW, [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.u.GetGUIThreadInfo, [w.DWORD, ctypes.POINTER(GuiThreadInfo)], w.BOOL),
            (self.u.GetKeyboardState, [ctypes.c_void_p], w.BOOL),
            (self.u.SetKeyboardState, [ctypes.c_void_p], w.BOOL),
            (self.u.AttachThreadInput, [w.DWORD, w.DWORD, w.BOOL], w.BOOL),
            (self.u.GetDC, [w.HWND], w.HDC),
            (self.u.ReleaseDC, [w.HWND, w.HDC], ctypes.c_int),
            (self.u.PrintWindow, [w.HWND, w.HDC, w.UINT], w.BOOL),
            (self.u.PostMessageW, [w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.BOOL),
            (self.u.SendMessageTimeoutW, [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t)], w.LPARAM),
            (self.u.MapVirtualKeyW, [w.UINT, w.UINT], w.UINT),
            (self.u.ChildWindowFromPointEx, [w.HWND, w.POINT, w.UINT], w.HWND),
            (self.u.MapWindowPoints, [w.HWND, w.HWND, ctypes.POINTER(w.POINT), w.UINT], ctypes.c_int),
            (self.u.WaitForInputIdle, [w.HANDLE, w.DWORD], w.DWORD),
            (self.g.CreateCompatibleDC, [w.HDC], w.HDC),
            (self.g.CreateCompatibleBitmap, [w.HDC, ctypes.c_int, ctypes.c_int], w.HBITMAP),
            (self.g.SelectObject, [w.HDC, w.HGDIOBJ], w.HGDIOBJ),
            (self.g.DeleteObject, [w.HGDIOBJ], w.BOOL),
            (self.g.DeleteDC, [w.HDC], w.BOOL),
            (self.g.GetDIBits, [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT], ctypes.c_int),
            (self.k.GetCurrentThreadId, [], w.DWORD),
            (self.k.CreateProcessW, [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL,
                w.DWORD, ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo)], w.BOOL),
            (self.k.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.k.QueryFullProcessImageNameW, [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.k.GetProcessTimes, [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
            (self.k.WaitForSingleObject, [w.HANDLE, w.DWORD], w.DWORD),
            (self.k.CloseHandle, [w.HANDLE], w.BOOL),
        )
        for function, arguments, result in signatures:
            function.argtypes, function.restype = arguments, result
        self.dpi = getattr(self.u, "SetThreadDpiAwarenessContext", None)
        if self.dpi:
            self.dpi.argtypes, self.dpi.restype = [w.HANDLE], w.HANDLE

    @contextmanager
    def desktop(self, name):
        # No DESKTOP_SWITCHDESKTOP access. SetThreadDesktop affects this fresh
        # worker only, so even a game's own activation stays off the user desktop.
        access = 0x0001 | 0x0002 | 0x0004 | 0x0040 | 0x0080
        handle = self.u.OpenDesktopW(name, 0, False, access)
        if not handle:
            handle = self.u.CreateDesktopW(name, None, None, 0, access, None)
        if not handle:
            raise BackgroundWindowError("BACKGROUND_DESKTOP_UNAVAILABLE", "无法创建游戏后台桌面。")
        original = self.u.GetThreadDesktop(self.k.GetCurrentThreadId())
        previous_dpi = None
        try:
            if not self.u.SetThreadDesktop(handle):
                raise BackgroundWindowError("BACKGROUND_DESKTOP_UNAVAILABLE", "无法连接游戏后台桌面。")
            previous_dpi = self.dpi(ctypes.c_void_p(-4)) if self.dpi else None
            self.desktop_handle = handle
            yield
        finally:
            if previous_dpi:
                self.dpi(previous_dpi)
            self.u.SetThreadDesktop(original)
            self.u.CloseDesktop(handle)
            self.desktop_handle = None

    def create_process(self, executable, desktop, environment=None, *, arguments=()):
        # subprocess.STARTUPINFO does not marshal lpDesktop. Use the complete
        # Win32 structure so the new process can never start on the user desktop.
        startup = StartupInfo(cb=ctypes.sizeof(StartupInfo), desktop="WinSta0\\" + desktop, flags=1, show=4)
        process = ProcessInfo()
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(executable), *arguments]))
        flags = 0x08000000 | 0x00000200 | 0x00004000  # no console, new group, below normal priority
        environment_block = None
        if environment is not None:
            environment_block = ctypes.create_unicode_buffer("\0".join(
                key + "=" + str(value) for key, value in sorted(environment.items(), key=lambda item: item[0].casefold())) + "\0\0")
            flags |= 0x00000400  # CREATE_UNICODE_ENVIRONMENT
        if not self.k.CreateProcessW(str(executable), command_line, None, None, False, flags, environment_block,
                                    str(executable.parent), ctypes.byref(startup), ctypes.byref(process)):
            raise BackgroundWindowError("BACKGROUND_LAUNCH_FAILED", "无法在指定后台桌面启动游戏：" + str(ctypes.get_last_error()))
        try:
            # Keep the desktop alive while the child initializes USER32. Closing
            # the creator's last handle earlier can destroy it before the child
            # connects. The wait is bounded for non-GUI/slow launchers.
            self.u.WaitForInputIdle(process.process, 5000)
            return int(process.pid)
        finally:
            self.k.CloseHandle(process.thread)
            self.k.CloseHandle(process.process)

    def process(self, pid):
        handle = self.k.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            return None
        try:
            if self.k.WaitForSingleObject(handle, 0) != 0x102:
                return None
            path, length = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
            times = [w.FILETIME() for _ in range(4)]
            if (not self.k.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length))
                    or not self.k.GetProcessTimes(handle, *(ctypes.byref(item) for item in times))):
                return None
            return {"pid": int(pid), "executable": str(Path(path.value).resolve()),
                    "process_started": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime}
        finally:
            self.k.CloseHandle(handle)

    def window(self, hwnd, root):
        pid = w.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = self.process(pid.value)
        if not process or not Path(process["executable"]).is_relative_to(root):
            return None
        rect, client = w.RECT(), w.RECT()
        if (not self.u.GetWindowRect(hwnd, ctypes.byref(rect))
                or not self.u.GetClientRect(hwnd, ctypes.byref(client))):
            return None
        title = ctypes.create_unicode_buffer(1024)
        self.u.GetWindowTextW(hwnd, title, len(title))
        class_name = ctypes.create_unicode_buffer(256)
        self.u.GetClassNameW(hwnd, class_name, len(class_name))
        placement = WindowPlacement(length=ctypes.sizeof(WindowPlacement))
        normal = {}
        if self.u.GetWindowPlacement(hwnd, ctypes.byref(placement)):
            rect_normal = placement.normal
            normal["normal_rect"] = [rect_normal.left, rect_normal.top, rect_normal.right, rect_normal.bottom]
        return {**process, "hwnd": int(hwnd), "title": title.value,
                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                "size": [client.right - client.left, client.bottom - client.top],
                "class_name": class_name.value, "visible": bool(self.u.IsWindowVisible(hwnd)),
                "enabled": bool(self.u.IsWindowEnabled(hwnd)), "minimized": bool(self.u.IsIconic(hwnd)),
                "owner_hwnd": int(self.u.GetWindow(hwnd, 4) or 0),
                "style": self.u.GetWindowLongW(hwnd, -16) & 0xFFFFFFFF,
                "ex_style": self.u.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF, **normal}

    def windows(self, root, *, include_hidden=False):
        windows = []
        @self.enum_callback
        def visit(hwnd, _):
            if include_hidden or self.u.IsWindowVisible(hwnd):
                class_name = ctypes.create_unicode_buffer(256)
                self.u.GetClassNameW(hwnd, class_name, len(class_name))
                if class_name.value in self.INPUT_INDICATORS:
                    return True
                window = self.window(hwnd, root)
                if window:
                    windows.append(window)
            return True
        ctypes.set_last_error(0)
        if not self.u.EnumDesktopWindows(self.desktop_handle, visit, 0) and ctypes.get_last_error():
            raise BackgroundWindowError("BACKGROUND_ENUM_FAILED", "无法枚举游戏后台窗口。")
        return windows

    def restore_window(self, window):
        # SW_SHOWNOACTIVATE restores only this authorized application's window
        # on our thread's private desktop. It never switches the input desktop.
        self.u.ShowWindowAsync(window["hwnd"], 4)

    def capture(self, window):
        from window_selection import valid_client
        if not valid_client(window):
            raise BackgroundWindowError("BACKGROUND_WINDOW_LOADING", "内容窗口尚未建立有效客户区。", input_posted=False)
        result = ctypes.c_size_t()
        if not self.u.SendMessageTimeoutW(window["hwnd"], 0, 0, 0, 3, 1000, ctypes.byref(result)):
            raise BackgroundWindowError("BACKGROUND_WINDOW_HUNG", "内容窗口暂未响应，未截图或投递输入。", input_posted=False)
        # PrintWindow handles cross-process rendering. Sending our HDC directly
        # in WM_PRINTCLIENT can fail inside the target's graphics framework.
        methods = ["printwindow_client_full", "printwindow_client", "printwindow_full"]
        preferred = getattr(self, "capture_preference", "")
        if preferred in methods:
            methods.remove(preferred)
            methods.insert(0, preferred)
        attempts = []
        for method in methods:
            try:
                picture = self._capture_method(window, method)
                # A successful native call may still return no render data.
                if all(low == high for low, high in picture.getextrema()):
                    attempts.append({"method": method, "state": "blank"})
                    continue
                attempts.append({"method": method, "state": "captured"})
                picture.info.update(capture_method=method, capture_attempts=attempts)
                return picture
            except BackgroundWindowError as error:
                attempts.append({"method": method, "state": "failed", "code": error.code})
                if error.code in ("BACKGROUND_WINDOW_CHANGED", "BACKGROUND_WINDOW_HUNG"):
                    error.details["capture_attempts"] = attempts
                    raise
        blank = any(attempt["state"] == "blank" for attempt in attempts)
        from window_selection import window_details
        raise BackgroundWindowError("BACKGROUND_CAPTURE_BLANK" if blank else "BACKGROUND_CAPTURE_UNSUPPORTED",
            "窗口已存在，但多种后台取图方法仍未取得有效画面。" if blank else "当前窗口不支持已提供的后台取图方法。",
            details={"windows": window_details([window]), "capture_attempts": attempts}, input_posted=False)

    def _capture_method(self, window, method):
        width, height = window["size"]
        crop = None
        if method == "printwindow_full":
            left, top, right, bottom = window["rect"]
            width, height = right - left, bottom - top
            if not 0 < width <= 8192 or not 0 < height <= 8192:
                raise BackgroundWindowError("BACKGROUND_CAPTURE_FAILED", "窗口外框尺寸暂不可用。")
            point = w.POINT(0, 0)
            # MapWindowPoints legitimately returns zero for a zero translation.
            ctypes.set_last_error(0)
            mapped = self.u.MapWindowPoints(window["hwnd"], None, ctypes.byref(point), 1)
            if not mapped and ctypes.get_last_error():
                raise BackgroundWindowError("BACKGROUND_CAPTURE_FAILED", "无法定位窗口客户区。")
            x, y = point.x - left, point.y - top
            cw, ch = window["size"]
            if x < 0 or y < 0 or x + cw > width or y + ch > height:
                raise BackgroundWindowError("BACKGROUND_WINDOW_CHANGED", "取图期间客户区或边框变化。")
            crop = (x, y, x + cw, y + ch)
        src = self.u.GetDC(window["hwnd"])
        dc = self.g.CreateCompatibleDC(src) if src else None
        bitmap = self.g.CreateCompatibleBitmap(src, width, height) if dc else None
        old = self.g.SelectObject(dc, bitmap) if bitmap else None
        try:
            if not old:
                raise BackgroundWindowError("BACKGROUND_CAPTURE_FAILED", "无法建立后台取图缓冲区。")
            flags = {"printwindow_client_full": 3, "printwindow_client": 1, "printwindow_full": 2}[method]
            ok = self.u.PrintWindow(window["hwnd"], dc, flags)
            if not ok:
                raise BackgroundWindowError("BACKGROUND_CAPTURE_FAILED", "窗口未返回后台绘制结果。")
            self.g.SelectObject(dc, old)
            header = BitmapHeader(ctypes.sizeof(BitmapHeader), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
            buffer = ctypes.create_string_buffer(width * height * 4)
            if self.g.GetDIBits(dc, bitmap, 0, height, buffer, ctypes.byref(header), 0) != height:
                raise BackgroundWindowError("BACKGROUND_CAPTURE_FAILED", "无法读取游戏截图像素。")
            picture = Image.frombytes("RGB", (width, height), buffer.raw, "raw", "BGRX")
            return picture.crop(crop) if crop else picture
        finally:
            if old:
                self.g.SelectObject(dc, old)
            if bitmap:
                self.g.DeleteObject(bitmap)
            if dc:
                self.g.DeleteDC(dc)
            if src:
                self.u.ReleaseDC(window["hwnd"], src)

    def input_window(self, window, root, point=None):
        hwnd = window["hwnd"]
        if point is None:
            info = GuiThreadInfo(cbSize=ctypes.sizeof(GuiThreadInfo))
            thread = self.u.GetWindowThreadProcessId(hwnd, None)
            if self.u.GetGUIThreadInfo(thread, ctypes.byref(info)) and info.focus and self.u.IsChild(hwnd, info.focus):
                hwnd = info.focus
        else:
            # Child controls need their own client coordinates. All hit testing
            # is in the game tree, regardless of windows on the user's desktop.
            for _ in range(32):
                child = self.u.ChildWindowFromPointEx(hwnd, point, 7)
                if not child or child == hwnd:
                    break
                self.u.MapWindowPoints(hwnd, child, ctypes.byref(point), 1)
                hwnd = child
        if not self.window(hwnd, root):
            raise BackgroundWindowError("BACKGROUND_WINDOW_CHANGED", "输入窗口不属于已授权游戏。")
        return hwnd, point

    def post(self, hwnd, message, wp, lp):
        if not self.u.PostMessageW(hwnd, message, wp, lp):
            raise BackgroundWindowError("BACKGROUND_INPUT_REJECTED", "游戏窗口拒绝后台输入。")

    @contextmanager
    def modifiers(self, hwnd, key):
        """Set modifier state only for threads on this inactive desktop."""
        codes = [code for name, code in (("ctrl+", 0x11), ("alt+", 0x12), ("shift+", 0x10)) if name in key]
        if not codes:
            yield
            return
        current = self.k.GetCurrentThreadId()
        target = self.u.GetWindowThreadProcessId(hwnd, None)
        attached = target != current
        if attached and not self.u.AttachThreadInput(current, target, True):
            raise BackgroundWindowError("BACKGROUND_INPUT_REJECTED", "无法连接后台窗口的按键队列。")
        saved = (ctypes.c_ubyte * 256)()
        try:
            if not self.u.GetKeyboardState(saved):
                raise BackgroundWindowError("BACKGROUND_INPUT_REJECTED", "无法读取后台按键状态。")
            state = (ctypes.c_ubyte * 256)(*saved)
            for code in codes:
                state[code] |= 0x80
            if not self.u.SetKeyboardState(state):
                raise BackgroundWindowError("BACKGROUND_INPUT_REJECTED", "无法设置后台快捷键状态。")
            try:
                yield
            finally:
                self.u.SetKeyboardState(saved)
        finally:
            if attached:
                self.u.AttachThreadInput(current, target, False)


class BackgroundWindow:
    KEYS = {"enter": 0x0D, "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
            "escape": 0x1B, "s": 0x53, "l": 0x4C, "tab": 0x09, "backspace": 0x08, "f5": 0x74}
    BUTTONS = {"left": (0x0201, 0x0202, 1), "right": (0x0204, 0x0205, 2), "middle": (0x0207, 0x0208, 16)}

    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def status():
        ready = os.name == "nt"
        return {"mode": "background", "state": "ready" if ready else "windows_required", "ready": ready,
                "os_isolated": False, "host_input": False, "background_desktop": True,
                "input_transport": "window_messages", "capture": "PrintWindow", "coordinate_space": "client",
                "capture_adapters": ["PrintWindow", "renpy"], "input_transports": ["window_messages", "renpy_events"]}

    def desktop_name(self, target):
        identity = str(self.root).casefold() + ":" + target["id"]
        return "qqbot-game-" + hashlib.sha256(identity.encode()).hexdigest()[:24]

    def _call(self, target, method, *args, **kwargs):
        desktop = self.desktop_name(target)
        def work():
            with file_lock(self.root / (desktop + ".lock"), timeout=2):
                api = Win32Background()
                with api.desktop(desktop):
                    return method(api, target, desktop, *args, **kwargs)
        # SetThreadDesktop must never run on a Qt/UI thread or a reused worker
        # that owns windows. A fresh thread also restores/closes its desktop.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="qqbot-background") as pool:
            return pool.submit(work).result()

    def launch(self, target):
        return self._call(target, self._launch)

    @staticmethod
    def _windows(api, target, *, include_hidden=False):
        root = Path(target["executable"]).resolve().parent
        windows = api.windows(root, include_hidden=True) if include_hidden else api.windows(root)
        from browser_control import browser_executable
        if target.get("kind") == "software" and browser_executable(target["executable"]):
            # Edge creates an untitled native profile flyout alongside the page
            # window. Page input goes through CDP; that flyout is not its target.
            executable = Path(target["executable"]).resolve()
            windows = [window for window in windows if window["title"].strip()
                       and Path(window["executable"]).resolve() == executable]
        return windows

    def _launch(self, api, target, desktop):
        windows = self._windows(api, target)
        from window_selection import choose_window, window_details
        window, reason = choose_window(windows)
        if reason == "ambiguous":
            raise BackgroundWindowError("BACKGROUND_WINDOW_AMBIGUOUS", "后台有多个独立内容窗口，不重复启动。",
                                        details={"windows": window_details(windows)})
        if windows:
            return {"pid": (window or windows[0])["pid"], "attached": True, "pending": True,
                    "state": "process_running", "window_ready": False, "mode": "background", "desktop": desktop, "host_input": False}
        hidden = self._windows(api, target, include_hidden=True)
        if hidden:
            return {"pid": hidden[0]["pid"], "attached": True, "pending": True,
                    "state": "process_running", "window_ready": False, "mode": "background", "desktop": desktop, "host_input": False}
        # The launcher may not have created a window yet. Persist its identity
        # so independent MCP/CLI workers do not launch a second copy.
        state = self.root / (desktop + ".json")
        if state.is_file():
            previous = json.loads(state.read_text(encoding="utf-8"))
            process = api.process(previous["pid"])
            age = time.time() - previous["started"]
            pending_grace = previous.get("window_ready") is False and age < 60
            legacy_grace = not process and not previous.get("process") and age < 30
            if (process and process == previous.get("process")) or pending_grace or legacy_grace:
                return {"pid": previous["pid"], "attached": True, "pending": True,
                        "state": "process_running", "window_ready": False,
                        "mode": "background", "desktop": desktop, "host_input": False}
        executable = Path(target["executable"]).resolve(strict=True)
        from browser_control import browser_executable, BrowserBridge
        if target.get("kind") == "software" and browser_executable(executable):
            bridge = BrowserBridge(target)
            profile_lock = hashlib.sha256(str(bridge.profile).casefold().encode()).hexdigest()[:24]
            with file_lock(self.root / ("profile-" + profile_lock + ".lock"), timeout=2):
                pid = api.create_process(executable, desktop, arguments=bridge.launch_arguments())
        else:
            pid = self._launch_native(api, target, desktop, executable)
        data = {"pid": pid, "started": time.time(), "process": api.process(pid), "window_ready": False}
        temporary = state.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(state)
        return {"pid": pid, "attached": False, "pending": True, "state": "process_started", "window_ready": False,
                "mode": "background", "desktop": desktop, "host_input": False}

    def _launch_native(self, api, target, desktop, executable):
        from renpy_bridge import is_renpy
        if is_renpy(executable):
            return api.create_process(executable, desktop, self._bridge(target, desktop).prepare())
        return api.create_process(executable, desktop)

    @staticmethod
    def _locate(api, target):
        windows = BackgroundWindow._windows(api, target)
        from window_selection import choose_window, window_details
        window, reason = choose_window(windows)
        details = {"windows": window_details(windows), "selection": reason}
        if window is None:
            if reason == "ambiguous":
                raise BackgroundWindowError("BACKGROUND_WINDOW_AMBIGUOUS", "没有唯一可用的内容窗口，请先核对窗口候选。",
                                            details=details, input_posted=False)
            raise BackgroundWindowError("BACKGROUND_WINDOW_NOT_READY", "进程尚未出现可操作的内容窗口；辅助窗口不能用于取图或输入。",
                                        details=details, input_posted=False)
        if api.u.IsIconic(window["hwnd"]):
            raise BackgroundWindowError("BACKGROUND_WINDOW_MINIMIZED", "后台内容窗口已最小化，等待窗口恢复。",
                                        details=details, input_posted=False)
        from renpy_bridge import is_renpy
        if is_renpy(target["executable"]) and window["title"].casefold() == str(Path(target["executable"]).with_suffix(".py")).casefold():
            raise BackgroundWindowError("BACKGROUND_WINDOW_LOADING", "Ren'Py 仍在显示启动加载条，尚未产生游戏画面。",
                                        details=details, input_posted=False)
        return window

    def _observation_window(self, api, target):
        try:
            return self._locate(api, target), ""
        except BackgroundWindowError as error:
            if error.code not in ("BACKGROUND_WINDOW_NOT_READY", "BACKGROUND_WINDOW_MINIMIZED"):
                raise
            from window_selection import choose_window, window_details
            windows = self._windows(api, target, include_hidden=True)
            window, reason = choose_window(windows, recover=True)
            if window is None:
                error.details = {"windows": window_details(windows), "selection": reason}
                if reason == "ambiguous":
                    raise BackgroundWindowError("BACKGROUND_WINDOW_AMBIGUOUS", "多个内容窗口均可能是目标，未恢复或发送输入。",
                                                details=error.details, input_posted=False) from None
                raise
            api.restore_window(window)
            time.sleep(.1)
            try:
                return self._locate(api, target), "shown_without_activation"
            except BackgroundWindowError as pending:
                pending.details.update(recovery="show_requested", hwnd=window["hwnd"])
                raise

    def _bridge(self, target, desktop):
        from renpy_bridge import RenpyBridge
        return RenpyBridge(self.root, target, desktop)

    @staticmethod
    def _check(current, observation):
        if any(current.get(field) != observation.get(field) for field in ("hwnd", "pid", "process_started", "rect", "size")):
            raise BackgroundWindowError("BACKGROUND_WINDOW_CHANGED", "窗口或尺寸已变化，请重新观察。", input_posted=False)

    def observe(self, target, path):
        return self._call(target, self._observe, path)

    def _observe(self, api, target, desktop, path):
        window, restored = self._observation_window(api, target)
        from browser_control import browser_executable, BrowserBridge
        if target.get("kind") == "software" and browser_executable(target["executable"]):
            picture, page = BrowserBridge(target).capture()
            self._check(self._locate(api, target), window)
            picture.save(path, "PNG")
            self._record_ready(desktop, window, "chromium")
            return {**window, **page, "window_size": window["size"], "size": list(picture.size),
                    "mode": "background", "desktop": desktop, "target_id": target["id"], "host_input": False,
                    "coordinate_space": "browser_viewport", "capture": "chromium", "window_recovery": restored}
        bridge = self._bridge(target, desktop)
        capture = "PrintWindow"
        if bridge.ready(window):
            picture = bridge.call(window, "capture")
            capture = "renpy"
        else:
            try:
                cached = self.root / (desktop + ".capture.json")
                if cached.is_file():
                    try:
                        profile = json.loads(cached.read_text(encoding="utf-8"))
                        if all(profile.get(key) == window.get(key) for key in ("executable", "class_name")):
                            api.capture_preference = profile.get("method", "")
                    except (OSError, ValueError, TypeError):
                        pass
                picture = api.capture(window)
            except BackgroundWindowError as error:
                from renpy_bridge import is_renpy, RenpyBridgeError
                if error.code in ("BACKGROUND_CAPTURE_BLANK", "BACKGROUND_CAPTURE_UNSUPPORTED") and is_renpy(target["executable"]):
                    raise RenpyBridgeError("RENPY_BRIDGE_RESTART_REQUIRED", "Ren'Py 的 GPU 窗口无法由 PrintWindow 读取；当前进程未加载原生取图适配，需允许后重新启动游戏。") from error
                raise
        self._check(self._locate(api, target), window)
        picture.save(path, "PNG")
        method = picture.info.get("capture_method", capture)
        self._record_ready(desktop, window, method)
        return {**window, "mode": "background", "desktop": desktop, "target_id": target["id"],
                "os_isolated": False, "host_input": False, "coordinate_space": "client", "capture": capture,
                "capture_method": method,
                "capture_attempts": picture.info.get("capture_attempts", []), "window_recovery": restored,
                **({"game_reading": picture.info["game_reading"]} if "game_reading" in picture.info else {})}

    def _record_ready(self, desktop, window, method):
        state = self.root / (desktop + ".json")
        if state.is_file():
            try:
                previous = json.loads(state.read_text(encoding="utf-8"))
                previous.update(window_ready=True, last_window={key: window[key] for key in ("hwnd", "pid", "process_started")},
                                observed_at=time.time())
                temporary = state.with_suffix(".tmp")
                temporary.write_text(json.dumps(previous), encoding="utf-8")
                temporary.replace(state)
            except (OSError, ValueError, TypeError, AttributeError) as error:
                logging.getLogger(__name__).warning("Could not persist background launch readiness: %s", error)
        # These are hints for the next capture. A damaged or unwritable cache
        # must not invalidate real pixels; SandboxRuntime stores the observation.
        try:
            profile = {key: window.get(key, "") for key in ("executable", "class_name")}
            profile["method"] = method
            cached = self.root / (desktop + ".capture.json")
            temporary = cached.with_suffix(".tmp")
            temporary.write_text(json.dumps(profile), encoding="utf-8")
            temporary.replace(cached)
        except OSError as error:
            logging.getLogger(__name__).warning("Could not persist background capture preference: %s", error)

    def act(self, target, observation, action, *, key="", x=None, y=None, button="left", guard=None, **extra):
        from game_window import GameWindow
        GameWindow.validate_action(action, key=key, x=x, y=y, button=button, software=target.get("kind") == "software", **extra)
        return self._call(target, self._act, observation, action, key=key, x=x, y=y, button=button, guard=guard, **extra)

    def inspect(self, target, observation):
        return self._call(target, self._inspect, observation)

    def _inspect(self, api, target, desktop, observation):
        if observation.get("desktop") != desktop or observation.get("target_id") != target["id"]:
            raise BackgroundWindowError("BACKGROUND_OBSERVATION_MISMATCH", "观察不属于当前任务。", input_posted=False)
        window = self._locate(api, target)
        self._check(window, {**observation, "size": observation.get("window_size", observation["size"])})
        from computer_control import PlaywrightBridge, WindowsAutomation
        return (PlaywrightBridge(target).inspect(observation) if observation.get("capture") == "chromium"
                else WindowsAutomation(window).inspect())

    def _act(self, api, target, desktop, observation, action, *, key, x, y, button, guard, text="", delta=0, url="", element_id=""):
        browser = observation.get("capture") == "chromium"
        if (observation.get("mode") != "background" or observation.get("desktop") != desktop
                or observation.get("target_id") != target["id"]
                or observation.get("coordinate_space") != ("browser_viewport" if browser else "client")):
            raise BackgroundWindowError("BACKGROUND_OBSERVATION_MISMATCH", "截图不属于当前后台任务，未投递输入。", input_posted=False)
        window = self._locate(api, target)
        self._check(window, {**observation, "size": observation.get("window_size", observation["size"])})
        if action in ("invoke", "fill", "select"):
            from computer_control import PlaywrightBridge, WindowsAutomation
            controller = PlaywrightBridge(target) if browser else WindowsAutomation(window)
            return controller.act(observation, action, element_id, text=text, guard=guard)
        if browser:
            if guard:
                guard()
            from browser_control import BrowserBridge
            BrowserBridge(target).act(observation, action, key=key, x=x, y=y, button=button, text=text, delta=delta, url=url)
            return {"posted": True, "verified": False, "mode": "background", "host_input": False,
                    "input_transport": "chromium", "hint": "操作仅投递到后台浏览器页面；核对返回的新截图。"}
        if action == "navigate":
            raise ValueError("navigate 仅用于受支持的浏览器页面。")
        if observation.get("capture") == "renpy":
            if guard:
                guard()
            self._bridge(target, desktop).call(window, action, {"key": key, "x": x, "y": y, "button": button})
            return {"posted": True, "verified": False, "mode": "background", "host_input": False,
                    "input_hwnd": window["hwnd"], "input_transport": "renpy_events",
                    "hint": "事件仅投递到当前游戏引擎；请用 observe 新截图确认效果。"}
        root = Path(target["executable"]).resolve().parent
        if action == "type":
            hwnd, _ = api.input_window(window, root)
            self._check(self._locate(api, target), observation)
            if guard:
                guard()
            raw = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r").encode("utf-16-le")
            for index in range(0, len(raw), 2):
                if guard:
                    guard()
                api.post(hwnd, 0x0102, int.from_bytes(raw[index:index + 2], "little"), 1)  # WM_CHAR, no clipboard.
            return {"posted": True, "verified": False, "host_input": False, "input_transport": "window_messages"}
        if action == "scroll":
            point = w.POINT(int(x * window["size"][0]), int(y * window["size"][1]))
            hwnd, _ = api.input_window(window, root, point)
            screen = w.POINT(int(x * window["size"][0]), int(y * window["size"][1]))
            api.u.MapWindowPoints(window["hwnd"], None, ctypes.byref(screen), 1)
            self._check(self._locate(api, target), observation)
            if guard:
                guard()
            # Wheel messages use screen coordinates; this does not move the OS cursor.
            amount = (-1 if delta > 0 else 1) * max(1, round(abs(delta) / 100)) * 120
            api.post(hwnd, 0x020A, (amount & 0xFFFF) << 16, (screen.x & 0xFFFF) | ((screen.y & 0xFFFF) << 16))
            return {"posted": True, "verified": False, "host_input": False, "input_transport": "window_messages"}
        if action == "key":
            hwnd, _ = api.input_window(window, root)
            from browser_control import SOFTWARE_KEYS
            code = self.KEYS[key] if key in self.KEYS else SOFTWARE_KEYS[key][1]
            scan = api.u.MapVirtualKeyW(code, 0)
            lp = 1 | (scan << 16) | ((1 << 24) if key in ("up", "down", "left", "right") else 0)
            down, up, wp = 0x0100, 0x0101, code
            release_lp = lp | (3 << 30)
        else:
            point = w.POINT(int(x * window["size"][0]), int(y * window["size"][1]))
            hwnd, point = api.input_window(window, root, point)
            lp = (point.x & 0xFFFF) | ((point.y & 0xFFFF) << 16)
            down, up, wp = self.BUTTONS[button]
            release_lp = lp
        self._check(self._locate(api, target), observation)
        if guard:
            guard()
        if action == "click":
            api.post(hwnd, 0x0200, 0, lp)  # WM_MOUSEMOVE carries local coords, no OS cursor movement.
        from contextlib import nullcontext
        with api.modifiers(hwnd, key) if action == "key" and "+" in key else nullcontext():
            try:
                api.post(hwnd, down, wp, lp)
                time.sleep(.06)
            finally:
                # Release even if stop/cancellation arrives during this one input.
                api.post(hwnd, up, code if action == "key" else 0, release_lp)
        return {"posted": True, "verified": False, "mode": "background", "host_input": False, "input_hwnd": hwnd,
                "hint": "消息仅投递到后台游戏窗口；用 observe 新截图确认实际效果。"}
