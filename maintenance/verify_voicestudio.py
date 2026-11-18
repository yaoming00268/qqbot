"""Real local VoiceStudio/DSP validation. No QQ messages or account changes."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPORT = ROOT / "maintenance_reports/voicestudio-20261116"


def save(name, value):
    (REPORT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def audio_info(path):
    from array import array
    with wave.open(str(path)) as stream:
        samples = array("h", stream.readframes(stream.getnframes()))
        result = {"duration": stream.getnframes() / stream.getframerate(), "rate": stream.getframerate(),
                  "channels": stream.getnchannels(), "width": stream.getsampwidth()}
    result["rms"] = math.sqrt(sum(v*v for v in samples) / max(1, len(samples)))
    assert result["rms"] > 8 and result["width"] == 2, result
    return result


async def benchmark():
    from config import AppConfig
    from voice_service import VoiceService
    from voice_config import VoiceConfig
    config = AppConfig.load(str(REPORT / "voice-config.yaml")).voice
    text = "こんにちは。一緒に本を読みましょう。"
    old = ROOT / "runtime/releases/singing-20261114/QQBot/QQBot.exe"
    rows = []
    for index in range(2):
        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(str(old), "--home", str(ROOT), "--module", "voice",
            "preview", "--text", text, "--emotion", "neutral", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        output, _ = await asyncio.wait_for(process.communicate(), 200)
        (REPORT / f"baseline-{index}.log").write_bytes(output)
        assert process.returncode == 0, output.decode("utf-8", errors="replace")[-1000:]
        value = json.loads(output.decode("utf-8").strip().splitlines()[-1])
        rows.append({"kind": "previous-runtime", "index": index, "wall_ms": round((time.perf_counter()-started)*1000, 2),
                     "audio": audio_info(value["wav_path"]), "manifest": value["manifest_path"], "sample_steps": 32})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    config.reuse_audio = False
    for index in range(2):
        value = await VoiceService(config).synthesize(text, "neutral")
        rows.append({"kind": "voicestudio-uncached", "index": index, "metrics": value["metrics"],
                     "audio": audio_info(value["wav_path"]), "manifest": value["manifest_path"], "sample_steps": config.sample_steps})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    config.reuse_audio = True
    for index in range(2):
        value = await VoiceService(config).synthesize(text, "neutral")
        rows.append({"kind": "voicestudio-cached", "index": index, "cache_hit": value.get("cache_hit", False),
                     "metrics": value["metrics"], "audio": audio_info(value["wav_path"]), "manifest": value["manifest_path"]})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    multi = config.model_copy(deep=True)
    multi.character = "atri"
    multi.reuse_audio = False
    value = await VoiceService(multi).synthesize("一緒なら[emotion:sad]少し寂しくても[emotion:firm]大丈夫[emotion:]だよ。", "neutral")
    assert [c["emotion"] for c in value["chunks"]] == ["neutral", "sad", "firm", "neutral"], value["chunks"]
    assert all(c["pause_before_ms"] == 0 for c in value["chunks"])
    save("multi-emotion.json", {"ok": True, "manifest": value["manifest_path"], "metrics": value["metrics"],
                                "chunks": value["chunks"], "audio": audio_info(value["wav_path"])})
    shutil.copyfile(value["wav_path"], REPORT / "multi-emotion-demo.wav")
    service = VoiceService(config.model_copy(update={"reuse_audio": False}))
    received = asyncio.Event()
    def progress(event):
        if event.get("type") == "audio":
            received.set()
    task = asyncio.create_task(service.synthesize("いっしょにほんをよみましょう。" * 8, "neutral", progress=progress))
    await asyncio.wait_for(received.wait(), 90)
    started = time.perf_counter()
    task.cancel()
    try:
        await task
        raise AssertionError("cancel was ignored")
    except asyncio.CancelledError:
        pass
    cancel_ms = round((time.perf_counter() - started) * 1000, 2)
    resumed = await service.synthesize("こんにちは。", "neutral")
    assert resumed["state"] == "ready"
    shutil.copyfile(resumed["wav_path"], REPORT / "speech-demo.wav")
    result = {"ok": True, "rows": rows, "cancel_ms": cancel_ms, "recovery_metrics": resumed["metrics"],
              "recovery_manifest": resumed["manifest_path"], "no_qq_send": True,
              "comparison": "Same warm native server and character. Previous runtime uses 32 diffusion steps, new default 16. "
                            "Previous wall includes frozen CLI startup; new metrics cover synthesis. No quality equivalence claimed."}
    save("benchmark.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


async def singing():
    from config import AppConfig
    from singing_service import SingingService
    config = AppConfig.load(str(REPORT / "voice-config.yaml")).voice
    config.singing.cache_dir = str(REPORT / "singing-jobs")
    prior = ROOT / "maintenance_reports/singing-20261114"
    service = SingingService(config)
    rows = []
    for mode in ("cover", "cover", "compose"):
        started = time.perf_counter()
        request = {"mode": mode, "song": str(prior / ("upstream-vocal-excerpt.wav" if mode == "cover" else "original-accompaniment.wav")),
                   "lyrics": "技術テスト" if mode == "cover" else (prior / "original-lyrics.txt").read_text(encoding="utf-8"),
                   "song_output": "both", "emotion": "neutral", "language": "ja"}
        value = await service.render(request, allow_disabled=True,
            progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
        manifest = json.loads(Path(value["manifest_path"]).read_text(encoding="utf-8"))
        assert manifest["denoise"]["accompaniment_processed"] is False
        row = {"mode": mode, "seconds": round(time.perf_counter()-started, 2), "result": value,
               "audio": audio_info(value["wav_path"]), "denoise": manifest["denoise"], "separation": manifest.get("separation")}
        rows.append(row)
        save(f"singing-{mode}-{len(rows)}.json", row)
        shutil.copyfile(value["mp3_path"], REPORT / f"{mode}-demo.mp3")
    assert rows[0]["separation"]["cache_hit"] is False and rows[1]["separation"]["cache_hit"] is True
    save("singing-verification.json", {"ok": True, "rows": rows, "no_qq_send": True,
        "limits": "6-second cover fixture and 12-second original composition; no full-song or lyric accuracy claim."})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("benchmark", "singing"))
    args = parser.parse_args()
    REPORT.mkdir(parents=True, exist_ok=True)
    asyncio.run(benchmark() if args.mode == "benchmark" else singing())


if __name__ == "__main__":
    main()
