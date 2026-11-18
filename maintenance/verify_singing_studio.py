"""Exercise the real local Studio bridge, without contacting a QQ transport."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPORT = ROOT / "maintenance_reports/singing-20261114"


def verify(mode=""):
    from config import AppConfig
    from playwright.sync_api import sync_playwright
    from ui.studio_api import StudioAPI
    REPORT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qqbot-singing-studio-", ignore_cleanup_errors=True) as folder:
        home = Path(folder)
        config = AppConfig()
        voice = AppConfig.load(str(ROOT / "config.yaml")).voice
        config.voice = voice.model_copy(deep=True)
        for key in ("catalog_path", "engine_python", "engine_directory", "engine_config", "cache_dir"):
            setattr(config.voice, key, str((ROOT / getattr(config.voice, key)).resolve()))
        for key in ("engine_python", "engine_directory", "generation_python", "generation_directory"):
            setattr(config.voice.singing, key, str((ROOT / getattr(config.voice.singing, key)).resolve()))
        config.voice.singing.cache_dir = str(REPORT / "jobs")
        config.save(str(home / "config.yaml"))
        api = StudioAPI(home)
        allowed = {"bootstrap", "poll", "records", "glossary", "voice", "singing", "preferences", "save_config"}
        def invoke(name, args):
            if name not in allowed:
                raise RuntimeError("Unexpected bridge operation: " + name)
            return getattr(api, name)(*args)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1320, "height": 860}, reduced_motion="reduce")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.expose_function("qqbotStudioBridge", invoke)
            page.add_init_script("window.pywebview={api:new Proxy({}, {get:(_,name)=>(...args)=>window.qqbotStudioBridge(name,args)})};")
            page.goto((ROOT / "ui/web/index.html").as_uri())
            page.wait_for_function("window.studio?.ready")
            smoke = page.evaluate("window.studio.smoke()")
            assert smoke["ok"], smoke
            page.locator('[data-page="singing"]').click()
            page.wait_for_function("S.voiceCatalog?.catalog?.characters?.popukar")
            assert page.locator("#sing-emotion option").all_text_contents() == ["neutral"]
            page.locator("#sing-mode").select_option("compose")
            assert page.locator("#sing-style").count() == 1
            page.locator("#sing-song").fill(str(REPORT / "original-accompaniment.wav"))
            lyrics_path = REPORT / "original-lyrics.txt"
            if lyrics_path.is_file():
                page.locator("#sing-lyrics").fill(lyrics_path.read_text(encoding="utf-8"))
            page.locator("#sing-output").select_option("both")
            page.screenshot(path=str(REPORT / "singing-studio-light.png"), full_page=True)
            page.evaluate("S.preferences.theme='dark';applyAppearance(S.preferences)")
            page.screenshot(path=str(REPORT / "singing-studio-dark.png"), full_page=True)
            page.set_viewport_size({"width": 940, "height": 640})
            page.locator('#navigation [data-page="singing"]').scroll_into_view_if_needed()
            assert not page.evaluate("document.documentElement.scrollWidth>innerWidth || document.querySelector('#main-content').scrollWidth>document.querySelector('#main-content').clientWidth+1")
            page.screenshot(path=str(REPORT / "singing-studio-compact.png"), full_page=True)
            result = {"ok": not errors, "errors": errors, "smoke": smoke, "no_qq_send": True}
            if mode:
                if mode == "cover":
                    composition = json.loads((REPORT / "compose-result.json").read_text(encoding="utf-8"))
                    page.locator("#sing-mode").select_option("cover")
                    page.locator("#sing-song").fill(composition["wav_path"])
                page.locator('[data-action="render-singing"]').click()
                deadline, last = time.monotonic() + 2400, ""
                while time.monotonic() < deadline:
                    page.wait_for_timeout(700)
                    state = page.evaluate("({task:S.status.task,status:S.singingStatus})")
                    if state["status"] != last:
                        last = state["status"]
                        print(last, flush=True)
                    task = api._task
                    if task and task["state"] != "running":
                        assert task["state"] == "completed", {k:v for k,v in task.items() if k != "result"}
                        render = {k:v for k,v in task["result"].items() if k != "mp3_base64"}
                        (REPORT / (mode + "-result.json")).write_text(json.dumps(render, ensure_ascii=False, indent=2), encoding="utf-8")
                        page.wait_for_function("Boolean(S.singingResult)")
                        page.screenshot(path=str(REPORT / (mode + "-complete.png")), full_page=True)
                        result["render"] = render
                        break
                else:
                    api.singing("cancel")
                    raise TimeoutError("真实唱歌验收超时，已请求停止。")
            (REPORT / ("studio-" + (mode or "ui") + "-verification.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()
            print(json.dumps({"ok": result["ok"], "mode": mode or "ui", "pages": len(smoke["pages"]), "no_qq_send": True}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--render", choices=("cover", "compose"), default="")
    args = parser.parse_args()
    verify(args.render)
