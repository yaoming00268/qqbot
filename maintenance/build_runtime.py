"""Build the multi-executable QQBot suite; never bundle account data."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def build():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "runtime")
    parser.add_argument("--work", type=Path, default=ROOT / "build_artifacts/frozen")
    parser.add_argument("--report", type=Path, default=ROOT / "maintenance_reports/cleanup_20261108")
    args = parser.parse_args()
    destination = args.output.resolve() / "QQBot"
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite an existing runtime: {destination}")
    report = args.report.resolve()
    report.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--log-level", "INFO",
               "--distpath", str(args.output.resolve()), "--workpath", str(args.work.resolve()),
               str(ROOT / "build_support/qqbot.spec")]
    # Resolve native libraries from the selected Python environment, without
    # accidentally picking a DLL from unrelated desktop tools on PATH.
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    prefix = Path(sys.prefix)
    environment = {**os.environ, "PATH": os.pathsep.join(str(path) for path in (
        prefix, prefix / "Library/bin", prefix / "DLLs", prefix / "Scripts", system / "System32", system))}
    environment.pop("PYTHONPATH", None)
    with (report / "build.log").open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=ROOT, env=environment, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise SystemExit(f"Build failed ({result.returncode}); see {report / 'build.log'}")
    shutil.copy2(ROOT / "config.yaml.example", destination / "config.yaml.example")
    shutil.copy2(ROOT / "build_support/README.md", destination / "README.md")
    # Only the static glossary template belongs in a clean distribution. The
    # existing user-edited glossary, personas, databases and cookies stay home.
    rows = []
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            rows.append({"path": path.relative_to(destination).as_posix(),
                         "bytes": path.stat().st_size, "sha256": digest})
    manifest = {"python": sys.version, "pyinstaller": importlib.metadata.version("pyinstaller"),
                "build_seconds": time.perf_counter() - started, "files": rows,
                "bytes": sum(row["bytes"] for row in rows), "home_data_bundled": False,
                "executables": ["QQBot.exe", "QQBotStudio.exe", "QQBotCLI.exe", "QQBotMCP.exe"]}
    (destination / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (report / "build-summary.json").write_text(json.dumps({key: value for key, value in manifest.items() if key != "files"},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "destination": str(destination), "files": len(rows), "bytes": manifest["bytes"],
                      "seconds": manifest["build_seconds"]}, ensure_ascii=False))


if __name__ == "__main__":
    build()
