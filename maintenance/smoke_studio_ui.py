"""Exercise the real Studio bridge through a disposable local browser and data home."""
from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from playwright.sync_api import sync_playwright
    from browser_control import installed_browser
    from ui.studio_api import StudioAPI
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = args.report.resolve()
    report.mkdir(parents=True, exist_ok=True)
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "ui/web")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="qqbot_studio_ui_") as folder, sync_playwright() as pw:
            api = StudioAPI(folder)
            api.bootstrap()
            browser = pw.chromium.launch(executable_path=str(installed_browser()), headless=True)
            page = browser.new_page(viewport={"width": 1320, "height": 860}, device_scale_factor=1)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            methods = [name for name in dir(api) if not name.startswith("_") and callable(getattr(api, name))]
            page.expose_function("studioBridge", lambda method, values: getattr(api, method)(*values))
            page.add_init_script("window.pywebview={api:Object.fromEntries(" + json.dumps(methods) +
                                 ".map(name=>[name,(...values)=>window.studioBridge(name,values)]))};")
            page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
            page.wait_for_function("window.studio?.ready && document.querySelector('h1')?.textContent==='工作空间'")
            page.wait_for_timeout(350)
            page.screenshot(path=str(report / "overview-light.png"))
            def nav(name):
                locator = page.locator(f'.sidebar [data-page="{name}"]')
                (locator if locator.count() else page.locator(f'[data-page="{name}"]').first).click()
                page.wait_for_selector(f'[data-view="{name}"]')
                page.wait_for_timeout(260)
            nav("settings")
            page.locator('[data-config="onebot.active_account"]').select_option("account_2")
            page.locator('[data-config="onebot.accounts.account_2.ws_url"]').fill("ws://127.0.0.1:4302")
            page.locator('#save-global').click()
            page.wait_for_function("document.querySelector('#save-global').disabled")
            assert api._config().onebot.ws_url == "ws://127.0.0.1:4302"
            assert api._config().onebot.accounts["account_2"].ws_url == "ws://127.0.0.1:4302"
            page.locator('[data-settings-tab="queue"]').click()
            page.locator('[data-config="bot.backlog_interrupt_messages"]').fill("6")
            page.locator('#save-global').click()
            page.wait_for_function("document.querySelector('#save-global').disabled")
            assert api._config().bot.backlog_interrupt_messages == 6
            nav("appearance")
            page.locator('[data-theme-choice="dark"]').click()
            page.wait_for_function("document.documentElement.dataset.theme==='dark'")
            page.locator('[data-preference="surface_opacity"]').fill("56")
            page.locator('[data-preference="window_opacity"]').fill("86")
            page.wait_for_function("document.documentElement.style.getPropertyValue('--density')==='0.56'")
            page.wait_for_timeout(350)
            assert api._preferences["window_opacity"] == 86
            page.evaluate("document.querySelector('main').scrollTop=0")
            page.screenshot(path=str(report / "appearance-dark.png"))
            page.locator('[data-theme-choice="light"]').click()
            page.wait_for_function("document.documentElement.dataset.theme==='light'")
            page.wait_for_timeout(300)
            page.screenshot(path=str(report / "appearance-light.png"))
            nav("personas")
            page.locator('[data-action="new-persona"]').click()
            page.locator('[data-persona-field="name"]').fill("本地验收角色")
            page.locator('[data-persona-field="description"]').fill("验收使用，保留持久身份")
            page.locator('[data-persona-field="prompt"]').fill("用简洁自然的方式交流，延续已有关系和记忆。")
            page.locator('[data-action="save-persona"]').click()
            page.wait_for_selector('.persona-item:has-text("本地验收角色")')
            assert any(p["name"] == "本地验收角色" for p in api.bootstrap()["data"]["personas"]["items"])
            page.wait_for_timeout(300)
            page.screenshot(path=str(report / "personas.png"))
            api._consume_line('[FEED_MSG] '+json.dumps({"sender":"本地测试用户","user_id":"123456","group_id":"10001","content":"<img src=x onerror=alert(1)> 这是一条测试消息。"}))
            nav("messages")
            page.wait_for_selector('.message-text:has-text("测试消息")')
            assert page.locator('.message-text img').count() == 0
            page.locator('#message-query').fill("没有匹配")
            page.wait_for_selector('h3:has-text("没有匹配的消息")')
            page.locator('#message-query').fill("")
            nav("emotion")
            page.screenshot(path=str(report / "emotion.png"))
            page.locator('[data-emotion-tab="dataset"]').click()
            page.locator('#sample-text').fill("今天真开心")
            page.locator('#sample-label').select_option(label="喜悦")
            page.locator('[data-action="add-sample"]').click()
            page.locator('[data-action="save-dataset"]').click()
            page.wait_for_selector('.toast:has-text("已保存 1 条样本")')
            assert len(api.emotion("load_dataset")["data"]) == 1
            nav("memory")
            page.screenshot(path=str(report / "memory.png"))
            page.locator('[data-memory-tab="glossary"]').click()
            template = page.locator('#glossary-text').input_value()
            page.locator('#glossary-text').fill(template.replace("<!-- entries:end -->", "| | 本地验收 | | 一次真实保存 | #验收 | |\n<!-- entries:end -->"))
            page.locator('[data-action="save-glossary"]').click()
            page.wait_for_selector('.toast:has-text("话术表已保存")')
            assert "本地验收" in api.glossary()["data"]["text"]
            nav("appearance")
            before_size = page.locator('.range-label').first.evaluate("el=>parseFloat(getComputedStyle(el).fontSize)")
            page.locator('[data-preference="font_size"]').fill("17")
            page.wait_for_function("document.documentElement.style.getPropertyValue('--font-size')==='17px'")
            after_size = page.locator('.range-label').first.evaluate("el=>parseFloat(getComputedStyle(el).fontSize)")
            assert after_size > before_size, (before_size, after_size)
            nav("overview")
            page.set_viewport_size({"width": 940, "height": 640})
            page.screenshot(path=str(report / "overview-compact.png"))
            assert page.evaluate("document.querySelector('main').scrollWidth<=document.querySelector('main').clientWidth")
            for target in ("settings", "emotion", "autonomy", "resources", "appearance", "personas", "logs"):
                nav(target)
                assert page.evaluate("document.querySelector('main').scrollWidth<=document.querySelector('main').clientWidth"), target
            page.locator('[data-page="logs"]').first.wait_for(state="visible")
            assert not errors, errors
            result = {"ok": True, "javascript_errors": errors, "real_bridge": True,
                      "config_save": True, "preferences_persisted": True, "persona_create": True,
                      "dataset_save": True, "glossary_save": True, "account_connection_save": True,
                      "font_scaling": [before_size, after_size], "human_text_escaped": True, "minimum_viewport": [940,640],
                      "screenshots": [str(path) for path in report.glob('*.png')], "live_accounts_used": False}
            (report / "ui-smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


if __name__ == "__main__":
    main()
