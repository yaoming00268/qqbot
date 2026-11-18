from agent_transcript import repeated_model_output


def model(text, step):
    return {"type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE", "content": text, "step_index": step}


def test_repeated_output_requires_consecutive_identical_completed_responses():
    rows = [model("No tool calls needed.", i) for i in range(4)]
    assert repeated_model_output(rows)
    assert not repeated_model_output(rows[:3])
    assert not repeated_model_output(rows[:2] + [{"type": "TOOL_RESULT", "source": "TOOL"}] + rows[2:])
    assert not repeated_model_output(rows[:2] + [dict(model("working", 2), tool_calls=[{"name": "read"}])] + rows[2:])
    assert not repeated_model_output(rows[:2] + [model("new result", 2)] + rows[2:])
    assert not repeated_model_output(rows[:2] + [{"type": "USER_INPUT", "content": "new information"}] + rows[2:])


def test_later_tool_progress_resets_an_earlier_repeat():
    rows = [model("repeat", i) for i in range(4)]
    assert not repeated_model_output(rows + [{"type": "TOOL_RESULT", "source": "TOOL"}])


def test_streaming_continuation_filler_is_detected_but_later_work_resets():
    signature = 'The user says "Continue" again. No tool calls needed.'
    filler = "".join(signature + text for text in ["随时待命", "准备好了", "一直陪伴", "等待吩咐"])
    streaming = dict(model(filler, 1), status="RUNNING")
    assert repeated_model_output([streaming])
    assert not repeated_model_output([dict(streaming, content=signature)])
    assert not repeated_model_output([streaming, {"type": "TOOL_RESULT", "source": "TOOL"}])
    assert not repeated_model_output([streaming, {"type": "USER_INPUT", "content": "补充要求"}])


def test_varied_standby_and_many_distinct_no_tool_steps():
    chinese = "随时待命。等待吩咐。准备就绪。随时准备。"
    assert repeated_model_output([model(chinese, 1)])
    varied = [model("阶段说明 " + str(i), i) for i in range(8)]
    assert not repeated_model_output(varied)
    assert repeated_model_output(varied, long_idle=True)
    assert not repeated_model_output(varied[:4] + [{"type": "TOOL_RESULT", "source": "TOOL"}] + varied[4:], long_idle=True)
    assert not repeated_model_output(varied[:4] + [{"type": "USER_INPUT"}] + varied[4:], long_idle=True)
import asyncio
import json
from unittest.mock import AsyncMock, call

import unittest
from antigravity_client import AntigravityClient, RepeatedModelOutputError
from test_agent_reply_latency import inbound


class NativeLoopTests(unittest.IsolatedAsyncioTestCase):
 async def test_native_wait_detects_streaming_loop_for_bound_request(self):
    import tempfile
    from pathlib import Path
    temp = tempfile.TemporaryDirectory()
    self.addCleanup(temp.cleanup)
    tmp_path = Path(temp.name)
    client = AntigravityClient(sessions_file=str(tmp_path / "sessions.json"))
    client._brain_dir = str(tmp_path)
    client._check_conversation_present = AsyncMock()
    path = tmp_path / "cid/.system_generated/logs/transcript.jsonl"
    path.parent.mkdir(parents=True)
    marker = "[qqbot_input:test]"
    phrase = 'The user says "Continue" again. No tool calls needed.'
    row = dict(model("".join(phrase + text for text in ["待命", "出发", "陪伴", "准备"]), 2), status="RUNNING")
    path.write_text("\n".join(json.dumps(item) for item in [inbound(1, marker), row]) + "\n", encoding="utf-8")
    with self.assertRaises(RepeatedModelOutputError):
        await asyncio.wait_for(client._wait_for_model_response("cid", expected_step_min=1,
            request_marker=marker, stop_requested=lambda: False), 2)


def test_varied_chinese_standby_across_model_steps_resets_on_work():
    rows = [model(text, i) for i, text in enumerate(["随时待命", "等待吩咐", "准备就绪", "随时准备"])]
    assert repeated_model_output(rows)
    assert not repeated_model_output(rows[:2] + [{"type": "TOOL_RESULT", "source": "TOOL"}] + rows[2:])

class WorkerFinalReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_report_requests_stop_and_preserves_result(self):
        from test_subagent_runtime import SubagentRuntimeTests
        fixture = SubagentRuntimeTests()
        fixture.setUp()
        try:
            bot, key, coordinator, _ = await fixture.prepare()
            from console_tools.cli import execute
            observed = []
            async def chat(session_key, prompt, **options):
                if not session_key.startswith("__work__:agent:"):
                    return ""
                context = json.loads(prompt.splitlines()[0])["console_context_file"]
                self.assertFalse(options["stop_requested"]())
                execute("agents", context, operation="report", params={"text": "真实结果", "finish": True})
                observed.append(options["stop_requested"]())
                return ""
            bot.ai.chat.side_effect = chat
            context = fixture.context(coordinator, key)
            receipt = execute("agents", context, operation="create", params={"name": "测试", "text": "任务"})
            await fixture.settle(coordinator)
            self.assertEqual(observed, ["reported"])
            messages = execute("agents", context, operation="read", params={"agent_id": receipt["id"]})["messages"]
            self.assertTrue(any(row["text"] == "真实结果" for row in messages))
        finally:
            await fixture.asyncTearDown()

class StopConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sessions.json"
        self.client = AntigravityClient(sessions_file=str(self.path))
        self.session = self.client.get_or_create_session("group")
        self.session.conversation_id = "owned-cid"
        self.client._save_sessions()

    async def test_polling_waits_for_idle_and_unknown_never_retries_cancel(self):
        self.client._retry_run_status = AsyncMock(side_effect=[
            {"status": "CASCADE_RUN_STATUS_RUNNING"}, {"status": "CASCADE_RUN_STATUS_IDLE"}])
        self.assertTrue(await self.client._confirm_native_idle("owned-cid", timeout=1))
        self.client._retry_run_status = AsyncMock(return_value=None)
        retry_stop = AsyncMock()
        self.assertFalse(await self.client._confirm_native_idle("owned-cid", timeout=.01, retry_stop=retry_stop))
        retry_stop.assert_not_awaited()

    async def test_unconfirmed_survives_restart_and_blocks_send_then_recovers(self):
        from antigravity_client import StopUnconfirmedError
        self.client._confirm_native_idle = AsyncMock(return_value=False)
        self.assertFalse(await self.client._cancel_native_conversation("owned-cid", kill_background_tasks=False))
        restored = AntigravityClient(sessions_file=str(self.path))
        restored._is_ready = True
        restored._confirm_native_idle = AsyncMock(return_value=False)
        restored._run_agentapi_cmd = AsyncMock()
        with self.assertRaises(StopUnconfirmedError):
            await restored.chat("group", "新消息")
        restored._run_agentapi_cmd.assert_not_awaited()
        self.assertEqual(restored.get_or_create_session("group").conversation_id, "owned-cid")
        restored._confirm_native_idle.return_value = True
        restored._is_ready = True
        restored._run_agentapi_cmd.side_effect = RuntimeError("send reached")
        with self.assertRaises(Exception):
            await restored.chat("group", "新消息", raise_on_error=True)
        self.assertNotIn("stop_pending", restored.get_or_create_session("group").chat_state)
        restored._run_agentapi_cmd.assert_awaited()

    async def test_running_pending_stop_retries_cancel_before_sending(self):
        from antigravity_client import StopUnconfirmedError

        self.client._is_ready = True
        self.session.chat_state["stop_pending"] = "owned-cid"
        self.client._retry_run_status = AsyncMock(return_value={"status": "CASCADE_RUN_STATUS_RUNNING"})
        self.client._cancel_native_conversation = AsyncMock(side_effect=[False, True])
        self.client._run_agentapi_cmd = AsyncMock(side_effect=RuntimeError("send reached"))

        with self.assertRaises(StopUnconfirmedError):
            await self.client.chat("group", "本轮被阻止")
        self.client._run_agentapi_cmd.assert_not_awaited()
        self.assertEqual(self.session.chat_state["stop_pending"], "owned-cid")

        with self.assertRaisesRegex(RuntimeError, "send reached"):
            await self.client.chat("group", "新消息", raise_on_error=True)
        self.assertNotIn("stop_pending", self.session.chat_state)
        self.assertEqual(self.client._cancel_native_conversation.await_args_list,
                         [call("owned-cid", kill_background_tasks=False)] * 2)
        args = self.client._run_agentapi_cmd.await_args.args[0]
        self.assertEqual(args[:2], ["send-message", "owned-cid"])
        self.assertIn("新消息", args[-1])

    async def test_changed_owner_during_status_read_does_not_retry_cancel(self):
        from antigravity_client import SessionInvalidatedError

        async def running(*args, **kwargs):
            self.session.generation += 1
            self.session.conversation_id = "new-cid"
            self.session.chat_state["stop_pending"] = "new-cid"
            return {"status": "CASCADE_RUN_STATUS_RUNNING"}

        self.client._is_ready = True
        self.session.chat_state["stop_pending"] = "owned-cid"
        self.client._retry_run_status = AsyncMock(side_effect=running)
        self.client._cancel_native_conversation = AsyncMock()
        self.client._run_agentapi_cmd = AsyncMock()
        with self.assertRaises(SessionInvalidatedError):
            await self.client.chat("group", "新消息")
        self.client._cancel_native_conversation.assert_not_awaited()
        self.client._run_agentapi_cmd.assert_not_awaited()
        self.assertEqual(self.session.chat_state["stop_pending"], "new-cid")

    async def test_generation_change_during_cancel_cannot_mark_new_session(self):
        async def change(_cid):
            self.session.generation += 1
            self.session.conversation_id = "new-cid"
            self.session.chat_state["stop_pending"] = "new-cid"
            return True
        self.client._confirm_native_idle = AsyncMock(side_effect=change)
        self.assertTrue(await self.client._cancel_native_conversation("owned-cid"))
        self.assertEqual(self.session.chat_state["stop_pending"], "new-cid")
    async def test_rpc_receipt_or_failure_never_substitutes_for_idle(self):
        from unittest.mock import patch
        class Response:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def raise_for_status(self): pass
        class Http:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def post(self, *args, **kwargs): return Response()
        self.client.ls_address = "fixture.invalid"
        self.client.csrf_token = "fixture"
        self.client._confirm_native_idle = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=Http()):
            self.assertFalse(await self.client._cancel_native_conversation("owned-cid"))
        self.assertEqual(self.session.chat_state["stop_pending"], "owned-cid")
        self.client._confirm_native_idle.return_value = True
        with patch("aiohttp.ClientSession", side_effect=OSError("fixture failure")):
            self.assertTrue(await self.client._cancel_native_conversation("owned-cid"))
        self.assertNotIn("stop_pending", self.session.chat_state)


