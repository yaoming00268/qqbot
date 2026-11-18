"""Activate the verified VoiceStudio release with normal save/stop and config merging."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

import psutil
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPORT = ROOT / "maintenance_reports/voicestudio-20261116"
RELEASE = ROOT / "runtime/releases/voicestudio-20261116/QQBot"


def save(name, value):
    (REPORT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def atomic(path, data):
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(data, encoding="utf-8")
    temporary.replace(path)


def verify_prerequisites():
    for name in ("benchmark.json", "multi-emotion.json", "singing-verification.json", "ui-verification.json", "frozen-verification.json"):
        assert json.loads((REPORT / name).read_text(encoding="utf-8"))["ok"], name
    assert "60 passed" in (REPORT / "tests-release.log").read_text(encoding="utf-8-sig")
    assert (RELEASE / "QQBot.exe").is_file()


def windows_for(pid):
    import uiautomation as auto
    controls = []
    user = ctypes.windll.user32
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    @callback
    def visit(handle, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(handle):
            controls.append(auto.ControlFromHandle(handle))
        return True
    user.EnumWindows(visit, 0)
    return controls


def stop_and_backup():
    verify_prerequisites()
    previous = (ROOT / "runtime/active-runtime.txt").read_text(encoding="utf-8-sig").strip()
    old = (ROOT / "runtime" / previous).resolve()
    assert old != RELEASE.resolve(), "already active"
    processes = list(psutil.process_iter(["pid", "exe", "cmdline"]))
    cores = [p for p in processes if p.info["exe"] and Path(p.info["exe"]).resolve() == old / "QQBot.exe"
             and "--module" not in (p.info["cmdline"] or [])]
    guis = [p for p in processes if p.info["exe"] and Path(p.info["exe"]).resolve() == old / "QQBotStudio.exe"]
    for gui in guis:
        for window in windows_for(gui.pid):
            save_button = window.ButtonControl(Name="保存更改", searchDepth=14)
            if save_button.Exists(.4) and save_button.IsEnabled:
                raise RuntimeError("旧控制台有未保存编辑；未停止或关闭，请先保留编辑。")
    control = ROOT / "data/runtime_control.json"
    atomic(control, json.dumps({"id": uuid.uuid4().hex, "action": "stop", "time": time.time()}))
    print(json.dumps({"stage": "normal-stop-requested", "core_pids": [p.pid for p in cores]}, ensure_ascii=False), flush=True)
    deadline = time.monotonic() + 120
    while any(p.is_running() for p in cores):
        if time.monotonic() >= deadline:
            raise TimeoutError("等待核心正常保存超时；没有强制停止。")
        time.sleep(.3)
    time.sleep(1)
    # The GUI's own close workflow is used after its core has saved and exited.
    for gui in guis:
        for window in windows_for(gui.pid):
            close = window.ButtonControl(Name="关闭", searchDepth=14)
            if close.Exists(.4):
                close.GetInvokePattern().Invoke()
                time.sleep(.6)
                if not gui.is_running():
                    break
                for name in ("关闭窗口", "停止并关闭"):
                    confirm = window.ButtonControl(Name=name, searchDepth=16)
                    try:
                        exists = confirm.Exists(.3)
                    except Exception:
                        if not gui.is_running():
                            break
                        raise
                    if exists:
                        confirm.GetInvokePattern().Invoke()
                        break
    backup = REPORT / "activation-backup"
    backup.mkdir(exist_ok=True)
    files = [ROOT / "config.yaml", ROOT / "personas.yaml", ROOT / "runtime/active-runtime.txt",
             *sorted((ROOT / "data").glob("*sessions*.json"))]
    rows = []
    for file in files:
        if not file.is_file():
            continue
        target = backup / file.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(target)
        shutil.copy2(file, target)
        rows.append({"path": file.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(file.read_bytes()).hexdigest()})
    for source in sorted((ROOT / "data").glob("*.sqlite3")):
        target = backup / source.relative_to(ROOT)
        if target.exists():
            raise FileExistsError(target)
        with sqlite3.connect(source) as live, sqlite3.connect(target) as copy:
            live.backup(copy)
    save("stop-backup.json", {"ok": True, "previous_runtime": previous, "stopped_core_pids": [p.pid for p in cores],
                              "old_gui_pids": [p.pid for p in guis], "files": rows, "forced_stop": False})


def merge_and_launch():
    from config import AppConfig
    from voice_service import voice_context
    assert json.loads((REPORT / "stop-backup.json").read_text(encoding="utf-8"))["ok"]
    assert not any(p.info["name"] == "QQBot.exe" and p.info["exe"] and Path(p.info["exe"]).is_relative_to(ROOT / "runtime")
                   for p in psutil.process_iter(["name", "exe"]))
    path = ROOT / "config.yaml"
    original = path.read_bytes()
    raw = yaml.safe_load(original.decode("utf-8-sig"))
    config = AppConfig.model_validate(raw)
    old_voice = config.voice.model_copy(deep=True)
    assert old_voice.provider == "gpt_sovits_v2", "recheck current backend before merging"
    config.voice.provider = "voicestudio"
    config.voice.endpoint = "http://127.0.0.1:9881"
    config.voice.voicestudio.backend_endpoint = old_voice.endpoint
    config.voice.streaming = True
    config.voice.stream_chunk_ms = 80
    config.voice.crossfade_ms = 8
    config.voice.reuse_models = True
    config.voice.reuse_audio = True
    config.voice.sample_steps = 16
    value = config.voice.model_dump(mode="json")
    assert config.voice.singing == old_voice.singing
    assert config.voice.character == old_voice.character
    replacement = yaml.safe_dump({"voice": value}, allow_unicode=True, sort_keys=False)
    lines = original.decode("utf-8-sig").splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if re.match(r"^voice\s*:", line)]
    assert len(starts) == 1
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^[A-Za-z_][\w-]*\s*:", lines[i])), len(lines))
    updated = "".join(lines[:start]) + replacement + "".join(lines[end:])
    merged = yaml.safe_load(updated)
    assert {k: v for k, v in raw.items() if k != "voice"} == {k: v for k, v in merged.items() if k != "voice"}
    assert AppConfig.model_validate(merged) == config
    assert path.read_bytes() == original
    atomic(path, updated)
    atomic(ROOT / "runtime/active-runtime.txt", "releases\\voicestudio-20261116\\QQBot\n")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    log = (REPORT / "active-gui.log").open("ab")
    process = subprocess.Popen([str(RELEASE / "QQBotStudio.exe"), "--home", str(ROOT)], cwd=ROOT,
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        env={**os.environ, "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--remote-debugging-port=" + str(port)},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log.close()
    capability = voice_context(config.voice)
    save("activation.json", {"ok": True, "runtime": str(RELEASE), "gui_pid": process.pid, "debug_port": port,
        "voice": {k: capability[k] for k in ("enabled", "language", "character", "emotions", "inline_emotion", "streaming")},
        "singing_config_preserved": True, "accompaniment_gain_db": config.voice.singing.accompaniment_gain_db,
        "unrelated_config_preserved": True, "normal_stop": True, "no_qq_send": True})
    print(json.dumps({"ok": True, "gui_pid": process.pid, "debug_port": port}, ensure_ascii=False), flush=True)


def verify_running():
    from playwright.sync_api import sync_playwright
    activation = json.loads((REPORT / "activation.json").read_text(encoding="utf-8"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:" + str(activation["debug_port"]))
        page = next(page for context in browser.contexts for page in context.pages if "index.html" in page.url)
        page.wait_for_function("window.studio?.ready", timeout=30000)
        assert page.evaluate("window.studio.version") == "20261116-voicestudio"
        assert page.evaluate("S.config.voice.provider") == "voicestudio"
        status = page.evaluate("() => rpc('control','start')")
        assert status["state"] == "running", status
        page.wait_for_function("S.status.state==='running'", timeout=15000)
        page.wait_for_function("S.events.some(r=>r.kind==='log' && /成功连接并登录/.test(r.data.text))", timeout=45000)
        page.locator('[data-page="voice"]').click()
        page.wait_for_selector("#voice-emotion option", state="attached")
        page.screenshot(path=str(REPORT / "active-console.png"))
        logs = page.evaluate("S.events.filter(r=>r.kind==='log').map(r=>r.data.text).filter(t=>/成功连接|已恢复|ERROR|Traceback/.test(t))")
        state = page.evaluate("({version:window.studio.version,core:S.status,provider:S.config.voice.provider,character:S.config.voice.character,emotions:[...document.querySelectorAll('#voice-emotion option')].map(e=>e.value)})")
    rows = []
    for before in sorted((REPORT / "activation-backup/data").glob("*sessions*.json")):
        after = ROOT / "data" / before.name
        a = json.loads(before.read_text(encoding="utf-8"))
        b = json.loads(after.read_text(encoding="utf-8"))
        row = {"file": before.name, "bytes_unchanged": before.read_bytes() == after.read_bytes(),
               "before_keys_preserved": set(a) <= set(b), "before_entries": len(a), "after_entries": len(b)}
        assert row["before_keys_preserved"], row
        rows.append(row)
    save("session-preservation.json", {"ok": True, "sessions": rows, "method": "Normal stop/save/start, no reset."})
    save("active-console-verification.json", {"ok": True, "state": state, "startup_evidence": logs, "no_qq_send": True})
    print(json.dumps({"ok": True, "runtime": str(RELEASE), "core_pid": state["core"]["pid"], "sessions_preserved": True}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stop", "activate", "verify"))
    args = parser.parse_args()
    {"stop": stop_and_backup, "activate": merge_and_launch, "verify": verify_running}[args.action]()


if __name__ == "__main__":
    main()
