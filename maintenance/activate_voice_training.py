"""Activate the training release through the desktop's normal save/stop workflow."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

import psutil
from playwright.sync_api import Error as BrowserError, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPORT = ROOT / "maintenance_reports/voice-training-20261116"
RELEASE = ROOT / "runtime/releases/voice-training-20261116-r3/QQBot"


def save(name, value):
    (REPORT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def connect(playwright, port):
    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    page = next(page for context in browser.contexts for page in context.pages if "index.html" in page.url)
    page.wait_for_function("window.studio?.ready", timeout=30000)
    return browser, page


def stop(port):
    from maintenance.package_runtime import runtime_files
    runtime_files(RELEASE)  # Verify all files before touching the running application.
    assert "86 passed" in (REPORT / "tests-final.log").read_text(encoding="utf-8-sig")
    portable = json.loads((REPORT / "portable/verification-summary.json").read_text(encoding="utf-8"))
    assert portable["ok"], portable
    previous = (ROOT / "runtime/active-runtime.txt").read_text(encoding="utf-8-sig").strip()
    assert (ROOT / "runtime" / previous).resolve() != RELEASE.resolve()
    with sync_playwright() as p:
        browser, page = connect(p, port)
        snapshot = page.evaluate("({config:S.config,savedConfig:S.savedConfig,revision:S.revision,personas:S.personas,personaDraft:S.personaDraft,personaDirty:S.personaDirty,datasetDirty:S.datasetDirty,glossaryDirty:S.glossaryDirty,status:S.status})")
        save("gui-before-activation.json", snapshot)
        assert (snapshot["status"].get("task") or {}).get("state") != "running"
        # saveAll merges actual edits with revision checks. A failure leaves the
        # old desktop and Core running, with the complete draft already saved.
        page.evaluate("async () => { await saveAll(); return true; }")
        config_before = (ROOT / "config.yaml").read_bytes()
        core_pid = page.evaluate("S.status.pid")
        gui_pids = [proc.pid for proc in psutil.process_iter(["exe"])
                    if proc.info["exe"] and Path(proc.info["exe"]).resolve() == (ROOT / "runtime" / previous / "QQBotStudio.exe").resolve()]
        page.evaluate("() => rpc('control','stop')")
        deadline = time.monotonic() + 120
        while core_pid and psutil.pid_exists(core_pid):
            if time.monotonic() > deadline:
                raise TimeoutError("Core has not completed its normal stop; no forced stop was performed")
            time.sleep(.25)
        backup = REPORT / "activation-backup"
        backup.mkdir(exist_ok=False)
        files = [ROOT / "config.yaml", ROOT / "personas.yaml", ROOT / "runtime/active-runtime.txt",
                 ROOT / "data/voice/catalog.json", *sorted((ROOT / "data").glob("*sessions*.json"))]
        rows = []
        for source in files:
            if source.is_file():
                target = backup / source.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                rows.append({"path": source.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
        # Online SQLite backup also preserves stores which other local workers
        # may still have open. Never copy a live DB without its WAL contents.
        for source in sorted((ROOT / "data").rglob("*.sqlite3")):
            target = backup / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(source) as live, sqlite3.connect(target) as copy:
                live.backup(copy)
        assert config_before == (ROOT / "config.yaml").read_bytes(), "Config changed during normal stop; review the preserved draft"
        save("stop-backup.json", {"ok": True, "previous_runtime": previous, "old_core_pid": core_pid,
            "old_gui_pids": gui_pids, "files": rows, "normal_stop": True, "forced_stop": False})
        try:
            page.evaluate("() => rpc('window_action','close')")
        except BrowserError:
            pass
        deadline = time.monotonic() + 30
        while any(psutil.pid_exists(pid) for pid in gui_pids):
            if time.monotonic() > deadline:
                raise TimeoutError("The old desktop has not closed normally")
            time.sleep(.2)
        browser.close()
    print(json.dumps({"ok": True, "normal_stop": True, "backup": str(backup)}), flush=True)


def activate():
    from config import AppConfig
    assert json.loads((REPORT / "stop-backup.json").read_text(encoding="utf-8"))["ok"]
    assert not any(proc.info["exe"] and Path(proc.info["exe"]).resolve().is_relative_to(ROOT / "runtime")
                   and Path(proc.info["exe"]).name in ("QQBot.exe", "QQBotStudio.exe")
                   and "--module" not in (proc.info["cmdline"] or [])
                   for proc in psutil.process_iter(["exe", "cmdline"]))
    config_bytes = (ROOT / "config.yaml").read_bytes()
    config = AppConfig.load(str(ROOT / "config.yaml"))
    pointer = ROOT / "runtime/active-runtime.txt"
    temporary = pointer.with_suffix(".tmp")
    temporary.write_text(str(RELEASE.relative_to(ROOT / "runtime")) + "\n", encoding="utf-8")
    temporary.replace(pointer)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (REPORT / "active-gui.log").open("ab") as log:
        proc = subprocess.Popen([str(RELEASE / "QQBotStudio.exe"), "--home", str(ROOT)], cwd=ROOT,
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env={**os.environ, "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={port}"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert config_bytes == (ROOT / "config.yaml").read_bytes()
    result = {"ok": True, "runtime": str(RELEASE), "gui_pid": proc.pid, "debug_port": port,
              "character": config.voice.character, "accompaniment_gain_db": config.voice.singing.accompaniment_gain_db,
              "config_sha256": hashlib.sha256(config_bytes).hexdigest(), "no_qq_send": True}
    save("activation.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def verify():
    activation = json.loads((REPORT / "activation.json").read_text(encoding="utf-8"))
    with sync_playwright() as p:
        deadline = time.monotonic() + 45
        while True:
            try:
                browser, page = connect(p, activation["debug_port"])
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.3)
        assert page.evaluate("window.studio.version") == "20261116-voice-training"
        result = page.evaluate("() => rpc('control','start')")
        assert result["state"] == "running", result
        page.wait_for_function("S.events.some(e=>e.kind==='log' && /成功连接并登录本地 QQNT/.test(e.data.text))", timeout=60000)
        page.wait_for_function("S.status.voice?.state==='applied'", timeout=20000)
        # Exercise the actual dropdown, preserving the user's saved selection.
        original = page.evaluate("S.savedConfig.voice.character")
        page.locator('[data-page="voice"]').click()
        select = page.locator('[data-config="voice.character"]')
        select.wait_for()
        page.wait_for_function("document.querySelectorAll('[data-config=\"voice.character\"] option').length>1", timeout=15000)
        choices = select.locator("option").evaluate_all("els=>els.map(e=>e.value)")
        alternate = next(value for value in choices if value and value != original)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            select.select_option(alternate)
            page.wait_for_function("wanted=>S.savedConfig.voice.character===wanted && S.status.voice?.state==='applied' && S.status.voice.character===wanted", arg=alternate, timeout=20000)
        finally:
            select.select_option(original)
            page.wait_for_function("wanted=>S.savedConfig.voice.character===wanted && S.status.voice?.state==='applied' && S.status.voice.character===wanted", arg=original, timeout=20000)
        state = page.evaluate("({version:window.studio.version,state:S.status.state,pid:S.status.pid,voice:S.status.voice,character:S.savedConfig.voice.character})")
        page.locator('[data-page="voice-training"]').click()
        page.wait_for_selector('[data-view="voice-training"] h1')
        page.wait_for_function("S.voiceTrainingInfo?.runs?.some(r=>r.state==='completed')", timeout=20000)
        page.screenshot(path=str(REPORT / "active-training-console.png"), full_page=True)
        logs = page.evaluate("S.events.filter(e=>e.kind==='log').map(e=>e.data.text).filter(t=>/成功连接|已恢复|ERROR|Traceback/.test(t))")
        browser.close()
    sessions = []
    for before in sorted((REPORT / "activation-backup/data").glob("*sessions*.json")):
        after = ROOT / "data" / before.name
        old = json.loads(before.read_text(encoding="utf-8"))
        new = json.loads(after.read_text(encoding="utf-8"))
        assert set(old) <= set(new), before.name
        # Existing sessions may receive normal activity after startup, but their
        # saved context IDs must not be discarded by the release switch.
        for key, value in old.items():
            if isinstance(value, dict) and value.get("conversation_id"):
                assert new[key].get("conversation_id") == value["conversation_id"], (before.name, key)
        sessions.append({"file": before.name, "entries": len(old), "keys_preserved": True,
                         "context_ids_preserved": True, "bytes_unchanged": before.read_bytes() == after.read_bytes()})
    assert not errors, errors
    save("active-console-verification.json", {"ok": True, "state": state, "live_switch": [alternate, original],
        "startup_evidence": logs, "sessions": sessions, "javascript_errors": errors, "no_qq_send": True})
    print(json.dumps({"ok": True, "state": state, "sessions_preserved": True}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stop", "activate", "verify"))
    parser.add_argument("--old-port", type=int, default=1828)
    args = parser.parse_args()
    if args.action == "stop":
        stop(args.old_port)
    elif args.action == "activate":
        activate()
    else:
        verify()
