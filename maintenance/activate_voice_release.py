"""Select only the verified voice release and merge its local voice settings."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def activate():
    from config import AppConfig
    from voice_config import VoiceConfig
    from voice_service import voice_context
    report = ROOT / "maintenance_reports/voice-dsh-20261114"
    verification = json.loads((report / "verification/verification-summary.json").read_text(encoding="utf-8"))
    archive = ROOT / "dist/QQBot-voice-dsh-20261114-r2-win-x64.zip"
    with archive.open("rb") as stream:
        if not verification["ok"] or hashlib.file_digest(stream, "sha256").hexdigest() != verification["archive_sha256"]:
            raise ValueError("Release has not passed portable verification")
    relative = "releases\\voice-dsh-20261114-r2\\QQBot"
    runtime = ROOT / "runtime" / relative
    if not (runtime / "QQBot.exe").is_file():
        raise FileNotFoundError(runtime)
    path = ROOT / "config.yaml"
    original = path.read_bytes()
    text = original.decode("utf-8-sig")
    raw = yaml.safe_load(text)
    old_voice = raw.get("voice") or {}
    value = {**VoiceConfig().model_dump(), **old_voice, "enabled": True, "auto_start": True,
             "provider": "gpt_sovits_v2", "endpoint": "http://127.0.0.1:9880", "default_language": "ja", "character": "atri"}
    value["dictionary"] = {**value["dictionary"]}
    value["dictionary"].setdefault("夏生", "なつき")
    voice = VoiceConfig.model_validate(value)
    summary = voice_context(voice)
    if summary.get("error") or not summary["emotions"]:
        raise ValueError("Installed voice catalog is incomplete")
    replacement = yaml.safe_dump({"voice": value}, allow_unicode=True, sort_keys=False)
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if re.match(r"^voice\s*:", line)]
    if "voice" in raw and len(starts) != 1:
        raise ValueError("Expected a single top-level voice settings block")
    if starts:
        start = starts[0]
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^[A-Za-z_][\w-]*\s*:", lines[i])), len(lines))
        updated = "".join(lines[:start]) + replacement + "".join(lines[end:])
    else:
        updated = text.rstrip() + "\n\n" + replacement
    merged = yaml.safe_load(updated)
    if {k: v for k, v in raw.items() if k != "voice"} != {k: v for k, v in merged.items() if k != "voice"}:
        raise ValueError("Refusing to modify unrelated bot settings")
    AppConfig.model_validate(merged)
    backup = report / "before/config-before-activation.yaml"
    if backup.exists() and backup.read_bytes() != original:
        raise FileExistsError("A different activation backup already exists")
    backup.write_bytes(original)
    temp = path.with_name("config.yaml.voice-update.tmp")
    temp.write_text(updated, encoding="utf-8")
    if path.read_bytes() != original:
        temp.unlink()
        raise RuntimeError("Configuration changed during activation; merge was not applied")
    temp.replace(path)
    pointer = ROOT / "runtime/active-runtime.txt"
    previous = pointer.read_text(encoding="utf-8-sig").strip()
    temporary = pointer.with_suffix(".tmp")
    temporary.write_text(relative + "\n", encoding="utf-8")
    temporary.replace(pointer)
    result = {"ok": True, "active_runtime": relative, "previous_runtime": previous,
              "voice": {k: summary[k] for k in ("enabled", "language", "character", "emotions")},
              "auto_start": True, "model_backend": merged.get("model_backend", {}).get("backend", "antigravity"),
              "unrelated_config_preserved": True, "live_bot_restarted": False, "config_backup": str(backup)}
    (report / "activation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    import os
    os.chdir(ROOT)
    activate()
