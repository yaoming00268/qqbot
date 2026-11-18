"""Official DeepSeek Harness SDK with the bot's existing durable session contract.

Only pure session/memory bookkeeping is inherited from AntigravityClient.
All transport, cancellation, probing and archive operations below are DSH-owned.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid

from antigravity_client import (AgentServiceError, AgentServiceTimeoutError,
    AntigravityClient, ReplyDeferred, SessionInvalidatedError)
from dsh_config import DSHConfig
from dsh_profile import build_patch


class DSHServiceError(AgentServiceError):
    code = "DSH_SERVICE_UNAVAILABLE"
    user_message = "DeepSeek Harness 服务未完成本次请求；原会话已保留，请检查 DSH 配置与凭据。"


def normalized_trace(notifications):
    result = []
    for notification in notifications:
        if notification.method != "session.event":
            continue
        event = notification.payload.get("event", {})
        kind, data = event.get("type"), event.get("data", {})
        if kind == "tool/call":
            name = data.get("name", "")
            # Preserve the bot's existing acquisition evidence vocabulary.
            name = {"web_search": "search_web", "web_fetch": "read_url_content", "fetch": "read_url_content",
                    "bash": "run_command", "pwsh": "run_command"}.get(name, name)
            call = {"id": data.get("callId", ""), "name": name, "args": data.get("arguments", "{}")}
            if result and result[-1].get("type") == "PLANNER_RESPONSE":
                result[-1]["tool_calls"].append(call)
            else:
                result.append({"type": "PLANNER_RESPONSE", "tool_calls": [call], "status": "DONE"})
        elif kind == "tool/result":
            message = data.get("message", {})
            content = message.get("content", [])
            text = content if isinstance(content, str) else "\n".join(
                block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
            result.append({"type": "TOOL_RESULT", "source": "TOOL", "status": "DONE",
                "tool_call_id": message.get("toolCallId", message.get("callId", "")),
                "content": text, "isError": bool(message.get("isError") or data.get("error"))})
    return result


class DeepSeekHarnessClient(AntigravityClient):
    def __init__(self, config: DSHConfig | dict | None = None, *, system_prompt="", sessions_file="data/dsh_sessions.json", account_key=""):
        self.dsh = DSHConfig.model_validate(config or {})
        self._runtimes = {}
        self._runtime_lock = threading.RLock()
        # No AG discovery, HTTP call, process or brain directory is used.
        super().__init__(default_model=self.dsh.model, system_prompt=system_prompt,
            sessions_file=sessions_file, session_timeout_minutes=0, account_key=account_key)
        self._brain_dir = str(Path(self.dsh.home).resolve() / "sessions")

    def initialize(self):
        from deepseek_harness_runtime import bundled_runtime_path
        if importlib.metadata.version("deepseek-harness-sdk") != "0.2.2rc2":
            raise DSHServiceError()
        self._is_ready = bundled_runtime_path().is_file()
        return self._is_ready

    def bind_account(self, account_key):
        if not account_key or account_key == self.account_key:
            return
        if self._runtimes:
            raise RuntimeError("正在使用的 DSH 客户端不能切换账号，请为账号创建独立客户端。")
        self._save_sessions()
        self.account_key = account_key
        self.sessions_file = self._resolve_sessions_file(account_key)
        self._sessions.clear()
        self._load_sessions()

    def _scope(self, key, cascade):
        if self._is_probe_session(key):
            return "probe"
        planner = (cascade or {}).get("plannerConfig", {})
        if self._is_work_session(key):
            components = planner.get("declarativeMixinConfig", {}).get("tools", [])
            if components and any(row.get("name") == "read_url_content" for row in components):
                return "public"
            return "work"
        return "main"

    def _open(self, session, scope, prompt):
        from deepseek_harness import DeepSeekHarness
        home = Path(self.dsh.home).resolve()
        patch = build_patch(home, scope=scope, system_prompt=prompt)
        signature = (scope, str(patch), session.model)
        with self._runtime_lock:
            saved = self._runtimes.get(session.conversation_id)
            if saved and saved[0] == signature:
                return saved[1], False
            if saved:
                saved[1].close()
                self._runtimes.pop(session.conversation_id, None)
            environment = {"DSH_PERMISSION_MODE": "workspace-write", "NO_COLOR": "1"}
            options = {"dsh_home": str(home), "cwd": str(Path(self.dsh.workspace).resolve()),
                "provider": self.dsh.provider, "model": session.model, "patches": (str(patch),),
                "env": environment, "initialize_timeout_seconds": self.dsh.initialize_timeout_seconds,
                "shutdown_timeout_seconds": 5,
                "on_user_question": lambda request, cancelled: {"cancelled": True}}
            if self.dsh.base_url:
                options["base_url"] = self.dsh.base_url
            key = os.environ.get(self.dsh.api_key_env)
            if key:
                options["api_key"] = key
            if self.dsh.reasoning_effort:
                options["reasoning_effort"] = self.dsh.reasoning_effort
            if self.dsh.max_tokens:
                options["max_tokens"] = self.dsh.max_tokens
            harness = DeepSeekHarness(**options)
            existing = bool(session.conversation_id)
            if not existing:
                session.conversation_id = "qqbot-" + uuid.uuid4().hex
                session.chat_state["dsh_created"] = False
                self._save_sessions()
            self._runtimes[session.conversation_id] = (signature, harness)
            return harness, existing

    async def chat(self, session_key, user_text, raise_on_error=False, timeout=None, system_prompt=None,
            response_trace=None, native_only=False, memory_manifest=None, allow_empty_response=False,
            bind_response=False, stop_requested=None, cascade_config=None, single_response=False):
        session = self.get_or_create_session(session_key)
        generation = session.generation
        async with session.lock:
            self._check_generation(session, generation)
            if not self._is_ready:
                self.initialize()
            text, plan = self._prepare_memory_request(session, memory_manifest, str(user_text))
            prompt = self.system_prompt if system_prompt is None else system_prompt
            opening = asyncio.create_task(asyncio.to_thread(self._open, session, self._scope(session_key, cascade_config), prompt))
            try:
                harness, existing = await asyncio.shield(opening)
            except asyncio.CancelledError:
                harness, _ = await opening
                await self._cancel_native_conversation(session.conversation_id)
                raise
            cid = session.conversation_id
            observations, stopped = [], threading.Event()
            def observe(notification):
                observations.append(notification)
                if notification.method == "session.event" and not session.chat_state.get("dsh_created"):
                    session.chat_state["dsh_created"] = True
                    self._save_sessions()
            def run():
                if stopped.is_set():
                    return None
                harness.start()
                diagnostics = harness.client._runtime_diagnostics()
                if "did not activate" in diagnostics and any(name in diagnostics for name in
                        ("qqbot-console", "session-persistence-jsonl", "system-prompt", "permission")):
                    raise DSHServiceError()
                if stopped.is_set():
                    return None
                # A live runtime already owns its Session. A freshly started
                # runtime must explicitly restore a previously persisted id.
                if not getattr(harness, "_qqbot_session_ready", False):
                    if existing and session.chat_state.get("dsh_created"):
                        handle = harness.resume_session(cid)
                    else:
                        handle = harness.start_session(cid)
                    harness._qqbot_session_ready = True
                    harness._qqbot_session_handle = handle
                return harness._qqbot_session_handle.run("[QQBot 持久会话事件]\n" + text, on_notification=observe)
            task = asyncio.create_task(asyncio.to_thread(run))
            start = time.monotonic()
            try:
                while not task.done():
                    self._check_generation(session, generation)
                    if stop_requested and (decision := stop_requested()):
                        raise ReplyDeferred(decision)
                    if timeout and timeout > 0 and time.monotonic() - start > timeout:
                        raise AgentServiceTimeoutError()
                    await asyncio.wait({task}, timeout=0.1)
                result = task.result()
                self._check_generation(session, generation)
                if result is None or result.finish_reason in ("error", "aborted", "max-tokens"):
                    raise DSHServiceError()
                answer = str(result.final_response or "")
                if not answer.strip() and not allow_empty_response:
                    raise DSHServiceError()
                trace = normalized_trace(observations)
                if response_trace is not None:
                    response_trace.extend(trace)
                self._remember_memory_reads(session, plan, trace)
                session.last_active = time.time()
                self._save_sessions()
                return answer
            except BaseException as error:
                stopped.set()
                # close() shuts down this conversation's runtime, drains native
                # work, and fails waiters. Never leave a to_thread run behind.
                await asyncio.shield(asyncio.to_thread(harness.close))
                with self._runtime_lock:
                    self._runtimes.pop(cid, None)
                if not task.done():
                    try:
                        await asyncio.shield(task)
                    except BaseException:
                        pass
                else:
                    try:
                        task.exception()
                    except BaseException:
                        pass
                if isinstance(error, (asyncio.CancelledError, ReplyDeferred, SessionInvalidatedError, AgentServiceError)):
                    raise
                raise DSHServiceError() from error

    async def _cancel_native_conversation(self, conversation_id, *, kill_background_tasks=True):
        with self._runtime_lock:
            saved = self._runtimes.pop(conversation_id, None)
        if saved:
            await asyncio.to_thread(saved[1].close)
        return True

    async def probe(self, timeout=15):
        # Start the real official binary, initialize the configured model route
        # and MCP-free scope, then close. No model request or billable input.
        from deepseek_harness import DeepSeekHarness
        patch = build_patch(self.dsh.home, scope="probe")
        options = {"dsh_home": str(Path(self.dsh.home).resolve()), "cwd": str(Path(self.dsh.workspace).resolve()),
            "provider": self.dsh.provider, "model": self.dsh.model, "patches": (str(patch),),
            "initialize_timeout_seconds": timeout, "shutdown_timeout_seconds": 3}
        if self.dsh.base_url:
            options["base_url"] = self.dsh.base_url
        if os.environ.get(self.dsh.api_key_env):
            options["api_key"] = os.environ[self.dsh.api_key_env]
        harness = DeepSeekHarness(**options)
        try:
            await asyncio.to_thread(harness.start)
            self._is_ready = True
            return True, "官方 DSH SDK 0.2.2rc2 握手成功；尚未发送模型请求。"
        except Exception as error:
            return False, "DSH 初始化失败（" + type(error).__name__ + "）；请检查模型路由、专用 Home 和凭据。"
        finally:
            await asyncio.to_thread(harness.close)

    def set_session_model(self, session_key, model_name):
        target = self.dsh.model_aliases.get(model_name, model_name)
        if not target or len(target) > 200:
            return False
        session = self.get_or_create_session(session_key)
        # Changing a model preserves its durable history; _open reinitializes
        # the selected route and resume_session restores the same identity.
        session.model = target
        self._save_sessions()
        return True

    def queue_probe_cleanup(self, outcome="startup"):
        return 0

    def _close_reset_runtimes(self, ids):
        for cid in filter(None, ids):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                with self._runtime_lock:
                    saved = self._runtimes.pop(cid, None)
                if saved:
                    saved[1].close()
            else:
                task = loop.create_task(self._cancel_native_conversation(cid))
                pending = self.__dict__.setdefault("_closing_tasks", set())
                pending.add(task)
                task.add_done_callback(pending.discard)

    def reset_session(self, session_key):
        ids = [self._sessions[key].conversation_id for key in (session_key, self._native_session_key(session_key)) if key in self._sessions]
        result = super().reset_session(session_key)
        self._close_reset_runtimes(ids)
        return result

    def reset_all_sessions(self):
        ids = [session.conversation_id for session in self._sessions.values()]
        super().reset_all_sessions()
        self._close_reset_runtimes(ids)

    async def archive_session(self, session_key, archive_path, *, expected_id=None):
        session = self._sessions.get(session_key)
        if not session or not session.conversation_id or session.lock.locked() or not self._is_cleanup_session(session_key):
            return False
        if expected_id and session.conversation_id != expected_id:
            return False
        evidence = Path(archive_path).resolve()
        if not evidence.is_file() or not evidence.stat().st_size:
            return False
        cid = session.conversation_id
        await self._cancel_native_conversation(cid)
        # Native history stays in DSH Home, plus an owned immutable archive copy.
        target = evidence.parent / ("dsh-" + cid)
        target.mkdir(exist_ok=True)
        sources = [path for path in (Path(self.dsh.home).resolve() / "sessions").rglob(cid) if path.is_dir()]
        for index, source in enumerate(sources):
            shutil.copytree(source, target / str(index), dirs_exist_ok=True)
        (target / "reference.json").write_text(json.dumps({"session_id": cid, "backend": "deepseek_harness",
            "native_home": str(Path(self.dsh.home).resolve()), "evidence": str(evidence), "copies": len(sources)}), encoding="utf-8")
        self._invalidate(session)
        self._save_sessions()
        return True

    async def _try_archive_record(self, record):
        cid = record["conversation_id"]
        result = await self.archive_session(record["session_key"], record["archive"], expected_id=cid)
        session = self._sessions.get(record["session_key"])
        queue = self._archive_queue()
        if result or session is None or session.conversation_id != cid:
            queue.pop(cid, None)
        elif cid in queue:
            queue[cid]["attempts"] = record.get("attempts", 0) + 1
            queue[cid]["next_attempt"] = time.time() + min(300, 2 ** min(8, queue[cid]["attempts"]))
        self._save_archive_queue(queue)
        return result

    async def aclose(self):
        pending = list(getattr(self, "_closing_tasks", set()))
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        with self._runtime_lock:
            runtimes, self._runtimes = list(self._runtimes.values()), {}
        for _, harness in runtimes:
            await asyncio.to_thread(harness.close)
