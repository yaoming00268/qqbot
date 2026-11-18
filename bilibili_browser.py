"""Unprivileged embedded Bilibili browser with its own persistent WebView profile."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
import uuid

from bilibili_credentials import credentials_from_cookies, save_credentials

START_URL = "https://www.bilibili.com/"


def validate_url(value):
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not (host == "bilibili.com" or host.endswith(".bilibili.com"))):
        raise ValueError("内置 B站浏览器只接受 bilibili.com 的 HTTPS 地址。")
    return value


def launch(home, *, url=START_URL, reset=False, preview=False):
    import webview
    from config import AppConfig
    home = Path(home).resolve()
    config = AppConfig.load(str(home / "config.yaml"), create=False)
    credentials = Path(config.bilibili.credentials_file).expanduser()
    credentials = credentials if credentials.is_absolute() else home / credentials
    state_path = home / "data/broadcast/browser-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    stopped = threading.Event()
    # No js_api: a remote page has no access to Studio's filesystem/process bridge.
    window = webview.create_window("QQBot Live2D · 直播姬窗口捕获" if preview else "B站 · QQBot 内置浏览器",
        url=url if preview else validate_url(url), width=1280, height=800,
        background_color="#20242d" if preview else "#ffffff")
    def state(status):
        temporary = state_path.with_name(state_path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps({"state": status, "pid": os.getpid(), "updated": time.time()}), encoding="utf-8")
            os.replace(temporary, state_path)
        finally:
            temporary.unlink(missing_ok=True)
    def collect():
        previous = None
        state("waiting_for_login")
        if reset:
            window.clear_cookies()
            credentials.unlink(missing_ok=True)
            window.load_url(START_URL)
        while not stopped.wait(2):
            try:
                validate_url(window.get_current_url())
                values = credentials_from_cookies(window.get_cookies())
                if values.get("sessdata") and values.get("bili_jct") and values != previous:
                    save_credentials(credentials, values)
                    previous = values
                    state("login_saved")
            except Exception:
                # Navigation/captcha/closing are normal; never log cookies or browser errors.
                pass
    window.events.closed += stopped.set
    def run():
        if not preview:
            collect()
    webview.start(run, gui="edgechromium" if os.name == "nt" else None,
        private_mode=preview, storage_path=str(home / ("data/studio/livehime-webview" if preview else "data/broadcast/browser-profile")))
    stopped.set()
    return 0


def main():
    from runtime_paths import application_home
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=application_home())
    parser.add_argument("--url", default=START_URL)
    parser.add_argument("--reset-session", action="store_true")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    if args.preview:
        parsed = urlsplit(args.url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path != "/overlay":
            raise ValueError("模型预览必须为本机 overlay 地址。")
    return launch(args.home, url=args.url, reset=args.reset_session, preview=args.preview)


if __name__ == "__main__":
    raise SystemExit(main())
