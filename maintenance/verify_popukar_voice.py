"""Real Studio preview, Japanese frontend and SILK roundtrip for Popukar."""
from __future__ import annotations

from array import array
import io
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "maintenance_reports/popukar-voice-20261114"
sys.path.insert(0, str(ROOT))


def verify():
    from config import AppConfig
    from playwright.sync_api import sync_playwright
    from pysilk import decode
    from ui.studio_api import StudioAPI
    from voice_engine import engine_status
    from voice_service import SILK_HEADER

    REPORT.mkdir(parents=True, exist_ok=True)
    result = {"ok": False, "transport": "real local StudioAPI + GPT-SoVITS v4 + SILK; no QQ send"}
    with tempfile.TemporaryDirectory(prefix="qqbot-popukar-studio-", ignore_cleanup_errors=True) as folder:
        home = Path(folder)
        config = AppConfig()
        live = AppConfig.load(str(ROOT / "config.yaml")).voice
        config.voice = live.model_copy(update={"enabled": True, "character": "atri", "default_language": "ja",
            "catalog_path": str(ROOT / "data/voice/catalog.json"), "cache_dir": str(REPORT / "voice-cache"),
            "engine_python": str(ROOT / live.engine_python), "engine_directory": str(ROOT / live.engine_directory),
            "engine_config": str(ROOT / "tools/voice-engine/popukar-infer.yaml"), "auto_start": True,
            "timeout_seconds": 600})
        config.save(str(home / "config.yaml"))
        api = StudioAPI(home)
        allowed = {"bootstrap", "poll", "records", "glossary", "voice", "preferences", "save_config"}

        def invoke(name, args):
            if name not in allowed:
                raise RuntimeError("Unexpected UI operation: " + name)
            return getattr(api, name)(*args)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1320, "height": 860}, reduced_motion="reduce")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.expose_function("qqbotStudioBridge", invoke)
            page.add_init_script("window.pywebview={api:new Proxy({}, {get:(_,name)=>(...args)=>window.qqbotStudioBridge(name,args)})};")
            page.goto((ROOT / "ui/web/index.html").as_uri())
            page.wait_for_function("window.studio && window.studio.ready")
            page.locator('[data-page="voice"]').click()
            select = page.locator('select[data-config="voice.character"]')
            select.wait_for()
            assert select.locator('option[value="popukar"]').count() == 1
            assert "泡普卡" in select.locator('option[value="popukar"]').inner_text()
            assert page.locator("#voice-emotion option").count() == 3
            page.locator("#voice-emotion").select_option("sad")
            select.select_option("popukar")
            assert page.locator("#voice-emotion").input_value() == "neutral"
            assert page.locator("#voice-emotion option").count() == 1
            page.locator('[data-action="save-config"]').click()
            page.wait_for_function("!dirty()")
            assert AppConfig.load(str(home / "config.yaml")).voice.character == "popukar"
            print("角色选择、情绪更新与保存：通过", flush=True)

            page.locator("details.settings-group summary").click()
            page.locator('[data-action="start-voice-engine"]').click()
            page.wait_for_function("S.status.task?.kind==='voice-engine' && S.status.task.state!=='running'", timeout=180000)
            assert api._task["state"] == "completed", api._task
            print("控制台启动 v4 推理引擎：通过", flush=True)
            page.locator('[data-action="check-voice"]').click()
            page.wait_for_function("S.status.task?.kind==='voice-check' && S.status.task.state!=='running'", timeout=30000)
            assert api._task.get("result", {}).get("reachable"), api._task

            text = "こんにちは。今日も一緒に本を読みましょう。"
            page.locator("#voice-text").fill(text)
            started = time.monotonic()
            page.locator('[data-action="preview-voice"]').click()
            page.wait_for_function("S.status.task?.kind==='voice-preview' && S.status.task.state!=='running'", timeout=650000)
            assert api._task["state"] == "completed", {k: v for k, v in api._task.items() if k != "result"}
            sample = api._task["result"]
            assert sample["character"] == "popukar" and sample["emotion"] == "neutral"
            assert sample["state"] == "ready" and sample["language"] == "ja"
            assert sample["chunks"] and all(not re.search(r"[A-Za-z0-9\u3400-\u9fff]", c["text"]) for c in sample["chunks"])
            with wave.open(sample["wav_path"], "rb") as source:
                assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (24000, 1, 2)
                pcm = array("h", source.readframes(source.getnframes()))
            rms = math.sqrt(sum(v * v for v in pcm) / max(1, len(pcm)))
            assert rms > 50, "Synthesis is empty or near silent"
            silk = Path(sample["silk_path"]).read_bytes()
            assert silk.startswith(SILK_HEADER)
            decoded = io.BytesIO()
            decode(io.BytesIO(silk), decoded, sample_rate=24000)
            decoded_duration = len(decoded.getvalue()) / 2 / 24000
            assert abs(decoded_duration - sample["duration"]) <= .15
            page.wait_for_function("document.querySelector('.voice-preview audio')?.readyState>=1")
            assert page.locator(".voice-preview audio").evaluate("e=>e.duration") > 1
            page.locator("details.settings-group summary").click()
            page.screenshot(path=str(REPORT / "popukar-studio.png"))
            page.set_viewport_size({"width": 940, "height": 640})
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth && document.querySelector('#main-content').scrollWidth<=document.querySelector('#main-content').clientWidth+1")
            result.update(ok=not errors, character="popukar", emotion="neutral", language="ja",
                character_dropdown=True, switching_resets_emotion=True, config_saved=True,
                engine=engine_status(), synthesis_seconds=round(time.monotonic() - started, 2),
                reference=sample["reference"], chunks=[{"language": c["language"], "text": c["text"]} for c in sample["chunks"]],
                wav=sample["wav_path"], silk=sample["silk_path"], manifest=sample["manifest_path"],
                wav_duration=sample["duration"], silk_decoded_duration=decoded_duration, rms=round(rms, 2),
                preview_audio_loaded=True, compact_no_overflow=True, js_errors=errors)
            browser.close()
            print("真实控制台合成、WAV 播放与 SILK 解码：通过", flush=True)
    (REPORT / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    assert result["ok"], result
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    import os
    os.chdir(ROOT)
    verify()
