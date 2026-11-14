"""In-memory Antigravity contract for transport tests; no model or storage IO."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
from unittest.mock import AsyncMock, MagicMock

from antigravity_client import AntigravityClient, UserSession


def finish_console_fixture(context_file):
    """A compliant fixture agent explicitly commits its tools, then finishes."""
    from console_tools.broker import ConsoleBroker
    from console_tools.cli import execute
    broker, token = ConsoleBroker.from_context(context_file)
    context = broker.context(token)
    if (any(context.get(key) for key in ("agent_worker_id", "sandbox_session_id", "parent_request_id", "activity_controller"))
            or broker.reply_control(token).get("mode") in ("wait", "finished", "sealed", "reported")):
        return
    if broker.staged_messages(token) and broker.reply_control(token).get("mode") != "committed":
        execute("commit", context_file)
    if context.get("memory_maintenance"):
        revision = execute("preset", context_file, operation="show")["revision"]
        for archive in execute("memory", context_file, operation="pending", params={"limit": 100})["archives"]:
            execute("memory", context_file, operation="review", params={"archive_id": archive["archive_id"],
                "decision": "keep", "revision": revision, "reason": "测试模型已核对本次归档，保留当前成长预设。"})
    execute("finish", context_file)


class ScriptedConsoleAgentMock(AsyncMock):
    """Run only the fixture's scripted CLI actions, then explicitly finish.

    Unlike ConsoleAgentMock this never translates model text into actions.
    Protocol tests use plain AsyncMock when exercising a missing finish.
    """
    async def _execute_mock_call(self, *args, **kwargs):
        value = await super()._execute_mock_call(*args, **kwargs)
        if len(args) >= 2:
            import json
            for line in str(args[1]).splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("console_context_file"):
                    finish_console_fixture(item["console_context_file"])
                    break
        return value


class ConsoleAgentMock(AsyncMock):
    """Submit existing test dialogue fixtures through the v2 public CLI.

    Fixtures for memory, timing and images still describe dialogue as text/JSON.
    This test model executes the corresponding CLI calls instead of asking the
    production runtime to interpret final output. CLI contract regressions use
    a plain AsyncMock in test_console_v2 and exercise calls explicitly.
    """
    async def _execute_mock_call(self, *args, **kwargs):
        value = await super()._execute_mock_call(*args, **kwargs)
        if len(args) < 2 or not isinstance(value, str):
            return value
        import json
        from console_tools.broker import ConsoleBroker
        from console_tools.cli import execute
        from chat_turns import plain_reply
        context_file = ""
        result_event = None
        for line in str(args[1]).splitlines():
            if line.startswith('{"console_context_file":'):
                context_file = json.loads(line)["console_context_file"]
            elif line.startswith('{"type": "task_result"'):
                result_event = json.loads(line)
        if not context_file:
            return value
        broker, token = ConsoleBroker.from_context(context_file)
        if broker.reply_control(token).get("mode") in ("wait", "finished", "sealed", "reported"):
            return value
        def finish():
            finish_console_fixture(context_file)
        if broker.context(token).get("idle_event"):
            finish()
            return value
        if result_event:
            payload = json.loads(broker.record(result_event["request_id"])["result"])
            if not (payload.get("delivery") or {}).get("approved_by"):
                row = broker.record(result_event["request_id"])
                text = result_event.get("answer") if row["kind"] in ("bot_command", "command_error", "media_command") else ""
                execute("deliver", context_file, request_id=result_event["request_id"],
                        text=text or ("任务完成" if result_event.get("success") else "任务未完成"))
            finish()
            return value
        if broker.staged_messages(token):
            finish()
            return value
        decision = plain_reply(value, 1200)
        # Fixtures declaring a completed answer make the same explicit decision
        # required from the real agent; the production runtime never auto-readies.
        if broker.context(token).get("speech_check_required"):
            execute("speech", context_file, operation="ready")
        if decision.skill_name:
            params = decision.skill_params or {}
            kind = {"web_search": "search", "send_image": "image", "send_video": "video", "send_file": "file"}.get(decision.skill_name, "task")
            receipt = execute("submit", context_file, task_type=kind,
                task=str(params.get("task") or params.get("query") or params.get("content") or "测试任务"))
            broker.update(receipt["id"], "submitted", {"legacy_skill": decision.skill_name, "params": params})
            finish()
            return value
        for task in decision.tasks:
            execute("submit", context_file, task_type=task.get("type", "task"), task=task["task"])
        if decision.should_reply:
            for index, item in enumerate(decision.messages):
                execute("message", context_file, text=item["text"], request_key="fixture:" + str(index))
        finish()
        return value


async def drain_runtime(bot):
    import asyncio
    for _ in range(20):
        runtime = bot._get_runtime()
        tasks = [task for task in runtime.chat_workers.values() if not task.done()]
        if runtime.work_worker and not runtime.work_worker.done():
            tasks.append(runtime.work_worker)
        coordinator = getattr(bot, "_main_coordinator", None)
        if coordinator:
            tasks.extend(task for task in coordinator.delivery_tasks.values() if not task.done())
        if not tasks:
            return
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
    raise AssertionError("background runtime did not drain")


def orchestrated_response(decision, worker=None, *, reply="任务完成，结果已发送 (´▽`)"):
    async def chat(key, prompt, **options):
        import inspect
        import json
        if '"type": "task_result"' in prompt:
            return json.dumps({"should_reply": True, "reply": reply})
        if key.startswith("__work__:task:"):
            if worker is None:
                from test_agent_artifacts import task_result
                return task_result(prompt, response_trace=options["response_trace"])
            value = worker(key, prompt, **options)
            return await value if inspect.isawaitable(value) else value
        return decision
    return chat


def mock_agent():
    ai = MagicMock(spec=AntigravityClient)
    ai.system_prompt = ""
    ai.default_model = "flash"
    ai._sessions = {}
    ai.chat = ConsoleAgentMock()
    def session(key):
        if key not in ai._sessions:
            ai._sessions[key] = UserSession(key, conversation_id="fixture-" + key)
        return ai._sessions[key]
    ai.get_or_create_session.side_effect = session
    return ai



def prompt_object(prompt, key):
    """Parse controller-owned JSON lines without depending on whitespace."""
    import json
    for line in prompt.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and key in value:
            return value
    raise AssertionError("Missing prompt object: " + key)
