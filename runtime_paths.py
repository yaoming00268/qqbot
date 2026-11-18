"""Separate writable bot state from bundled resources and executable modules."""
from __future__ import annotations

import os
from pathlib import Path
import sys


MODULES = {"core": ("QQBot.exe", "main.py"), "gui": ("QQBotStudio.exe", "gui.py"),
           "cli": ("QQBotCLI.exe", "console_tools/cli.py"), "mcp": ("QQBotMCP.exe", "console_tools/server.py")}


def application_home() -> Path:
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("QQBOT_HOME") or Path(sys.executable).parent).expanduser().resolve()
    return Path(__file__).resolve().parent


def resource_path(relative: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / relative


def module_command(module: str) -> list[str]:
    if module in {"osu-spectator", "osu-telemetry-worker", "osu-telemetry-mcp", "osu-resident", "osu-resident-audio", "live-audio-sink", "osu-live"}:
        scripts = {"osu-spectator": "osu_spectator.py", "osu-telemetry-worker": "osu_telemetry_worker.py", "osu-telemetry-mcp": "osu_telemetry_mcp.py", "osu-resident": "osu_resident.py", "osu-resident-audio": "osu_resident_audio.py", "live-audio-sink": "live_audio_sink.py", "osu-live": "osu_live.py"}
        return [*module_command("core"), "--module", module] if getattr(sys, "frozen", False) else [sys.executable, "-X", "utf8", str(resource_path(scripts[module]))]
    if module == "bilibili-browser":
        return [*module_command("core"), "--module", module] if getattr(sys, "frozen", False) else [sys.executable, "-X", "utf8", str(resource_path("bilibili_browser.py"))]
    if module == "voice":
        return [*module_command("core"), "--module", "voice"] if getattr(sys, "frozen", False) else [sys.executable, "-X", "utf8", str(resource_path("voice_cli.py"))]
    if module == "media":
        return [*module_command("core"), "--module", "media"] if getattr(sys, "frozen", False) else [sys.executable, "-X", "utf8", str(resource_path("media_worker.py"))]
    if module == "actor":
        return [*module_command("core"), "--module", "actor"] if getattr(sys, "frozen", False) else [sys.executable, "-m", "agent_core"]
    executable, script = MODULES[module]
    if getattr(sys, "frozen", False):
        path = Path(sys.executable).resolve().with_name(executable)
        if not path.is_file():
            raise FileNotFoundError(f"Missing runtime module: {path}")
        return [str(path), "--home", str(application_home())]
    options = ["-X", "utf8", "-u"] if module == "core" else []
    return [sys.executable, *options, str(resource_path(script))]


def configure_runtime(home: str | Path | None = None) -> Path:
    target = Path(home).expanduser().resolve() if home is not None else application_home()
    if not target.is_dir():
        raise FileNotFoundError(f"Bot data directory does not exist: {target}")
    os.environ["QQBOT_HOME"] = str(target)
    os.chdir(target)
    return target
