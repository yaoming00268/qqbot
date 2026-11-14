"""Exercise structured-memory and reading APIs from the actual frozen suite."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def verify(runtime, report):
    from bot_identity import IdentityStore
    from console_tools.broker import ConsoleBroker
    runtime, report = runtime.resolve(), report.resolve()
    report.mkdir(parents=True, exist_ok=True)
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("ANTIGRAVITY_", "QQBOT_")) and key != "PYTHONPATH"}
    environment["PATH"] = os.pathsep.join(str(Path(environment.get("SystemRoot", r"C:\Windows")) / part) for part in ("System32", ""))
    checks = []
    with tempfile.TemporaryDirectory(prefix="qqbot-结构记忆 & ") as folder:
        home = Path(folder)
        docs = home / "资料"
        docs.mkdir()
        (docs / "music.md").write_text("拨奏和拉奏可以产生不同音色。", encoding="utf-8")
        store = IdentityStore(home / "identity.sqlite3")
        broker = ConsoleBroker(home / "console.sqlite3")
        archive = store.enqueue_archive("persona", "group", "fixture", {
            "summary": "阿林研究配乐", "session_start": "2026-11-10 12:00:00", "session_end": "2026-11-10 13:00:00",
            "participants": {"123": {"nickname": "阿林", "role": "user"}},
            "events": [{"id": "music", "title": "研究配乐", "people": ["123"], "tags": ["音乐"]}],
            "people": [{"user_id": "123", "facts": ["学习配乐"], "tags": ["音乐"]}]})
        context = broker.issue_context({"namespace": "fixture", "scope": "group", "session_key": "fixture",
            "main_agent": True, "identity_owner": "persona", "identity_base_prompt": "测试身份",
            "identity_database": str(store.path), "exploration_config": {"roots": {"documents": str(docs)}},
            "output_dir": str(home / "output")}, home / "contexts")

        def cli(action, *args):
            command = [str(runtime / "QQBotCLI.exe"), "--home", str(home), action, *args, "--context-file", context]
            process = subprocess.run(command, cwd=home, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            checks.append({"action": action, "arguments": list(args), "exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr})
            assert process.returncode == 0, checks[-1]
            value = json.loads(process.stdout)
            assert value.get("ok"), value
            return value

        assert cli("explore", "read", "--root-id", "documents", "--path", "music.md")["text"].startswith("拨奏")
        assert cli("events", "list", "--tag", "音乐", "--user-id", "123", "--since", "2026-11-10", "--until", "2026-11-10")["events"][0]["id"] == "music"
        assert cli("contacts", "show", "--user-id", "123")["facts"] == ["学习配乐"]
        cli("events", "upsert", "--event-id", "music", "--revision", "1", "--reason", "实际阅读补全", "--event-status", "resolved", "--text", "已比较拨奏与拉奏")
        assert cli("events", "history", "--event-id", "music")["history"][0]["revision"] == 2
        assert cli("memory", "pending", "--limit", "1")["archives"][0]["archive_id"] == archive
        cli("preset", "update", "--revision", "0", "--reason", "阅读复盘", "--text", "先阅读资料再表达观点")
        cli("memory", "review", "--archive-id", archive, "--decision", "update", "--revision", "1", "--reason", "已落实来源核对习惯")
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "qqbot_cli", "arguments": {
                "action": "explore", "operation": "read", "context_file": context, "params": {"root_id": "documents", "path": "music.md"}}}}]
        process = subprocess.run([str(runtime / "QQBotMCP.exe"), "--home", str(home)], cwd=home, env=environment,
            input="\n".join(json.dumps(value, ensure_ascii=False) for value in requests) + "\n", capture_output=True,
            text=True, encoding="utf-8", timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert process.returncode == 0, process.stderr
        responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        assert len(responses) == 2 and not responses[1]["result"].get("isError"), responses
        assert all(name in json.dumps(responses[0]) for name in ("explore", "events", "memory"))
        assert "拨奏" in responses[1]["result"]["content"][0]["text"]
        cli("finish")
        assert store.reviewed("persona", "group") == {archive}
        assert IdentityStore(store.path).event("persona", "group", "music")["status"] == "resolved"
    (report / "checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {"ok": True, "frozen_cli_checks": len(checks), "mcp_schema_and_read": True,
        "events_contacts_growth_review": True, "unicode_paths": True, "restart_persistence": True,
        "live_accounts_used": False, "runtime": str(runtime)}
    (report / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.runtime, args.report), ensure_ascii=False))
