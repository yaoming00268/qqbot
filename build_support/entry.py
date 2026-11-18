"""Shared frozen entry: each executable imports only its requested role."""
from __future__ import annotations

import argparse
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time


def main():
    multiprocessing.freeze_support()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    roles = {"qqbot": "core", "qqbotstudio": "gui", "qqbotcli": "cli", "qqbotmcp": "mcp"}
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--module", choices=("core", "gui", "cli", "mcp", "actor", "media", "voice", "bilibili-browser", "osu-spectator", "osu-telemetry-worker", "osu-telemetry-mcp", "osu-resident", "osu-resident-audio", "live-audio-sink", "osu-live"),
                        default=roles.get(Path(sys.executable).stem.casefold(), "core"))
    parser.add_argument("--home", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--check-gui", action="store_true")
    parser.add_argument("--check-dependencies", action="store_true")
    # Parse only the launcher prefix. A CLI text argument such as '--check'
    # belongs to the CLI and must never activate a launcher action.
    prefix, rest = [], list(sys.argv[1:])
    while rest:
        option = rest[0].split("=", 1)[0]
        if option not in ("--module", "--home", "--check", "--check-gui", "--check-dependencies"):
            break
        value = rest.pop(0)
        prefix.append(value)
        if option in ("--module", "--home") and "=" not in value and rest:
            prefix.append(rest.pop(0))
    args = parser.parse_args(prefix)
    from runtime_paths import configure_runtime, module_command, resource_path
    home = configure_runtime(args.home)
    sys.argv = [sys.argv[0], *rest]
    modules = {"core": "main", "gui": "gui", "cli": "console_tools.cli", "mcp": "console_tools.server", "actor": "agent_core.cli", "media": "media_worker", "voice": "voice_cli", "bilibili-browser": "bilibili_browser", "osu-spectator": "osu_spectator", "osu-telemetry-worker": "osu_telemetry_worker", "osu-telemetry-mcp": "osu_telemetry_mcp", "osu-resident": "osu_resident", "osu-resident-audio": "osu_resident_audio", "live-audio-sink": "live_audio_sink", "osu-live": "osu_live"}
    if args.check or args.check_gui or args.check_dependencies:
        if args.check_gui:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        started = time.perf_counter()
        module = importlib.import_module(modules[args.module])
        dependencies = []
        if args.check_dependencies:
            for name in ("onebot_client", "bot_client", "gemini_webapi", "cryptography",
                         "uiautomation", "rapidocr_onnxruntime", "pycdlib", "cv2", "onnxruntime", "playwright.sync_api", "agent_core.runtime",
                         "lmstudio", "emotion_models", "emotion_training", "pykakasi", "pysilk",
                         "deepseek_harness", "deepseek_harness_runtime", "bilibili_api",
                         "broadcast_host", "broadcast_speech", "broadcast_tts", "vtuber_service"):
                importlib.import_module(name)
                dependencies.append(name)
            import ssl
            ssl.create_default_context()
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            cipher = AESGCM(bytes(32))
            cipher.decrypt(bytes(12), cipher.encrypt(bytes(12), b"runtime-check", None), None)
            from rapidocr_onnxruntime import RapidOCR
            import numpy
            RapidOCR()(numpy.zeros((32, 96, 3), dtype=numpy.uint8))
            # Exercise dictionaries and the native codec, not just imports.
            import io
            from voice_text import japanese_tokens
            from pysilk import encode, decode
            assert "".join(japanese_tokens("一緒に本を読む。")) == "いっしょにほんをよむ。"
            encoded, decoded = io.BytesIO(), io.BytesIO()
            encode(io.BytesIO(bytes(24000)), encoded, sample_rate=24000, bit_rate=24000, tencent=True)
            assert encoded.getvalue().startswith(b"\x02#!SILK_V3")
            decode(io.BytesIO(encoded.getvalue()), decoded, sample_rate=24000)
            assert len(decoded.getvalue()) >= 22000
            import asyncio
            from dsh_client import DeepSeekHarnessClient
            from dsh_config import DSHConfig
            checker = DeepSeekHarnessClient(DSHConfig(home=str(home / "data/dsh-check"), workspace=str(home)),
                                           sessions_file=str(home / "data/dsh-check/map.json"))
            ok, message = asyncio.run(checker.probe())
            if not ok:
                raise RuntimeError(message)
        if args.check_gui:
            from ui.webview_app import launch
            launch(home, smoke=True, report=home / "studio-check.json")
        result = {"ok": True, "module": args.module, "home": str(home),
                  "frozen": bool(getattr(sys, "frozen", False)),
                  "import_seconds": time.perf_counter() - started,
                  "commands": {name: module_command(name) for name in modules},
                  "resources": {name: resource_path(name).exists() for name in (
                      "console_tools/README.md", "browser_extensions/gemini_connector/manifest.json",
                      "renpy_bridge_agent.py", "vm_guest/agent.py", "vm_guest/Setup.ps1", "ui/web/index.html",
                      "ui/web/studio.js", "ui/web/studio.css", "ui/web/broadcast.js", "ui/web/vtuber_overlay.html",
                      "ui/web/vtuber_assets/manifest.json")},
                  "dependencies": dependencies}
        if not all(result["resources"].values()):
            raise RuntimeError("Runtime resources are incomplete: " + json.dumps(result))
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    module = importlib.import_module(modules[args.module])
    return module.main()


if __name__ == "__main__":
    raise SystemExit(main())