def test_guard_waits_for_tool_receipt():
    from agent_transcript import has_pending_tool_calls
    call = dict(model("", 1), tool_calls=[{"name": "qqbot_cli", "id": "report-1"}])
    assert has_pending_tool_calls([call])
    assert not has_pending_tool_calls([call, {"type": "TOOL_RESULT", "tool_call_id": "report-1"}])
    assert repeated_model_output([dict(model("随时待命。等待吩咐。准备就绪。随时准备。", 2), status="RUNNING")])

class ReportLoopReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_reported_loop_ends_only_after_report_receipt(self):
        import tempfile
        from pathlib import Path
        from antigravity_client import ReplyDeferred
        with tempfile.TemporaryDirectory() as directory:
            client = AntigravityClient(sessions_file=str(Path(directory) / "sessions.json"))
            client._brain_dir = directory
            path = Path(directory) / "cid/.system_generated/logs/transcript.jsonl"
            path.parent.mkdir(parents=True)
            marker = "[qqbot_input:report]"
            call = dict(model("", 2), tool_calls=[{"name": "qqbot_cli", "id": "report-1"}])
            loop = dict(model('The user says "Continue" again. No tool calls needed.' * 4, 3), status="RUNNING")
            rows = [inbound(1, marker), call, loop]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            task = asyncio.create_task(client._wait_for_model_response("cid", expected_step_min=1,
                request_marker=marker, stop_requested=lambda: "reported"))
            await asyncio.sleep(.25)
            self.assertFalse(task.done())
            rows.insert(2, {"step_index": 3, "type": "TOOL_RESULT", "tool_call_id": "report-1", "status": "DONE"})
            rows[3]["step_index"] = 4
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            with self.assertRaises(ReplyDeferred) as caught:
                await asyncio.wait_for(task, 2)
            self.assertEqual(caught.exception.mode, "finished")

class WorkerCancellationIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_unknown_keeps_work_session_across_restart_and_blocks_send(self):
        import tempfile
        from pathlib import Path
        from antigravity_client import StopUnconfirmedError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            client = AntigravityClient(sessions_file=str(path))
            client._is_ready = True
            key = "__work__:agent:test"
            session = client.get_or_create_session(key)
            session.conversation_id = "worker-cid"
            entered = asyncio.Event()
            async def waiting(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            client._run_agentapi_cmd = AsyncMock(side_effect=waiting)
            client._confirm_native_idle = AsyncMock(return_value=False)
            task = asyncio.create_task(client.chat(key, "任务", raise_on_error=True))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(session.conversation_id, "worker-cid")
            self.assertEqual(session.chat_state.get("stop_pending"), "worker-cid")
            restored = AntigravityClient(sessions_file=str(path))
            restored._is_ready = True
            restored._confirm_native_idle = AsyncMock(return_value=False)
            restored._run_agentapi_cmd = AsyncMock()
            with self.assertRaises(StopUnconfirmedError):
                await restored.chat(key, "后续任务")
            restored._run_agentapi_cmd.assert_not_awaited()
            self.assertEqual(restored.get_or_create_session(key).conversation_id, "worker-cid")
