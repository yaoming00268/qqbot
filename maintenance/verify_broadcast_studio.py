"""Exercise the actual broadcast UI and bridge with a disposable, account-free home."""
from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def verify(report):
    from playwright.sync_api import sync_playwright
    from browser_control import installed_browser
    from ui.studio_api import StudioAPI
    from config import AppConfig
    from broadcast_host import stop_server
    report = Path(report).resolve()
    report.mkdir(parents=True, exist_ok=True)
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "ui/web")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="qqbot-broadcast-ui-", ignore_cleanup_errors=True) as folder, sync_playwright() as driver:
            home = Path(folder)
            config_path = home / "config.yaml"
            config = AppConfig()
            config.bilibili.port = free_port()
            config.vtuber.port = free_port()
            config.vtuber.credentials_file = str(home / "data/vtuber/credentials.json")
            catalog = home / "live-catalog.json"
            catalog.write_text(json.dumps({"version": 1, "characters": {
                "live-neutral": {"name": "直播默认声音", "emotions": {"neutral": {"description": "平静"}}},
                "live-sad": {"name": "直播独立声音", "emotions": {"sad": {"description": "轻声难过"}}}
            }}), encoding="utf-8")
            config.voice.character = "qq-only"
            config.broadcast_voice.character = "live-neutral"
            config.broadcast_voice.catalog_path = str(catalog)
            original_qq = config.voice.model_dump()
            config.save(str(config_path))
            api = StudioAPI(home)
            api.bootstrap()
            browser = driver.chromium.launch(executable_path=str(installed_browser()), headless=True,
                args=["--disable-background-networking", "--disable-component-update", "--disable-sync", "--no-pings"])
            try:
                page = browser.new_page(viewport={"width": 1320, "height": 860}, device_scale_factor=1)
                origin = f"http://127.0.0.1:{server.server_port}/"
                page.route("**/*", lambda route: route.continue_() if route.request.url.startswith((origin, "blob:", "data:")) else route.abort())
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                methods = [name for name in dir(api) if not name.startswith("_") and callable(getattr(api, name))]
                failures = []
                def invoke(method, values):
                    result = getattr(api, method)(*values)
                    if isinstance(result, dict) and not result.get("ok", True):
                        failures.append({"method": method, "error": result.get("error")})
                    return result
                page.expose_function("studioBridge", invoke)
                page.add_init_script("window.pywebview={api:Object.fromEntries(" + json.dumps(methods) + ".map(name=>[name,(...values)=>window.studioBridge(name,values)]))};")
                page.goto(origin + "index.html")
                page.wait_for_function("window.studio?.ready")
                page.locator('.sidebar [data-page="broadcast"]').click()
                page.wait_for_function("S.page === 'broadcast' && S.broadcast?.sdk_ready === true")
                page.wait_for_timeout(400)
                assert page.locator('[data-config="broadcast_voice.character"] option[value="live-sad"]').count() == 1
                page.locator('[data-config="broadcast_voice.character"]').select_option("live-sad")
                page.wait_for_function("S.broadcastText.emotion === 'sad'")
                emotion_options = page.locator('[data-broadcast-text="emotion"] option').all_text_contents()
                assert page.locator('[data-broadcast-text="emotion"]').input_value() == "sad"
                assert len(emotion_options) == 1 and any("\u4e00" <= char <= "\u9fff" for char in emotion_options[0])
                page.locator('[data-config="broadcast_voice.delivery_mode"]').select_option("validated")
                page.locator('[data-action="broadcast-voice-save"]').click()
                page.wait_for_function("S.savedConfig.broadcast_voice.delivery_mode === 'validated'")
                saved_voice = AppConfig.load(str(config_path))
                assert saved_voice.broadcast_voice.character == "live-sad"
                assert saved_voice.broadcast_voice.delivery_mode == "validated"
                assert saved_voice.voice.model_dump() == original_qq
                page.locator('[data-config="broadcast_voice.delivery_mode"]').select_option("streaming")
                page.locator('[data-action="broadcast-voice-save"]').click()
                page.wait_for_function("S.savedConfig.broadcast_voice.delivery_mode === 'streaming'")
                page.screenshot(path=str(report / "broadcast-light.png"))
                text = "离线动态草稿：<img src=x onerror=alert(1)>\n最后一行也必须完整保留。"
                page.locator('[data-broadcast-draft="text"]').fill(text)
                page.locator('[data-action="broadcast-save-draft"]').click()
                try:
                    page.wait_for_function("Boolean(S.broadcastDraft?.draft_id)", timeout=10000)
                except Exception:
                    page.screenshot(path=str(report / "failure.png"))
                    print(json.dumps({"bridge_errors": failures, "javascript_errors": errors,
                                      "toasts": page.locator("#toast-region").inner_text(),
                                      "draft": page.evaluate("S.broadcastDraft")}, ensure_ascii=False))
                    raise
                page.locator('[data-action="broadcast-preview-draft"]').click()
                page.wait_for_selector("#dialog[open]")
                page.wait_for_timeout(300)
                assert text in page.locator("#dialog-body").inner_text().replace("\\n", "\n")
                assert page.locator("#dialog-body img").count() == 0
                page.screenshot(path=str(report / "draft-preview.png"))
                page.locator("#dialog-confirm").click()
                page.locator('[data-action="broadcast-bind-actor"]').click()
                page.wait_for_function("Boolean(S.config.bilibili.actor_id)")
                saved = AppConfig.load(str(config_path))
                assert saved.bilibili.actor_id in saved.agent.actors
                assert not saved.bilibili.allow_publish and not saved.bilibili.auto_reply
                avatar_options = page.locator('[data-broadcast-text="avatar_emotion"] option').evaluate_all("els => els.map(el => el.value)")
                assert len(avatar_options) == 8 and "happy" in avatar_options
                page.evaluate("applyAppearance({...S.preferences,theme:'dark'})")
                page.set_viewport_size({"width": 940, "height": 640})
                page.wait_for_timeout(400)
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth && document.querySelector('main').scrollWidth<=document.querySelector('main').clientWidth")
                page.screenshot(path=str(report / "broadcast-dark-small.png"))
                assert not errors, errors
                result = {"ok": True, "real_bridge": True, "draft_roundtrip": True, "full_preview_escaped": True,
                          "live_voice_settings_independent": True, "live_character_emotion_dropdown": True,
                          "actor_identity_bound": True, "avatar_emotions": avatar_options, "minimum_viewport": [940, 640],
                          "javascript_errors": errors, "live_accounts_used": False,
                          "screenshots": [str(p) for p in report.glob("*.png") if p.name != "failure.png"]}
                (report / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                return result
            finally:
                browser.close()
                stop_server(config_path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.report), ensure_ascii=False))
