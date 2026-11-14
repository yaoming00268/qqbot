"""Approved delivery finishes one reply turn and retains cancellation receipts."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from antigravity_client import AntigravityClient
from console_tools.cli import execute
from messaging import SendUnknown
from test_ai_fixture import drain_runtime


class ConsoleOwnedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import test_startup_participation
        self.fixture = test_startup_participation.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def agent(self, bot, key, *, count=3, task_started=None, release_task=None,
              busy_started=None, release_busy=None):
        from test_agent_artifacts import task_result
        async def respond(session_key, prompt, **options):
            self.assertEqual(options["timeout"], 0)
            if '"type": "task_result"' in prompt:
                event = next(json.loads(line) for line in prompt.splitlines() if line.startswith('{"type": "task_result"'))
                context = next(json.loads(line)["console_context_file"] for line in prompt.splitlines() if line.startswith('{"console_context_file":'))
                execute("deliver", context, request_id=event["request_id"], text="任务完成")
                return ""
            if session_key.startswith("__work__:task:"):
                if task_started:
                    task_started.set()
                if release_task:
                    await release_task.wait()
                return task_result(prompt, response_trace=options["response_trace"], kind="file", count=count)
            self.assertEqual(session_key, key)
            if "开始制作资料" in prompt:
                return '{"should_reply":false,"tasks":[{"type":"task","task":"制作全部资料文件"}]}'
            if busy_started:
                busy_started.set()
            if release_busy:
                await release_busy.wait()
            return '{"should_reply":true,"reply":"可以继续聊天"}'
        bot.ai.chat.side_effect = respond

    async def test_complete_batch_and_notice_finish_before_new_main_turn_starts(self):
        bot, key = self.fixture.bot(stored_cid="main-original")
        bot.app_config.antigravity.timeout_seconds = 1
        bot._is_ws_connected = lambda: True
        task_started, release_task = asyncio.Event(), asyncio.Event()
        busy_started, release_busy, uploading, release_upload, notified = (asyncio.Event() for _ in range(5))
        self.agent(bot, key, task_started=task_started, release_task=release_task,
                   busy_started=busy_started, release_busy=release_busy)
        order, upload_paths = [], []
        async def send_action(action, params, **options):
            if action == "upload_group_file":
                upload_paths.append(params["file"])
                if len(upload_paths) == 1:
                    uploading.set()
                    await release_upload.wait()
                order.append("file")
            return {"status": "ok", "data": {"file_id": "accepted"}}
        async def say(group, message):
            if getattr(message, "body", str(message)) == "任务完成":
                self.assertFalse(release_busy.is_set())
                self.assertEqual(message.quote, "")
                self.assertEqual(len(upload_paths), 3)
                order.append("complete")
                notified.set()
            return {"status": "ok", "data": {"message_id": "notice"}}
        bot.send_action.side_effect = send_action
        bot.send_group_message.side_effect = say
        await self.fixture.receive(bot, False, "source-task", "开始制作资料")
        await asyncio.wait_for(task_started.wait(), 1)
        release_task.set()
        await asyncio.wait_for(uploading.wait(), 2)
        new_message = {"post_type": "message", "message_type": "group", "group_id": 12345678,
            "user_id": 789, "message_id": "unrelated", "raw_message": "先说个别的话题",
            "sender": {"nickname": "另一位群友"}}
        bot._queue_event(new_message)
        try:
            await asyncio.sleep(.05)
            self.assertFalse(busy_started.is_set())
            release_upload.set()
            await asyncio.wait_for(notified.wait(), 2)
            await asyncio.wait_for(busy_started.wait(), 2)
            self.assertEqual(order, ["file", "file", "file", "complete"])
            self.assertEqual(bot.ai.chat.await_count, 4)  # initial main, worker, result review, busy main
            self.assertTrue(bot._get_main_coordinator().locks[key].locked())
            for path in upload_paths:
                self.assertTrue(Path(path).is_file())
        finally:
            release_busy.set()
            release_upload.set()
            release_task.set()
            await drain_runtime(bot)
        coordinator = bot._get_main_coordinator()
        with coordinator.broker.connect() as db:
            record = db.execute("SELECT * FROM requests").fetchone()
        self.assertEqual(record["state"], "delivered")
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "main-original")
        notice = next(c.args[1] for c in bot.send_group_message.await_args_list if getattr(c.args[1], "body", "") == "任务完成")
        self.assertEqual(notice.quote, "")

    async def test_completed_task_recovers_from_offline_with_no_main_agent_call(self):
        bot, key = self.fixture.bot(stored_cid="main-original")
        self.agent(bot, key, count=2)
        bot._is_ws_connected = lambda: False
        await self.fixture.receive(bot, False, "source-task", "开始制作资料")
        await drain_runtime(bot)
        coordinator = bot._get_main_coordinator()
        records = coordinator.broker.completed(coordinator.namespace, bot._image_scope("12345678"))
        self.assertEqual(len(records), 1)
        bot.send_action.assert_not_awaited()
        bot.send_group_message.assert_not_awaited()
        bot.ai.chat.reset_mock()
        bot.ai.chat.side_effect = AssertionError("main agent should not participate in delivery")
        bot._is_ws_connected = lambda: True
        coordinator.retry_after.clear()
        await coordinator.poll()
        await drain_runtime(bot)
        self.assertEqual(bot.send_action.await_count, 2)
        self.assertEqual(bot.send_group_message.await_args.args[1].body, "任务完成")
        bot.ai.chat.assert_not_awaited()
        self.assertEqual(coordinator.broker.record(records[0]["id"])["state"], "delivered")

    async def test_cancel_during_file_submission_keeps_unknown_receipt_and_suppresses_notice(self):
        bot, key = self.fixture.bot(stored_cid="main-original")
        bot._is_ws_connected = lambda: True
        self.agent(bot, key)
        uploading = asyncio.Event()
        async def send(action, params, **options):
            uploading.set()
            await asyncio.Event().wait()
        bot.send_action.side_effect = send
        await self.fixture.receive(bot, False, "source-task", "开始制作资料")
        await asyncio.wait_for(uploading.wait(), 2)
        coordinator = bot._get_main_coordinator()
        records = coordinator.broker.completed(coordinator.namespace, bot._image_scope("12345678"))
        context_file = next(iter(coordinator.requests))
        execute("cancel", context_file, request_id=records[0]["id"])
        await coordinator.poll()
        await asyncio.gather(*list(coordinator.delivery_tasks.values()), return_exceptions=True)
        await drain_runtime(bot)
        bot.send_group_message.assert_not_awaited()
        self.assertEqual(bot.send_action.await_count, 1)
        stored = coordinator.broker.record(records[0]["id"])
        result = json.loads(stored["result"])["result"]
        self.assertEqual(stored["state"], "unknown")
        self.assertIn("unknown", bot.media.store.sends(result["job_id"]).values())
        bot.ai.chat.side_effect = None
        bot.ai.chat.return_value = "后续消息仍可正常回复"
        bot._queue_event({"post_type": "message", "message_type": "group", "group_id": 12345678,
            "user_id": 789, "message_id": "after-cancel", "raw_message": "[CQ:at,qq=999] 继续聊天",
            "sender": {"nickname": "Fixture user"}})
        await drain_runtime(bot)
        bot.send_group_message.assert_awaited_once()
        self.assertEqual(bot.send_group_message.await_args.args[1].body, "后续消息仍可正常回复")

    async def test_unknown_send_never_calls_main_or_sends_a_completion(self):
        bot, key = self.fixture.bot(stored_cid="main-original")
        bot._is_ws_connected = lambda: True
        self.agent(bot, key)
        bot.send_action.side_effect = SendUnknown("transport receipt lost")
        await self.fixture.receive(bot, False, "source-task", "开始制作资料")
        await drain_runtime(bot)
        bot.send_group_message.assert_not_awaited()
        self.assertEqual(bot.send_action.await_count, 1)
        self.assertEqual(bot.ai.chat.await_count, 3)
        coordinator = bot._get_main_coordinator()
        await coordinator.poll()
        await drain_runtime(bot)
        self.assertEqual(bot.send_action.await_count, 1)

    async def test_official_completion_waits_for_renewed_window_after_five_files(self):
        bot, key = self.fixture.bot(True, stored_cid="official-main")
        self.agent(bot, key, count=5)
        bot.app_config.media.official_file_enabled = True
        service = bot._get_media_service()
        service.sender.public_asset = AsyncMock(return_value="https://example.com/prepared")
        bot.api.post_group_file = AsyncMock(return_value={"file_info": "opaque"})
        bot.api.post_group_message = AsyncMock(return_value={"id": "accepted"})
        original = await self.fixture.receive(bot, True, "source-task", "开始制作资料")
        await drain_runtime(bot)
        self.assertEqual(bot.api.post_group_message.await_count, 5)
        original.reply.assert_not_awaited()
        coordinator = bot._get_main_coordinator()
        ready = coordinator.broker.completed(coordinator.namespace, coordinator.contexts[key].scope)
        self.assertEqual(len(ready), 1)
        coordinator.retry_after.clear()
        fresh = await self.fixture.receive(bot, True, "fresh", "领取结果", elapsed=1)
        await drain_runtime(bot)
        self.assertEqual(bot.api.post_group_message.await_count, 5)  # no duplicate file
        texts = [call.kwargs.get("content") if not call.args else call.args[0] for call in fresh.reply.await_args_list]
        self.assertCountEqual(texts, ["可以继续聊天", "任务完成"])
        self.assertEqual(len({call.kwargs["msg_seq"] for call in fresh.reply.await_args_list}), 2)
        self.assertEqual(coordinator.broker.record(ready[0]["id"])["state"], "delivered")


class AgentUnlimitedWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_chat_timeout_cannot_limit_main_or_task_rounds(self):
        with tempfile.TemporaryDirectory(prefix="qqbot_unlimited_round_") as tmp:
            ai = AntigravityClient(sessions_file=str(Path(tmp) / "sessions.json"), timeout_seconds=.001)
            ai._is_ready = True
            ai._executable = "fixture-agentapi.exe"
            ai._cancel_native_conversation = AsyncMock()
            async def delayed_response(*args, **options):
                await asyncio.sleep(.025)
                return "完成"
            ai._wait_for_model_response = AsyncMock(side_effect=delayed_response)
            processes = []
            async def spawn(*args, **options):
                proc = MagicMock(returncode=0)
                async def communicate():
                    await asyncio.sleep(.025)
                    return json.dumps({"response": {"newConversation": {"conversationId": "fixture-new-cid"}}}).encode(), b""
                proc.communicate = AsyncMock(side_effect=communicate)
                proc.wait = AsyncMock(return_value=0)
                processes.append(proc)
                return proc
            with patch("asyncio.create_subprocess_exec", side_effect=spawn):
                for key in ("onebot_group_123", "group_456", "c2c_789", "__work__:task:long"):
                    self.assertEqual(await ai.chat(key, "执行任务", raise_on_error=True), "完成")
                    self.assertEqual(ai._wait_for_model_response.await_args.kwargs["timeout"], float("inf"))
            ai._cancel_native_conversation.assert_not_awaited()
            self.assertTrue(all(not proc.kill.called for proc in processes))


if __name__ == "__main__":
    unittest.main()
