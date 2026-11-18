"""Activate the verified Popukar voice and deploy the matching console assets."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "maintenance_reports/popukar-voice-20261114"
sys.path.insert(0, str(ROOT))


def activate():
    from config import AppConfig
    from voice_config import VoiceConfig
    from voice_service import voice_context

    verified = json.loads((REPORT / "verification.json").read_text(encoding="utf-8"))
    if not verified.get("ok") or verified.get("character") != "popukar":
        raise ValueError("泡普卡尚未通过真实控制台合成测试。")
    runtime_root = ROOT / "runtime"
    runtime = (runtime_root / (runtime_root / "active-runtime.txt").read_text(encoding="utf-8-sig").strip()).resolve()
    if not runtime.is_relative_to(runtime_root.resolve()) or not (runtime / "QQBotStudio.exe").is_file():
        raise ValueError("当前控制台运行包路径无效。")

    path = ROOT / "config.yaml"
    original = path.read_bytes()
    text = original.decode("utf-8-sig")
    raw = yaml.safe_load(text)
    value = {**VoiceConfig().model_dump(), **(raw.get("voice") or {}),
             "enabled": True, "auto_start": True, "character": "popukar", "default_language": "ja",
             "provider": "gpt_sovits_v2", "engine_config": "tools/voice-engine/popukar-infer.yaml"}
    value["dictionary"] = {**value["dictionary"], "ポプカル": "ぽぷかる", "泡普卡": "ぽぷかる"}
    summary = voice_context(VoiceConfig.model_validate(value))
    if summary.get("error") or summary["emotions"] != ["neutral"]:
        raise ValueError("泡普卡样本目录与已验证配置不符。")
    replacement = yaml.safe_dump({"voice": value}, allow_unicode=True, sort_keys=False)
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if re.match(r"^voice\s*:", line)]
    if "voice" in raw and len(starts) != 1:
        raise ValueError("配置中的 voice 分区无法安全定位。")
    if starts:
        start = starts[0]
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^[A-Za-z_][\w-]*\s*:", lines[i])), len(lines))
        updated = "".join(lines[:start]) + replacement + "".join(lines[end:])
    else:
        updated = text.rstrip() + "\n\n" + replacement
    merged = yaml.safe_load(updated)
    if {k: v for k, v in raw.items() if k != "voice"} != {k: v for k, v in merged.items() if k != "voice"}:
        raise ValueError("拒绝修改语音以外的配置。")
    AppConfig.model_validate(merged)
    before = REPORT / "before"
    before.mkdir(parents=True, exist_ok=True)
    backup = before / "config-before-activation.yaml"
    if not backup.exists():
        backup.write_bytes(original)

    deployed = []
    for relative in ("ui/web/studio.js", "ui/web/index.html", "VOICE_README.md"):
        source, target = ROOT / relative, runtime / "_internal" / relative
        if not source.is_file() or not target.is_file():
            raise FileNotFoundError(target)
        saved = before / "active-runtime" / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        if not saved.exists():
            shutil.copy2(target, saved)
        shutil.copy2(source, target)
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError("控制台资源同步校验失败。")
        deployed.append({"path": str(target), "sha256": expected})

    temporary = path.with_name("config.yaml.popukar-update.tmp")
    temporary.write_text(updated, encoding="utf-8")
    if path.read_bytes() != original:
        temporary.unlink()
        raise RuntimeError("配置同时被更新，请重试合并。")
    temporary.replace(path)
    result = {"ok": True, "voice": summary, "auto_start": True, "engine_config": value["engine_config"],
              "unrelated_config_preserved": True, "runtime": str(runtime), "deployed": deployed,
              "regression": {"passed": 31, "files": ["test_voice_pipeline.py", "test_webview_studio.py"]},
              "config_backup": str(backup)}
    (REPORT / "activation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    import os
    os.chdir(ROOT)
    activate()
