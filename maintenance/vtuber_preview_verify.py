"""Real isolated Chromium/WebGL/audio smoke check; never touches a user browser or OBS."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import wave

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from browser_control import installed_browser
from vtuber_config import VTuberConfig
from vtuber_host import VTuberServiceHost, execute, overlay_url


def verify(output_directory):
    from PIL import Image
    from playwright.sync_api import sync_playwright
    output_directory = Path(output_directory).resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qqbot-vtuber-preview-", ignore_cleanup_errors=True) as folder:
        root = Path(folder)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = VTuberConfig(port=port, credentials_file=str(root / "credentials.json"), vts_token_file=str(root / "vts-token.json"))
        host = VTuberServiceHost(config)
        host.thread.start()
        if not host.ready.wait(10) or host.error:
            raise RuntimeError("Cannot start fixture service: " + str(host.error))
        profile = root / "chromium-profile"
        guarded = bool(os.environ.get("QQBOT_TEST_SANDBOX"))
        browser_flags = [] if guarded else ["--autoplay-policy=no-user-gesture-required", "--enable-unsafe-swiftshader"]
        process = subprocess.Popen([str(installed_browser()), "--headless=new", "--disable-background-networking",
            "--disable-component-update", "--disable-sync", "--no-pings", "--no-first-run", "--no-default-browser-check",
            *browser_flags,
            "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", "--user-data-dir=" + str(profile),
            "--window-size=1000,700", "--force-device-scale-factor=1", f"http://127.0.0.1:{port}/overlay"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            if guarded:
                from testing_sandbox import own_browser_service, playwright_fixture_driver
                own_browser_service(process, profile)
            deadline = time.monotonic() + 15
            while not (profile / "DevToolsActivePort").is_file():
                if time.monotonic() > deadline: raise RuntimeError("Disposable Chromium did not start")
                time.sleep(0.05)
            browser_port = int((profile / "DevToolsActivePort").read_text().splitlines()[0])
            with (playwright_fixture_driver() if guarded else nullcontext()), sync_playwright() as driver:
                browser = driver.chromium.connect_over_cdp(f"http://127.0.0.1:{browser_port}")
                page = browser.contexts[0].new_page()
                page.set_viewport_size({"width": 1280, "height": 720})
                page.route("**/*", lambda route: route.continue_() if route.request.url.startswith((f"http://127.0.0.1:{port}/", "blob:", "data:")) else route.abort())
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(overlay_url(config), wait_until="networkidle")
                try:
                    page.wait_for_function("window.vtuberDebug?.loaded === true", timeout=30000)
                except Exception as exc:
                    raise RuntimeError("Live2D render failed: " + page.locator("#error").inner_text() + " " + str(errors)) from exc
                execute("event", {"type": "speech", "text": "你好，Live2D 已就绪。口型会跟随实际播放的声音。", "emotion": "happy"}, config)
                page.wait_for_function("document.querySelector('#caption').textContent.includes('Live2D')")
                page.screenshot(path=str(output_directory / "preview.png"))
                page.locator("#audio-enable").click()
                # A known local waveform verifies real playback-driven mouth movement.
                audio = root / "fixture-tone.wav"
                rate = 24000
                samples = [int(11500 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(rate * 2)]
                with wave.open(str(audio), "wb") as handle:
                    handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(rate)
                    handle.writeframes(struct.pack("<" + "h" * len(samples), *samples))
                execute("audio", {"path": str(audio), "text": "本机音频口型验证", "emotion": "happy"}, config)
                page.wait_for_function("window.vtuberDebug.lastAppliedMouth > 0.2", timeout=12000)
                mouth = page.evaluate("window.vtuberDebug.lastAppliedMouth")
                page.screenshot(path=str(output_directory / "audio-mouth.png"))
                remaining = page.evaluate("window.vtuberDebug.audio.duration - window.vtuberDebug.audio.currentTime")
                queued_at = time.monotonic()
                execute("audio", {"path": str(audio), "text": "第二段排队台词", "emotion": "neutral", "interrupt": False}, config)
                page.wait_for_function("window.vtuberDebug.audioQueue.length === 1", timeout=3000)
                assert page.locator("#caption").inner_text() == "本机音频口型验证"
                page.wait_for_function("document.querySelector('#caption').textContent === '第二段排队台词'", timeout=6000)
                assert time.monotonic() - queued_at >= remaining - 0.2, "Queued dialogue interrupted the current audio"
                page.wait_for_function("!window.vtuberDebug.audio && window.vtuberDebug.mouth < 0.01", timeout=10000)
                # PCM chunks use the same analyser, without waiting for a complete file.
                import base64
                pcm = struct.pack("<" + "h" * 12000, *samples[:12000])
                execute("pcm", {"pcm_base64": base64.b64encode(pcm).decode(), "sample_rate": rate,
                    "channels": 1, "sample_width": 2, "text": "PCM 流式口型验证", "emotion": "excited"}, config)
                page.wait_for_function("window.vtuberDebug.activeSources.size > 0 && window.vtuberDebug.lastAppliedMouth > 0.2", timeout=5000)
                execute("event", {"type": "motion", "group": "Idle", "index": 0}, config)
                page.goto(overlay_url(config, preview=False), wait_until="networkidle")
                page.wait_for_function("window.vtuberDebug.loaded === true", timeout=15000)
                execute("event", {"type": "reset"}, config)
                page.wait_for_function("document.querySelector('#caption').textContent === ''")
                page.screenshot(path=str(output_directory / "transparent-overlay.png"), omit_background=True)
                with Image.open(output_directory / "transparent-overlay.png") as image:
                    alpha = image.convert("RGBA").getchannel("A")
                    transparent_corner = alpha.getpixel((0, 0)) == 0
                    opaque_pixels = sum(alpha.histogram()[101:])
                assert transparent_corner, "OBS overlay background is not transparent"
                assert opaque_pixels > 10000, "Model did not actually render enough pixels"
                status = execute("status", config=config)
                assert status["state"]["model_loaded"]
                assert not status["vts"]["connected"] and not status["obs"]["connected"]
                assert not errors, errors
                report = {"ok": True, "renderer": "Pixi 6.5.10 + pixi-live2d-display 0.4.0 + Live2D Cubism Core",
                    "model": status["model"], "audio_peak_mouth": round(mouth, 4), "pcm_stream": True, "queued_audio": True,
                    "transparent_corner": transparent_corner, "opaque_model_pixels": opaque_pixels,
                    "browser_errors": errors, "external_services_connected": False,
                    "screenshots": ["preview.png", "audio-mouth.png", "transparent-overlay.png"]}
                (output_directory / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                page.evaluate("window.vtuberDebug.socket.close(1000)")
                page.wait_for_function("window.vtuberDebug.socket.readyState === WebSocket.CLOSED")
                page.close()
                return report
        finally:
            host.stop()
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/vtuber-preview")
    args = parser.parse_args()
    print(json.dumps(verify(args.output), ensure_ascii=False, indent=2))
