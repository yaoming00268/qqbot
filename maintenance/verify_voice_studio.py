"""Visual/browser checks against the real bridge and a verified local preview WAV."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def verify(report):
    from config import AppConfig
    from ui.studio_api import StudioAPI
    from playwright.sync_api import sync_playwright
    report.mkdir(parents=True, exist_ok=True)
    synthesis = json.loads((report / "voice-verification.json").read_text(encoding="utf-8"))
    assert synthesis["ok"]
    sample = json.loads(Path(synthesis["samples"][0]["manifest"]).read_text(encoding="utf-8"))
    async def preview(service, *args, **kwargs):
        assert service.config.enabled and service.config.max_duration_seconds <= 30
        return dict(sample)
    with tempfile.TemporaryDirectory(prefix="qqbot-voice-visual-", ignore_cleanup_errors=True) as directory:
        home = Path(directory).resolve()
        config = AppConfig()
        config.voice.catalog_path = str(ROOT / "data/voice/catalog.json")
        config.save(str(home / "config.yaml"))
        api = StudioAPI(home)
        allowed = {"bootstrap", "poll", "records", "glossary", "voice", "preferences"}
        def invoke(name, args):
            if name not in allowed:
                raise RuntimeError("Unexpected UI operation: " + name)
            return getattr(api, name)(*args)
        with patch("voice_service.VoiceService.synthesize", preview), sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1320, "height": 860}, device_scale_factor=1, reduced_motion="reduce")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.expose_function("qqbotStudioBridge", invoke)
            page.add_init_script("window.pywebview={api:new Proxy({}, {get:(_,name)=>(...args)=>window.qqbotStudioBridge(name,args)})};")
            page.goto((ROOT / "ui/web/index.html").as_uri())
            page.wait_for_function("window.studio && window.studio.ready")
            page.locator('[data-page="voice"]').click()
            page.wait_for_selector("#voice-emotion option", state="attached")
            assert page.locator("#voice-emotion option").count() == 3
            page.locator('[data-action="preview-voice"]').click()
            page.wait_for_function("document.querySelector('.voice-preview audio')?.readyState >= 1")
            assert page.locator(".voice-preview audio").evaluate("e=>e.duration") > 1
            page.locator('[data-action="preview-voice"]').hover()
            assert page.locator('[data-action="preview-voice"]').evaluate("e=>getComputedStyle(e).backgroundColor") == "rgb(66, 104, 207)"
            page.screenshot(path=str(report / "voice-studio-light.png"))
            page.evaluate("S.preferences.theme='dark'; applyAppearance(S.preferences)")
            page.screenshot(path=str(report / "voice-studio-dark.png"))
            page.set_viewport_size({"width": 940, "height": 640})
            compact = page.evaluate("document.documentElement.scrollWidth<=innerWidth && document.querySelector('#main-content').scrollWidth<=document.querySelector('#main-content').clientWidth+1")
            page.screenshot(path=str(report / "voice-studio-compact.png"))
            page.set_viewport_size({"width": 1320, "height": 860})
            page.evaluate("async()=>{S.preferences.theme='light'; applyAppearance(S.preferences); await navigate('settings'); S.settingTab='model'; render()}")
            page.screenshot(path=str(report / "dsh-settings.png"))
            assert page.locator('[data-config="model_backend.backend"]').count() == 1
            result = {"ok": not errors and compact, "backend": "real local StudioAPI with verified synthesis output fixture",
                      "preview_audio_loaded": True, "primary_hover_readable": True, "emotions": 3, "compact_no_overflow": compact, "js_errors": errors}
            browser.close()
    (report / "studio-visual.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    assert result["ok"], result
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "maintenance_reports/voice-dsh-20261114")
    verify(parser.parse_args().report.resolve())
