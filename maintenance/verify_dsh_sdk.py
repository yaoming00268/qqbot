"""Exercise the real pinned SDK/runtime against a loopback model fixture only."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from console_tools.broker import ConsoleBroker
from dsh_client import DeepSeekHarnessClient
from dsh_config import DSHConfig


async def verify(mcp_runtime=None):
    from deepseek_harness.api import Session
    import dsh_profile
    original_command = dsh_profile.module_command
    real_run = Session.run
    diagnostics = []
    def capture_run(self, *args, **kwargs):
        result = real_run(self, *args, **kwargs)
        diagnostics.append(asdict(result))
        return result
    Session.run = capture_run
    requests, request_ready = [], threading.Event()
    slow, finished = threading.Event(), threading.Event()
    call = {"value": 0}
    context = {"file": ""}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            request_ready.set()
            if slow.is_set():
                finished.wait(12)
            call["value"] += 1
            tool = call["value"] == 1
            message = {"role": "assistant", "content": None if tool else "DSH fixture completed", "reasoning_content": "fixture"}
            if tool:
                message["tool_calls"] = [{"id": "call-voice-check", "type": "function", "function": {
                    "name": "mcp__qqbot__qqbot_cli", "arguments": json.dumps({"action": "voice", "context_file": context["file"], "params": {}})}}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
            self.end_headers()
            try:
                if "system" in body:
                    block = {"type": "tool_use", "id": "call-voice-check", "name": "mcp__qqbot__qqbot_cli", "input": {}} if tool else {"type": "text", "text": ""}
                    events = [
                        ("message_start", {"message": {"id": "fixture", "type": "message", "role": "assistant", "model": body["model"], "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}}),
                        ("content_block_start", {"index": 0, "content_block": block}),
                        ("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": message["tool_calls"][0]["function"]["arguments"]} if tool else {"type": "text_delta", "text": message["content"]}}),
                        ("content_block_stop", {"index": 0}),
                        ("message_delta", {"delta": {"stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}}),
                        ("message_stop", {})]
                    for kind, event in events:
                        self.wfile.write(("event: " + kind + "\ndata: " + json.dumps({"type": kind, **event}) + "\n\n").encode())
                    self.wfile.flush()
                elif body.get("stream"):
                    delta = dict(message)
                    if tool:
                        delta["tool_calls"][0]["index"] = 0
                    for entry in [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}],
                                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]:
                        self.wfile.write(("data: " + json.dumps({"id": "fixture", "object": "chat.completion.chunk", "created": int(time.time()), "model": body["model"], **entry}) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.wfile.write(json.dumps({"id": "fixture", "object": "chat.completion", "created": int(time.time()),
                        "model": body["model"], "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool else "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode())
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    previous = os.environ.get("QQBOT_DSH_FIXTURE_KEY")
    os.environ["QQBOT_DSH_FIXTURE_KEY"] = "local-fixture-only"
    clients = []
    try:
        with tempfile.TemporaryDirectory(prefix="qqbot-dsh-sdk-", ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            if mcp_runtime:
                executable = Path(mcp_runtime).resolve() / "QQBotMCP.exe"
                assert executable.is_file()
                dsh_profile.module_command = lambda role: [str(executable), "--home", str(root)] if role == "mcp" else original_command(role)
            broker = ConsoleBroker(root / "console.sqlite3")
            context["file"] = broker.issue_context({"namespace": "fixture", "scope": "fixture:private:1", "sender_id": "1",
                "main_agent": True, "is_group": False, "voice": {"enabled": True, "language": "ja", "character": "fixture", "emotions": ["neutral"]}}, root / "contexts")
            config = DSHConfig(home=str(root / "dsh"), workspace=str(root), base_url=f"http://127.0.0.1:{server.server_port}/v1",
                api_key_env="QQBOT_DSH_FIXTURE_KEY")
            mapping = root / "sessions.json"
            client = DeepSeekHarnessClient(config, sessions_file=str(mapping))
            clients.append(client)
            trace = []
            answer = await client.chat("onebot_private_fixture", "Query the current voice capability using the QQBot MCP tool.",
                system_prompt="Keep one persistent QQBot identity. Use the provided console context only.", response_trace=trace)
            assert answer == "DSH fixture completed", answer
            assert len(requests) >= 2, "The model fixture did not receive the MCP continuation"
            names = [row.get("name", row.get("function", {}).get("name", "")) for row in requests[0].get("tools", [])]
            assert "mcp__qqbot__qqbot_cli" in names, names
            assert {name for name in names if name.startswith("mcp__qqbot__")} == {
                "mcp__qqbot__qqbot_cli", "mcp__qqbot__qqbot_reply", "mcp__qqbot__qqbot_read_memory", "mcp__qqbot__qqbot_console_help"}, names
            non_mcp = [name for name in names if not name.startswith("mcp__qqbot__")]
            # First-party MCP resource discovery can only address connected servers.
            assert all(name in ("list_mcp_resources", "read_mcp_resource", "list_mcp_resource_templates") for name in non_mcp), non_mcp
            assert any(row.get("type") == "TOOL_RESULT" and not row.get("isError") for row in trace), trace
            cid = client.get_or_create_session("onebot_private_fixture").conversation_id
            assert client.get_or_create_session("onebot_private_fixture").chat_state.get("dsh_created")
            await client.aclose()
            native_layout = [str(path.relative_to(root / "dsh/sessions")) for path in (root / "dsh/sessions").rglob("*") if path.is_file()]
            assert native_layout, "No native persistent session files"
            restored = DeepSeekHarnessClient(config, sessions_file=str(mapping))
            clients.append(restored)
            assert restored.get_or_create_session("onebot_private_fixture").conversation_id == cid
            before = len(requests)
            answer = await restored.chat("onebot_private_fixture", "Continue the same conversation.", system_prompt="Keep one persistent QQBot identity. Use the provided console context only.")
            assert restored.get_or_create_session("onebot_private_fixture").conversation_id == cid
            history = requests[before].get("messages", [])
            assert sum(row.get("role") == "user" for row in history) >= 2, "Persisted history was not restored"
            assert any(row.get("role") == "tool" or any(block.get("type") == "tool_result" for block in row.get("content", []) if isinstance(block, dict)) for row in history), "Previous MCP result missing after resume"
            assert cid in restored._runtimes
            assert restored.reset_session("onebot_private_fixture")
            await restored.aclose()
            assert not restored._runtimes
            assert restored.get_or_create_session("onebot_private_fixture").conversation_id is None
            await restored.aclose()
            cancelled = DeepSeekHarnessClient(config, sessions_file=str(root / "cancel-map.json"))
            clients.append(cancelled)
            request_ready.clear()
            slow.set()
            task = asyncio.create_task(cancelled.chat("onebot_private_cancel", "Cancellation fixture"))
            for _ in range(100):
                if request_ready.is_set():
                    break
                await asyncio.sleep(0.1)
            assert request_ready.is_set(), "No cancellable request started"
            task.cancel()
            try:
                await asyncio.wait_for(task, 12)
                raise AssertionError("Cancelled request returned a reply")
            except asyncio.CancelledError:
                pass
            assert not cancelled._runtimes, "A cancelled runtime was left running"
            return {"ok": True, "sdk": "0.2.2rc2", "model_transport": "loopback fixture; no real model or QQ send",
                "mcp_runtime": str(mcp_runtime) if mcp_runtime else "source Python",
                "checks": ["real SDK process", "QQBot MCP tool invocation", "main tool isolation", "durable history resume",
                           "same conversation id", "reset closes native runtime", "cancel closes native runtime"], "tools": names,
                "requests": len(requests), "trace_rows": len(trace), "native_layout": native_layout}
    except BaseException:
        print(json.dumps({"diagnostics": diagnostics, "requests": requests}, ensure_ascii=False))
        raise
    finally:
        Session.run = real_run
        dsh_profile.module_command = original_command
        finished.set()
        for client in clients:
            await client.aclose()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        if previous is None:
            os.environ.pop("QQBOT_DSH_FIXTURE_KEY", None)
        else:
            os.environ["QQBOT_DSH_FIXTURE_KEY"] = previous


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--mcp-runtime", type=Path)
    args = parser.parse_args()
    result = asyncio.run(verify(args.mcp_runtime))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
