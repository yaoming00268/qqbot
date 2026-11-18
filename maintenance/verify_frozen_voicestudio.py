"""Verify the built executable, external VoiceStudio worker and song conversion."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPORT = ROOT / "maintenance_reports/voicestudio-20261116"
RUNTIME = ROOT / "runtime/releases/voicestudio-20261116/QQBot"


def main():
    from maintenance.verify_voicestudio import audio_info
    home = REPORT / "frozen-clean-home"
    home.mkdir(exist_ok=True)
    rows = []
    def invoke(label, args, timeout=240):
        started = time.perf_counter()
        value = subprocess.run([str(RUNTIME / "QQBot.exe"), *args], cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        (REPORT / (label + ".log")).write_bytes(value.stdout)
        assert value.returncode == 0, value.stdout.decode("utf-8", errors="replace")[-2000:]
        result = json.loads(value.stdout.decode("utf-8-sig").strip().splitlines()[-1])
        assert result.get("ok"), result
        rows.append({"name": label, "seconds": round(time.perf_counter()-started, 2), "result": result})
        print(json.dumps({"name": label, "ok": True, "seconds": rows[-1]["seconds"]}), flush=True)
        return result
    invoke("frozen-dependencies", ["--home", str(home), "--check-dependencies"])
    invoke("frozen-native-ui", ["--home", str(home), "--module", "gui", "--check-gui"])
    native = json.loads((home / "studio-check.json").read_text(encoding="utf-8"))
    assert native["ok"] and native["version"] == "20261116-voicestudio"
    prefix = ["--home", str(ROOT), "--module", "voice", "--config", "maintenance_reports/voicestudio-20261116/voice-config.yaml"]
    check = invoke("frozen-voice-check", [*prefix, "check"])
    assert check["websocket"] and check["reachable"]
    speech = invoke("frozen-speech", [*prefix, "preview", "--text", "こんにちは。一緒に本を読みましょう。", "--emotion", "neutral", "--no-cache"])
    assert speech["state"] == "ready" and speech["effective_provider"] == "voicestudio"
    assert all(c["stream"]["blocks"] > 1 for c in speech["chunks"])
    audio_info(speech["wav_path"])
    song = invoke("frozen-singing", [*prefix, "sing", "cover", "--song", str(ROOT / "maintenance_reports/singing-20261114/upstream-vocal-excerpt.wav"),
        "--lyrics", "技術テスト", "--emotion", "neutral", "--output", "both"], timeout=360)
    assert song["state"] == "ready" and {f["kind"] for f in song["files"]} == {"file", "voice"}
    audio_info(song["wav_path"])
    result = {"ok": True, "runtime": str(RUNTIME), "checks": rows, "native_ui": native, "no_qq_send": True}
    (REPORT / "frozen-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "report": str(REPORT / "frozen-verification.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
