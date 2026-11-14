"""Extract and exercise the delivered Windows ZIP without using live bot data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from maintenance.package_runtime import digest, verify_package


def verify(archive: Path, report: Path, *, reply_flow=False) -> dict:
    archive, report = archive.resolve(), report.resolve()
    report.mkdir(parents=True, exist_ok=True)
    manifest = verify_package(archive)
    environment = dict(os.environ)
    # A configured developer machine must not supply a data home, Python import
    # path or model credentials to the unpacked launchers.
    for key in list(environment):
        if key.startswith(("QQBOT_", "ANTIGRAVITY_")) or key.endswith(("_API_KEY", "_ACCESS_TOKEN")):
            environment.pop(key, None)
    environment.pop("PYTHONPATH", None)
    environment.update(QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    environment["PATH"] = os.pathsep.join(str(Path(environment.get("SystemRoot", r"C:\Windows")) / part)
                                           for part in ("System32", ""))
    checks = []
    reply_result = None

    def invoke(label, argv, *, cwd, stdin=None, env=None, protocol=False, timeout=90):
        started = time.perf_counter()
        process = subprocess.run(argv, cwd=cwd, env=env or environment, input=stdin,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        record = {"name": label, "argv": argv, "exit_code": process.returncode,
                  "seconds": round(time.perf_counter() - started, 3),
                  "stdout": process.stdout, "stderr": process.stderr}
        checks.append(record)
        (report / "checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
        if process.returncode:
            raise RuntimeError(f"{label} exited {process.returncode}: {process.stdout}\n{process.stderr}")
        lines = process.stdout.strip().splitlines()
        if protocol:
            return [json.loads(line) for line in lines]
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError:
            pass
        for line in reversed(lines):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    with tempfile.TemporaryDirectory(prefix="qqbot_portable_", ignore_cleanup_errors=True) as folder:
        # tempfile may use an 8.3 user-directory alias; the runtime canonicalizes
        # --home. Resolve it here before comparing returned commands and paths.
        base = Path(folder).resolve() / "portable 便携 & test"
        with zipfile.ZipFile(archive) as saved:
            saved.extractall(base)
        runtime = base / manifest["name"]
        if not runtime.is_dir():
            raise ValueError("Manifest name does not match the archive root")

        def assert_files():
            for row in manifest["files"]:
                path = runtime / row["path"]
                if not path.is_file() or path.stat().st_size != row["bytes"] or digest(path) != row["sha256"]:
                    raise ValueError("Extracted file changed: " + row["path"])
        assert_files()
        home = base / "isolated 数据"
        home.mkdir()
        core = str(runtime / "QQBot.exe")
        for label, parts in (("core_import", ["--check"]),
                             ("actor_import", ["--module", "actor", "--check"]),
                             ("gui_offscreen", ["--module", "gui", "--check-gui"]),
                             ("dependencies_ocr_crypto_playwright", ["--check-dependencies"])):
            value = invoke(label, [core, "--home", str(home), *parts], cwd=base)
            assert value["ok"] and value["frozen"]
            assert Path(value["home"]) == home
            assert all(value["resources"].values())
            assert Path(value["commands"]["cli"][0]) == runtime / "QQBotCLI.exe"
            if label == "gui_offscreen":
                studio = json.loads((home / "studio-check.json").read_text(encoding="utf-8"))
                assert studio["ok"] and studio["engine"] == "pywebview" and studio["bridge"]
                assert studio["emoji_free"] and not studio["overflow"]
                assert studio["opacity_check"]["ok"]
                (report / "frozen-studio.json").write_text(json.dumps(studio, ensure_ascii=False, indent=2), encoding="utf-8")

        # Exercise CMD wrappers from an unrelated cwd, with PATH limited to
        # Windows system tools. No developer Python environment can be used.
        cmd = str(Path(environment.get("SystemRoot", r"C:\Windows")) / "System32/cmd.exe")

        def batch(label, name, arguments="", **kwargs):
            command = '""' + str(runtime / name) + '"' + (" " + arguments if arguments else "") + '"'
            # list2cmdline escapes quotes for the C argv parser, whereas CMD
            # parses /c as command text. Pass the quoted command line verbatim.
            return invoke(label, '"' + cmd + '" /d /s /c ' + command, cwd=base, **kwargs)

        status = batch("actor_launcher_first_run", "actor.bat", "status")
        assert status["ok"]
        default_config = runtime / "data/config.yaml"
        assert default_config.exists()
        import yaml
        config = yaml.safe_load(default_config.read_text(encoding="utf-8"))
        assert config["bot"]["mode"] == "agent" and config["agent"]["actors"] == {}
        assert config["agent"]["accounts"] == {}
        original = default_config.read_bytes() + b"\n# preserve-existing-config\n"
        default_config.write_bytes(original)
        batch("existing_config_preserved", "actor.bat", "status")
        assert default_config.read_bytes() == original

        cli = batch("cli_actor_help", "cli.bat", "help actor publish")
        assert cli
        assert "publish" in json.dumps(cli, ensure_ascii=False)
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        mcp = batch("mcp_clean_stdio_and_actor_desktop_tools", "mcp.bat", stdin=request, protocol=True)
        assert len(mcp) == 1 and mcp[0]["id"] == 1
        tools = {tool["name"]: tool for tool in mcp[0]["result"]["tools"]}
        assert all("qqbot_desktop_" + name in tools for name in ("inspect", "invoke", "fill", "select"))
        assert "actor" in tools["qqbot_cli"]["inputSchema"]["properties"]["action"]["enum"]
        batch("core_launcher", "run.bat", "--check")
        batch("gui_launcher_offscreen", "run_gui.bat", "--check-gui")

        external = base / "external 数据 & space"
        external_env = {**environment, "QQBOT_HOME": str(external)}
        batch("external_home_first_run", "actor.bat", "status", env=external_env)
        external_config = external / "config.yaml"
        assert external_config.is_file()
        marked = external_config.read_bytes() + b"\n# keep-external-config\n"
        external_config.write_bytes(marked)
        batch("external_config_preserved", "actor.bat", "status", env=external_env)
        assert external_config.read_bytes() == marked
        invalid_home = base / "not-a-directory"
        invalid_home.write_text("fixture", encoding="ascii")
        command = '""' + str(runtime / "actor.bat") + '" status"'
        rejected = subprocess.run('"' + cmd + '" /d /s /c ' + command, cwd=base,
            env={**environment, "QQBOT_HOME": str(invalid_home)}, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        assert rejected.returncode != 0 and not rejected.stdout.strip()

        if reply_flow:
            from console_tools.broker import ConsoleBroker
            broker = ConsoleBroker(home / "fixture-console.sqlite3")
            payload = {"namespace": "fixture", "scope": "group", "session_key": "fixture-persona",
                       "generation": 0, "sender_id": "fixture-user", "is_group": True,
                       "main_agent": True, "speech_check_required": True,
                       "source_message_id": "fragment", "source_text": "因为"}
            context = broker.issue_context(payload, home / "contexts")
            cli_exe = str(runtime / "QQBotCLI.exe")
            def cli_call(label, *parts):
                return invoke(label, [cli_exe, "--home", str(home), *parts,
                                     "--context-file", context], cwd=base)
            held = cli_call("frozen_speech_hold_without_finish", "speech", "hold")
            assert held["state"] == "waiting"
            first_token = ConsoleBroker.from_context(context)[1]
            assert broker.reply_control(first_token)["mode"] == "wait"
            assert broker.has_pending_speech("fixture", "group", "fixture-persona", 0)
            context = broker.issue_context({**payload, "source_message_id": "complete", "source_text": "完整问题"}, home / "contexts")
            token = ConsoleBroker.from_context(context)[1]
            assert [row["text"] for row in broker.context(token)["speech"]["cached_messages"]] == ["因为"]
            fast_reply = cli_call("frozen_reply_and_finish_without_ready", "reply", "--text", "直接回答", "--finish")
            assert fast_reply["state"] == "finished" and fast_reply["next_action"] == "none"
            assert broker.reply_control(token)["mode"] == "finished"
            assert cli_call("frozen_reply_finish_idempotent", "reply", "--text", "直接回答", "--finish") == fast_reply
            request = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "qqbot_cli", "arguments": {"action": "reply", "context_file": context,
                "params": {"text": ["直接回答"], "finish": True}}}}, ensure_ascii=False) + "\n"
            response = batch("frozen_mcp_reply_finish", "mcp.bat", stdin=request, protocol=True)
            assert len(response) == 1 and not response[0]["result"]["isError"]
            assert json.loads(response[0]["result"]["content"][0]["text"]) == fast_reply
            assert len(broker.staged_messages(token)) == 1
            broker.seal_messages(token)
            assert not broker.has_pending_speech("fixture", "group", "fixture-persona", 0)
            context = broker.issue_context({**payload, "source_message_id": "multi"}, home / "contexts")
            token = ConsoleBroker.from_context(context)[1]
            cli_call("frozen_message_without_ready", "message", "--text", "复杂回复")
            committed = cli_call("frozen_commit_and_finish", "commit", "--finish")
            assert committed["state"] == "finished" and len(broker.staged_messages(token)) == 1
            reply_result = {"ok": True, "hold_finishes_automatically": True,
                            "reply_without_ready": True, "reply_finish_single_call": True,
                            "commit_finish_single_call": True, "mcp_supported": True,
                            "idempotent": True, "cached_speech_consumed": True}

        smoke = invoke("native_browser_localhost_lifecycle", [sys.executable,
            str(ROOT / "maintenance/smoke_actor_runtime.py"), "--runtime", core,
            "--report", str(report / "browser-smoke")], cwd=base, timeout=180)
        assert smoke["ok"] and smoke["server_posts"] == 1 and smoke["persistent_cookie"]
        assert_files()

    result = {"ok": True, "archive": str(archive), "archive_sha256": digest(archive),
              "archive_integrity": True, "extracted_files_integrity": True,
              "frozen_checks": 4, "subprocess_checks": len(checks),
              "all_launchers": True, "first_run_agent_mode": True,
              "existing_configs_preserved": True, "external_home_supported": True,
              "invalid_home_rejected": True, "unicode_spaces_ampersand_paths": True,
              "system_only_path": True, "mcp_clean_stdio": True,
              **({"reply_flow": reply_result} if reply_flow else {}),
              "browser": smoke, "live_accounts_used": False,
              "live_bot_restarted": False}
    (report / "verification-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--reply-flow", action="store_true", help="Also exercise the 3.1 reply/commit --finish flow")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(verify(args.archive, args.report, reply_flow=args.reply_flow), ensure_ascii=False))


if __name__ == "__main__":
    main()
