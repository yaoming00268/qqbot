"""Real PCM playback, stop, cache/settings and layout checks in a local WebView page."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPORT = ROOT / "maintenance_reports/voicestudio-20261116"


def main():
    from config import AppConfig
    from playwright.sync_api import sync_playwright
    from ui.studio_api import StudioAPI
    with tempfile.TemporaryDirectory(prefix="qqbot-voicestudio-ui-", ignore_cleanup_errors=True) as directory:
        home = Path(directory).resolve()
        config = AppConfig()
        config.voice = AppConfig.load(str(REPORT / "voice-config.yaml")).voice
        for key in ("catalog_path", "cache_dir", "engine_python", "engine_directory", "engine_config"):
            setattr(config.voice, key, str((ROOT / getattr(config.voice, key)).resolve()))
        config.voice.voicestudio.source_directory = str((ROOT / config.voice.voicestudio.source_directory).resolve())
        config.voice.cache_dir = str(REPORT / "ui-voice-jobs")
        for key in ("engine_python", "engine_directory", "generation_python", "generation_directory"):
            setattr(config.voice.singing, key, str((ROOT / getattr(config.voice.singing, key)).resolve()))
        config.save(str(home / "config.yaml"))
        api = StudioAPI(home)
        allowed = {"bootstrap", "poll", "records", "glossary", "voice", "singing", "preferences", "save_config"}
        events = []
        def invoke(name, args):
            assert name in allowed, name
            result = getattr(api, name)(*args)
            if name == "poll" and result.get("ok"):
                events.extend(result["data"]["events"])
            return result
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True,
                args=["--autoplay-policy=no-user-gesture-required"])
            page = browser.new_page(viewport={"width": 1320, "height": 860}, reduced_motion="reduce")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.expose_function("qqbotStudioBridge", invoke)
            page.add_init_script("window.pywebview={api:new Proxy({}, {get:(_,name)=>(...args)=>window.qqbotStudioBridge(name,args)})};")
            page.goto((ROOT / "ui/web/index.html").as_uri())
            page.wait_for_function("window.studio?.ready")
            smoke = page.evaluate("window.studio.smoke()")
            assert smoke["ok"], smoke
            page.locator('[data-page="voice"]').click()
            page.wait_for_selector("#voice-emotion option", state="attached")
            page.locator("#voice-text").fill("こんにちは。一緒に本を読みましょう。")
            page.locator('[data-action="insert-voice-emotion"]').click()
            assert "[emotion:neutral]" in page.locator("#voice-text").input_value()
            page.locator("#voice-text").fill("こんにちは。一緒に本を読みましょう。")
            page.locator('[data-action="preview-voice"]').click()
            page.wait_for_function("voiceAudioNodes.size>0", timeout=90000)
            during = page.evaluate("({task:S.status.task, context:voiceAudioContext.state, nodes:voiceAudioNodes.size, tail:voiceAudioTail})")
            assert during["context"] == "running" and during["task"]["state"] == "running", during
            page.screenshot(path=str(REPORT / "voice-stream-playing.png"))
            page.wait_for_function("S.voicePreview?.state==='ready' && S.status.task?.state!=='running'", timeout=90000)
            page.wait_for_function("document.querySelector('.voice-preview audio')?.readyState>=1")
            preview = page.evaluate("({metrics:S.voicePreview.metrics,chunks:S.voicePreview.chunks,audioDuration:document.querySelector('.voice-preview audio').duration})")
            assert preview["audioDuration"] > 1
            page.screenshot(path=str(REPORT / "voice-studio-light.png"), full_page=True)
            page.evaluate("S.preferences.theme='dark';applyAppearance(S.preferences)")
            page.screenshot(path=str(REPORT / "voice-studio-dark.png"), full_page=True)
            page.set_viewport_size({"width": 940, "height": 640})
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth && document.querySelector('#main-content').scrollWidth<=document.querySelector('#main-content').clientWidth+1")
            page.screenshot(path=str(REPORT / "voice-studio-compact.png"), full_page=True)
            page.locator("#voice-text").fill("いっしょにほんをよみましょう。" * 12)
            page.locator('[data-action="preview-voice"]').click()
            page.wait_for_function("voiceAudioNodes.size>0", timeout=90000)
            page.locator('[data-action="cancel-voice"]').click()
            page.wait_for_function("S.status.task?.state==='cancelled'", timeout=15000)
            assert page.evaluate("voiceAudioNodes.size===0")
            page.set_viewport_size({"width": 1320, "height": 860})
            page.locator('[data-page="singing"]').click()
            page.wait_for_selector("#sing-mode")
            page.screenshot(path=str(REPORT / "singing-denoise-settings.png"), full_page=True)
            assert not errors, errors
            result = {"ok": True, "errors": errors, "smoke": smoke, "streaming_during_synthesis": during,
                "preview": preview, "pcm_events": sum(e["kind"] == "voice_audio" for e in events),
                "cancelled_and_stopped_playback": True, "no_qq_send": True}
            (REPORT / "ui-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()
    print(json.dumps({"ok": True, "report": str(REPORT / "ui-verification.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
