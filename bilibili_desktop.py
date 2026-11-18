"""Desktop-only Bilibili browser and LiveHime handoff; never starts a stream."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

_processes = {}
_lock = threading.RLock()


def browser_command(home, *, url=None, reset=False, preview=False):
    if getattr(sys, "frozen", False):
        command = [str(Path(sys.executable).with_name("QQBot.exe")), "--home", str(home), "--module", "bilibili-browser"]
    else:
        command = [sys.executable, "-X", "utf8", str(Path(__file__).with_name("bilibili_browser.py")), "--home", str(home)]
    if url:
        command += ["--url", url]
    if reset:
        command.append("--reset-session")
    if preview:
        command.append("--preview")
    return command


def open_browser(home, *, url=None, reset=False, preview=False):
    from bilibili_browser import START_URL, validate_url
    home = Path(home).resolve()
    if not preview:
        url = validate_url(url or START_URL)
    key = (str(home), preview)
    with _lock:
        existing = _processes.get(key)
        if existing and existing.poll() is None:
            if reset:
                raise ValueError("请先关闭 B站内置浏览器，再重新登录。")
            return {"opened": True, "reused": True, "window": "QQBot Live2D · 直播姬窗口捕获" if preview else "B站 · QQBot 内置浏览器"}
        process = subprocess.Popen(browser_command(home, url=url, reset=reset, preview=preview), cwd=home,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _processes[key] = process
    return {"opened": True, "pid": process.pid, "window": "QQBot Live2D · 直播姬窗口捕获" if preview else "B站 · QQBot 内置浏览器"}


def login_status(home, credentials_file):
    from bilibili_adapter import BilibiliAdapter
    path = Path(credentials_file)
    path = path if path.is_absolute() else Path(home) / path
    result = BilibiliAdapter(path).status()
    state = Path(home) / "data/broadcast/browser-state.json"
    if state.is_file():
        try:
            value = json.loads(state.read_text(encoding="utf-8"))
            result["browser_state"] = value.get("state", "closed")
            result["updated"] = value.get("updated")
        except (OSError, ValueError):
            pass
    return result


def detect_livehime(configured=""):
    names = {"livehime.exe", "bililive.exe", "bilibili直播姬.exe"}
    candidates = [Path(configured).expanduser()] if configured else []
    if configured and (Path(configured).name.casefold() not in names or not Path(configured).is_file()):
        raise ValueError("请选择已安装的直播姬可执行文件（livehime.exe）。")
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            for folder in ("bilibili/LiveHime", "bililive", "bilibili/直播姬", "Programs/livehime"):
                candidates.append(Path(base) / folder / "livehime.exe")
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for root in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    with winreg.OpenKey(hive, root) as key:
                        for index in range(winreg.QueryInfoKey(key)[0]):
                            try:
                                with winreg.OpenKey(key, winreg.EnumKey(key, index)) as item:
                                    name = str(winreg.QueryValueEx(item, "DisplayName")[0])
                                    if "直播姬" not in name and "livehime" not in name.lower():
                                        continue
                                    folder = Path(winreg.QueryValueEx(item, "InstallLocation")[0])
                                    candidates += [folder / filename for filename in names]
                            except OSError:
                                continue
                except OSError:
                    continue
    return next((path.resolve() for path in candidates if path.is_file()), None)


def livehime_status(config):
    executable = detect_livehime(config.executable)
    return {"installed": executable is not None, "executable": str(executable or ""),
            "transport": config.transport, "audio_device": config.audio_device,
            "stream_control": "native_livehime_ui",
            "instructions": (["点击准备画面，打开 QQBot Live2D 窗口。", "在直播姬添加窗口捕获，选择 QQBot Live2D · 直播姬窗口捕获。",
                "在角色窗口启用音频，直播姬选择对应系统音频或虚拟音频设备。"]
                if config.transport == "window" else ["启用 OBS WebSocket 并在项目中连接 OBS，点击准备画面。",
                    "在直播姬添加摄像头，选择 OBS Virtual Camera。", "虚拟摄像头不含音频；另选配置的音频设备，先试听避免重复收音。"])
                + ["在直播姬内登录、设置分区并点击开始直播；平台账号登录与直播姬登录互相独立。"]}


def open_livehime(config):
    path = detect_livehime(config.executable)
    if path is None:
        raise ValueError("未检测到直播姬，请安装后选择 EXE 路径。")
    try:
        subprocess.Popen([str(path)], cwd=path.parent, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 740:
            raise
        _request_livehime_elevation(path)
        return {"opened": False, "launch_requested": True, "stream_started": False,
                "note": "已提交管理员启动请求；请完成 Windows 提示并检查直播姬窗口。"}
    return {"opened": True, "stream_started": False}


def _request_livehime_elevation(path):
    # ShellExecute invokes the normal Windows UAC prompt, without bypassing it.
    import ctypes
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    shell.ShellExecuteW.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    result = shell.ShellExecuteW(None, "runas", str(path), None, str(path.parent), 1)
    if not result or result <= 32:
        error = ctypes.get_last_error()
        if error == 1223:
            raise PermissionError("已取消 Windows 管理员确认，直播姬未启动。")
        raise OSError(error, "无法提交直播姬管理员启动请求。")


def prepare_livehime(home, config):
    if config.bilibili.livehime.transport == "window":
        from vtuber_host import overlay_url
        from urllib.parse import urlsplit
        url = urlsplit(overlay_url(config.vtuber, preview=False))._replace(query="capture=1").geturl()
        result = open_browser(home, url=url, preview=True)
        return {**result, "transport": "window", "stream_started": False}
    from vtuber_host import execute
    execute("obs.connect", config=config.vtuber)
    execute("obs.browser_source", config=config.vtuber)
    execute("obs.scene", {"scene": config.vtuber.obs_scene}, config=config.vtuber)
    execute("obs.audio_monitor", {"source": config.vtuber.obs_source}, config=config.vtuber)
    status = execute("obs.virtualcam_status", config=config.vtuber)
    if not status.get("outputActive"):
        execute("obs.virtualcam_start", config=config.vtuber)
    return {"prepared": True, "transport": "obs_virtual_camera", "camera": "OBS Virtual Camera",
            "audio_device": config.bilibili.livehime.audio_device, "stream_started": False,
            "audio_note": "画面已准备；请在 OBS 选择监听设备，并在直播姬添加相应音频输入，虚拟摄像头不携带音频。"}
