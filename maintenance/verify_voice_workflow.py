"""Real local ATRI synthesis and SILK roundtrip; never invokes QQ transport."""
from __future__ import annotations

import argparse
import asyncio
from array import array
import io
import json
import math
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def verify(report):
    from voice_config import VoiceConfig
    from voice_service import VoiceService, SILK_HEADER
    from voice_engine import engine_status, ready, stop_engine
    from pysilk import decode
    import psutil
    config = VoiceConfig(enabled=True, auto_start=True)
    before = engine_status()
    status = await ready(config)
    pid = status.get("pid")
    children = [p.pid for p in psutil.Process(pid).children(recursive=True)] if status.get("owned") else []
    result = {"ok": False, "transport": "local synthesis and codec only; no QQ send", "engine": status, "samples": []}
    try:
        for name, emotion, language, text in (
            ("japanese-neutral", "neutral", "ja", "こんにちは。一緒に本を読みましょう。"),
            ("japanese-sad", "sad", "ja", "悲しまないでください。そばにいます。"),
            ("mixed-firm", "firm", "mixed", "[ja]いっしょに。[/ja][zh]你好。[/zh][en]Hello.[/en]")):
            local = config.model_copy(update={"default_language": language})
            manifest = await VoiceService(local).synthesize(text, emotion)
            with wave.open(manifest["wav_path"], "rb") as source:
                pcm = array("h", source.readframes(source.getnframes()))
                assert source.getframerate() == 24000 and source.getnchannels() == 1
            rms = math.sqrt(sum(value * value for value in pcm) / max(1, len(pcm)))
            silk = Path(manifest["silk_path"]).read_bytes()
            assert silk.startswith(SILK_HEADER)
            decoded = io.BytesIO()
            decode(io.BytesIO(silk), decoded, sample_rate=24000)
            duration = len(decoded.getvalue()) / 2 / 24000
            assert rms > 50, "Synthesis is empty or near silent"
            assert abs(duration - manifest["duration"]) <= .15, "SILK roundtrip lost audio duration"
            result["samples"].append({"name": name, "emotion": emotion, "language": language,
                "wav_duration": manifest["duration"], "silk_decoded_duration": duration,
                "rms": round(rms, 2), "wav": manifest["wav_path"], "silk": manifest["silk_path"],
                "manifest": manifest["manifest_path"], "chunks": [{"language": c["language"], "text": c["text"]} for c in manifest["chunks"]]})
            print(name, "passed", flush=True)
        result["ok"] = True
    except BaseException as error:
        result["error"] = str(error)
        raise
    finally:
        if not before.get("running") and status.get("owned") and engine_status().get("pid") == pid:
            result["stop"] = await asyncio.to_thread(stop_engine)
            result["all_owned_processes_stopped"] = all(not psutil.pid_exists(value) for value in [pid, *children])
            result["ok"] = result["ok"] and result["all_owned_processes_stopped"]
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not result["ok"]:
        raise RuntimeError("Voice workflow verification failed")


if __name__ == "__main__":
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "maintenance_reports/voice-dsh-20261114/voice-verification.json")
    args = parser.parse_args()
    os.chdir(ROOT)
    asyncio.run(verify(args.report))
