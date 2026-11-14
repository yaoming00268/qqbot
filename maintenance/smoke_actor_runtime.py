"""Exercise source or frozen actor CLI against a disposable localhost website."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from config import AppConfig
    from agent_core.configuration import AgentConfig
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = args.report.resolve()
    report.mkdir(parents=True, exist_ok=True)
    received, cookies = [], []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            cookies.append(self.headers.get("Cookie", ""))
            html = ("<!doctype html><meta charset='utf-8'><title>Runtime fixture</title>"
                "<h1>现场录音的本地文章</h1><p>这是实际可读的测试内容。</p>"
                "<span data-testid='identity'>local-fixture</span>"
                "<label for='editor'>正文</label><textarea id='editor'></textarea><button>发布</button>"
                "<output role='status' hidden></output><a id='receipt' hidden>发布结果</a>"
                "<script>document.querySelector('button').onclick=async()=>{"
                "const r=await fetch('/publish',{method:'POST',body:document.querySelector('textarea').value});"
                "const data=await r.json();const s=document.querySelector('output');s.hidden=false;s.textContent='已发布';"
                "const a=document.querySelector('a');a.hidden=false;a.href='/post/'+data.id;};</script>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Set-Cookie", "fixture_session=persistent; Path=/; Max-Age=600")
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self):
            received.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
            data = json.dumps({"id": len(received)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *values):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="qqbot_native_actor_", ignore_cleanup_errors=True) as tmp:
            home = Path(tmp)
            url = "http://127.0.0.1:" + str(server.server_port)
            config = AppConfig(agent=AgentConfig.model_validate({"autonomous_model_enabled": False,
                "database_path": str(home / "actors.sqlite3"), "actors": {"fixture": {"name": "本地验证角色"}},
                "accounts": {"site": {"actor_id": "fixture", "home_url": url + "/",
                    "actions": ["read_content", "read_feed", "create_draft", "publish_post"],
                    "identity": {"test_id": "identity"}, "expected_identity": "local-fixture",
                    "publish": {"editor": {"label": "正文"}, "submit": {"role": "button", "name": "发布"},
                        "confirmation": {"role": "status"}, "result_link": {"css": "#receipt"},
                        "composer_url": url + "/compose"}}}}))
            config.bot.mode = "agent"
            config.save(home / "config.yaml")
            base = ([str(args.runtime.resolve()), "--home", str(home), "--module", "actor"] if args.runtime
                    else [sys.executable, "-m", "agent_core"])
            def invoke(*parts):
                process = subprocess.run([*base, "--config", str(home / "config.yaml"), *parts], cwd=ROOT,
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if process.returncode:
                    raise RuntimeError(process.stdout + "\n" + process.stderr)
                value = json.loads(process.stdout)["result"]
                if isinstance(value, dict) and "state" in value and value["state"] != "confirmed":
                    raise RuntimeError(json.dumps(value, ensure_ascii=False))
                return value
            read = invoke("read", "--account", "site", "--url", url + "/", "--request-key", "read")
            assert "本地文章" in read["result"]["text"]
            draft = invoke("draft", "--account", "site", "--text", "基于实际阅读准备的本地内容", "--request-key", "draft")
            draft_id = draft["result"]["draft"]["id"]
            publish = invoke("publish", "--account", "site", "--draft", draft_id, "--request-key", "publish")
            repeated = invoke("publish", "--account", "site", "--draft", draft_id, "--request-key", "publish")
            assert repeated["id"] == publish["id"] and len(received) == 1
            assert any("fixture_session=persistent" in cookie for cookie in cookies)
            service_command = ([str(args.runtime.resolve()), "--home", str(home)] if args.runtime else
                               [sys.executable, str(ROOT / "main.py")])
            with (report / "service.log").open("w", encoding="utf-8") as output:
                service = subprocess.Popen(service_command, cwd=home, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    deadline = time.monotonic() + 30
                    scheduled = False
                    while time.monotonic() < deadline and service.poll() is None:
                        with sqlite3.connect(home / "actors.sqlite3", timeout=1) as db:
                            scheduled = db.execute("SELECT 1 FROM actor_actions WHERE kind='read_feed' AND state='confirmed'").fetchone()
                        if scheduled:
                            break
                        time.sleep(.2)
                    assert scheduled, "Standalone agent service did not confirm a scheduled reading"
                    config.agent.accounts["site"].enabled = False
                    config.save(home / "config.yaml")
                    deadline = time.monotonic() + 5
                    revoked = False
                    while time.monotonic() < deadline and service.poll() is None:
                        with sqlite3.connect(home / "actors.sqlite3", timeout=1) as db:
                            revoked = db.execute("SELECT enabled FROM actor_accounts WHERE id='web:site'").fetchone()[0] == 0
                        if revoked:
                            break
                        time.sleep(.2)
                    assert revoked, "Standalone service did not apply account revocation"
                finally:
                    service.terminate()
                    service.wait(timeout=10)
            evidence = []
            for name, receipt in (("read", read), ("publish", publish)):
                for path in receipt["result"]["evidence"]:
                    target = report / (name + "-" + Path(path).name)
                    shutil.copy2(path, target)
                    evidence.append(str(target))
            result = {"ok": True, "frozen": bool(args.runtime), "read": "confirmed", "publish": "confirmed",
                "server_posts": len(received), "duplicate_replayed": False, "persistent_cookie": True,
                "browser": "native_playwright_persistent_context", "evidence": evidence,
                "standalone_without_qq": True, "scheduled_reading": "confirmed", "hot_reload_revocation": True,
                "scope": "localhost fixture only; no real platform accounts"}
            (report / "smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


if __name__ == "__main__":
    main()
