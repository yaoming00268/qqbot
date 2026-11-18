"""Package a verified frozen runtime, static templates and portable launchers."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile


ROOT = Path(__file__).resolve().parents[1]
EXECUTABLES = ("QQBot.exe", "QQBotStudio.exe", "QQBotCLI.exe", "QQBotMCP.exe")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_relative(name: str) -> str:
    if (not name or "\\" in name or ":" in name or name.startswith("/")
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise ValueError(f"Unsafe package path: {name!r}")
    return PurePosixPath(name).as_posix()


def runtime_files(runtime: Path) -> list[tuple[str, Path]]:
    manifest_path = runtime / "build-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("home_data_bundled") is not False:
        raise ValueError("Runtime must declare that it contains no user data")
    rows, seen = [], set()
    for row in manifest["files"]:
        name = safe_relative(row["path"])
        if name.casefold() in seen:
            raise ValueError(f"Duplicate runtime path: {name}")
        seen.add(name.casefold())
        if name not in (*EXECUTABLES, "config.yaml.example", "README.md") and not name.startswith("_internal/"):
            raise ValueError(f"Not a runtime resource: {name}")
        path = runtime / name
        if any(parent.is_symlink() or parent.is_junction() for parent in (path, *path.parents) if parent != parent.parent):
            raise ValueError(f"Linked runtime resource: {name}")
        if not path.is_file() or path.stat().st_size != row["bytes"] or digest(path) != row["sha256"]:
            raise ValueError(f"Runtime integrity failure: {name}")
        rows.append((name, path))
    required = {*EXECUTABLES, "config.yaml.example", "README.md"}
    if not required.issubset({name for name, _ in rows}):
        raise ValueError("Runtime is missing a required executable or template")
    expected = {name for name, _ in rows} | {"build-manifest.json"}
    actual = {path.relative_to(runtime).as_posix() for path in runtime.rglob("*") if path.is_file()}
    if actual != expected:
        raise ValueError(f"Unlisted or missing runtime files: {sorted(actual ^ expected)}")
    return [*rows, ("build-manifest.json", manifest_path)]


def portable_files(template: bytes, readme: bytes) -> dict[str, bytes]:
    text = template.decode("utf-8")
    # Preserve the reviewed template and its comments; only the portable default
    # uses agent mode. The original QQ template keeps its build manifest hash.
    text, count = re.subn(r'(?m)^(  mode:) "official"', r'\1 "agent"', text)
    if count != 1:
        raise ValueError("Expected exactly one bot.mode: official in the static template")
    files = {"README-START.md": readme, "config.agent.yaml.example": text.encode("utf-8")}
    prepare = '''@echo off
if not defined QQBOT_HOME set "QQBOT_HOME=%~dp0data"
if not exist "%QQBOT_HOME%\\" mkdir "%QQBOT_HOME%" >nul 2>nul
if not exist "%QQBOT_HOME%\\" (
  >&2 echo Cannot create QQBOT_HOME. Choose a writable data directory.
  exit /b 1
)
if not exist "%QQBOT_HOME%\\config.yaml" (
  copy /y "%~dp0config.agent.yaml.example" "%QQBOT_HOME%\\config.yaml" >nul
  if errorlevel 1 exit /b 1
)
exit /b 0
'''
    files["prepare_home.cmd"] = prepare.replace("\n", "\r\n").encode("ascii")
    commands = {"run.bat": '"%~dp0QQBot.exe"', "run_gui.bat": '"%~dp0QQBotStudio.exe"',
                "cli.bat": '"%~dp0QQBotCLI.exe"', "mcp.bat": '"%~dp0QQBotMCP.exe"',
                "actor.bat": '"%~dp0QQBot.exe"', "voice.bat": '"%~dp0QQBot.exe"'}
    for name, command in commands.items():
        suffix = " --module actor" if name == "actor.bat" else " --module voice" if name == "voice.bat" else ""
        script = ('@echo off\nsetlocal DisableDelayedExpansion\ncall "%~dp0prepare_home.cmd"\n'
                  'if errorlevel 1 exit /b %errorlevel%\n'
                  + command + ' --home "%QQBOT_HOME%"' + suffix + ' %*\nexit /b %errorlevel%\n')
        files[name] = script.replace("\n", "\r\n").encode("ascii")
    return files


def verify_package(archive: Path) -> dict:
    """Read every archived byte and verify an exact, single-root manifest."""
    with zipfile.ZipFile(archive) as saved:
        names = saved.namelist()
        if not names or len({name.casefold() for name in names}) != len(names):
            raise ValueError("Empty package or duplicate archive paths")
        for name in names:
            safe_relative(name)
        roots = {name.split("/", 1)[0] for name in names}
        if len(roots) != 1 or any("/" not in name for name in names):
            raise ValueError("Package must contain one top-level directory")
        root = roots.pop()
        manifest = json.loads(saved.read(root + "/package-manifest.json"))
        expected = {root + "/package-manifest.json"}
        for row in manifest["files"]:
            name = root + "/" + safe_relative(row["path"])
            if name in expected:
                raise ValueError(f"Duplicate manifest path: {name}")
            expected.add(name)
            info = saved.getinfo(name)
            with saved.open(info) as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if info.file_size != row["bytes"] or actual != row["sha256"]:
                raise ValueError(f"Archive integrity failure: {name}")
        if expected != set(names):
            raise ValueError("Archive contains unlisted files or is incomplete")
        return manifest


def package(runtime: Path, output: Path, name: str, readme: bytes) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", name):
        raise ValueError("Package name must be a simple directory name")
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite: {output}")
    runtime = runtime.resolve()
    files = runtime_files(runtime)
    additions = portable_files((runtime / "config.yaml.example").read_bytes(), readme)
    rows = [{"path": relative, "bytes": path.stat().st_size, "sha256": digest(path)} for relative, path in files]
    rows.extend({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for relative, data in additions.items())
    manifest = {"format": 1, "name": name, "platform": "windows-x64", "home_data_bundled": False,
                "runtime_manifest_sha256": digest(runtime / "build-manifest.json"),
                "files": sorted(rows, key=lambda row: row["path"])}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as saved:
        for relative, path in files:
            saved.write(path, name + "/" + relative)
        for relative, data in additions.items():
            saved.writestr(name + "/" + relative, data)
        saved.writestr(name + "/package-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    verify_package(output)
    result = {"ok": True, "archive": str(output), "sha256": digest(output),
              "archive_bytes": output.stat().st_size, "files": len(rows) + 1,
              "uncompressed_bytes": sum(row["bytes"] for row in rows),
              "home_data_bundled": False, "runtime": str(runtime)}
    checksum = output.with_suffix(output.suffix + ".sha256")
    with checksum.open("x", encoding="ascii", newline="\n") as stream:
        stream.write(result["sha256"] + "  " + output.name + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", default="QQBot-agent-win-x64")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = package(args.runtime, args.output, args.name, (ROOT / "build_support/PORTABLE_README.md").read_bytes())
    args.report.mkdir(parents=True, exist_ok=True)
    (args.report / "package-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
