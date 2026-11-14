"""Immediate committed speech, short events and ordered acknowledgement."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.server import handle
from console_tools.files import ConsoleFileError
from image_context import ImageContextStore
import test_startup_participation as fixtures
from test_ai_fixture import prompt_object, ConsoleAgentMock, drain_runtime


class StreamingBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.context = self.broker.issue_context({"namespace": "onebot:a", "scope": "onebot:a:group:g",
            "session_key": "key", "sender_id": "u", "main_agent": True, "is_group": True,
            "reply_sources": [{"id": "q", "sender_id": "u", "text": "请求"}]}, self.root / "contexts")
        _, self.token = ConsoleBroker.from_context(self.context)

    def test_multiple_committed_batches_and_drafts_are_distinct_and_retries_safe(self):
        first = execute("reply", self.context, text="正在准备。")
        self.broker.message_state(first["message_ids"][0], "accepted")
        draft = execute("message", self.context, text="结果还没提交")
        self.assertEqual(self.broker.committed_messages(self.token), [])
        with self.assertRaises(ConsoleFileError):
            execute("finish", self.context)
        second = execute("commit", self.context)
        self.assertEqual(second["message_ids"], [draft["id"]])
        self.assertEqual(execute("reply", self.context, text="正在准备。"), first)
        self.assertEqual(execute("commit", self.context), second)
        self.broker.message_state(draft["id"], "accepted")
        third = execute("reply", self.context, text="另一条已核实的结果。")
        self.assertEqual(len(self.broker.committed_messages(self.token)), 1)
        terminal = execute("finish", self.context)
        self.assertEqual(terminal["message_ids"], first["message_ids"] + second["message_ids"] + third["message_ids"])
        with self.assertRaises(ConsoleFileError):
            execute("reply", self.context, text="晚到正文")

    def test_shortcut_defaults_to_explicit_finish_and_continue_keeps_tools_open(self):
        result = handle({"id": 1, "method": "tools/call", "params": {"name": "qqbot_reply",
            "arguments": {"context_file": self.context, "text": "一句正常回复"}}})["result"]
        self.assertFalse(result["isError"])
        receipt = json.loads(result["content"][0]["text"])
        self.assertEqual(receipt["state"], "finished")
        self.assertEqual(self.broker.reply_control(self.token)["mode"], "finished")
        second = self.broker.issue_context(self.broker.context(self.token), self.root / "contexts")
        result = handle({"id": 2, "method": "tools/call", "params": {"name": "qqbot_reply",
            "arguments": {"context_file": second, "text": "我先准备。", "continue_work": True}}})["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["state"], "committed")

    def test_failed_turn_preserves_only_committed_speech_and_cancellation_blocks_replay(self):
        first = execute("reply", self.context, text="已确定的回复", request_key="first")
        draft = execute("message", self.context, text="尚未确定的草稿")
        self.assertIsNone(self.broker.seal_messages(self.token, cancel=True, preserve_committed=True))
        sent = self.broker.seal_messages(self.token)
        self.assertEqual([row["text"] for row in sent], ["已确定的回复"])
        self.assertEqual(self.broker.message_status(self.token, draft["id"])["state"], "cancelled")
        self.assertEqual(execute("reply", self.context, text="已确定的回复", request_key="first"), first)
        self.broker.seal_messages(self.token, cancel=True)
        with self.assertRaises(ConsoleFileError):
            execute("reply", self.context, text="已确定的回复", request_key="first")

    def test_conflicting_key_does_not_create_a_second_batch(self):
        first = execute("reply", self.context, text="确定的原文", request_key="same")
        with self.assertRaises(ConsoleFileError) as caught:
            execute("reply", self.context, text="不同的原文", request_key="same")
        self.assertEqual(caught.exception.code, "REQUEST_KEY_CONFLICT")
        self.assertEqual([row["command_id"] for row in self.broker.committed_messages(self.token)], first["message_ids"])

    def test_a_new_reply_does_not_commit_an_unrelated_draft(self):
        draft = execute("message", self.context, text="未确定的草稿")
        reply = execute("reply", self.context, text="确定的台词")
        self.assertNotIn(draft["id"], reply["message_ids"])
        self.assertEqual([row["text"] for row in self.broker.committed_messages(self.token)], ["确定的台词"])
        self.assertEqual([row["text"] for row in self.broker.seal_messages(self.token)], ["确定的台词"])


class StreamingMainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_reply_is_received_while_model_is_still_working_before_finish_both_transports(self):
        for official in (False, True):
            bot, key = self.fixture.bot(official, stored_cid="stream-cid")
            coordinator = bot._get_main_coordinator()
            entered, release = asyncio.Event(), asyncio.Event()
            context_file = []
            async def model(session_key, prompt, **options):
                context = prompt_object(prompt, "console_context_file")["console_context_file"]
                context_file.append(context)
                execute("reply", context, text="正在准备这件事。")
                entered.set()
                await release.wait()
                execute("contacts", context, operation="upsert", params={"user_id": "456", "revision": 0,
                    "reason": "对方告知", "preferred_name": "阿林"})
                execute("reply", context, text="记录和准备都完成了。")
                execute("finish", context)
                return "不应额外发送"
            bot.ai.chat = AsyncMock(side_effect=model)
            with patch.object(coordinator, "start"), patch.object(coordinator, "poll", new=AsyncMock()):
                turn = asyncio.create_task(self.fixture.receive(bot, official, "q", "帮我准备并记录一下"))
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    # Bounded local dispatch: no model final or finish is needed.
                    for _ in range(100):
                        ctx = coordinator.requests[context_file[0]]
                        rows = coordinator.broker.messages(ctx.console_token)
                        with coordinator.broker.connect() as db:
                            accepted = db.execute("SELECT 1 FROM outgoing WHERE token=? AND state='accepted'", (ctx.console_token,)).fetchone()
                        if accepted:
                            break
                        await asyncio.sleep(.01)
                    self.assertTrue(accepted)
                    self.assertFalse(turn.done())
                    self.assertEqual(coordinator.broker.reply_control(ctx.console_token)["mode"], "committed")
                    release.set()
                    message = await asyncio.wait_for(turn, 4)
                    texts = ([call.kwargs["content"] for call in message.reply.await_args_list] if official else
                             [str(call.args[1]) for call in bot.send_group_message.await_args_list])
                    self.assertEqual(texts, ["正在准备这件事。", "记录和准备都完成了。"])
                    self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "stream-cid")
                    self.assertEqual(bot.ai.get_or_create_session(key).chat_state["last_delivery"]["state"], "sent")
                finally:
                    release.set()
                    if not turn.done():
                        turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)

    async def test_work_starts_after_ack_transport_receipt_not_after_finish(self):
        bot, key = self.fixture.bot(stored_cid="ack-cid")
        coordinator = bot._get_main_coordinator()
        sending, receipt, work_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        finish_model = asyncio.Event()
        order = []
        async def send(*args, **kwargs):
            order.append("ack-start")
            sending.set()
            await receipt.wait()
            order.append("ack-receipt")
            return {"status": "ok", "data": {"message_id": "sent"}}
        bot.send_group_message = AsyncMock(side_effect=send)
        async def work(*args, **kwargs):
            order.append("work")
            work_started.set()
            from skills.base import SkillResult
            return SkillResult(True, "完成", data={"files": []})
        async def model(session_key, prompt, **options):
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            execute("reply", context, text="我在准备了。")
            execute("submit", context, task="整理资料")
            await finish_model.wait()
            execute("finish", context)
            return ""
        bot.ai.chat = AsyncMock(side_effect=model)
        with patch.object(coordinator, "start"), patch.object(coordinator, "_queue_result"), \
             patch("skills.task_skills.RunTaskSkill.execute", new=AsyncMock(side_effect=work)):
            turn = asyncio.create_task(self.fixture.receive(bot, False, "q", "整理一份资料"))
            try:
                await asyncio.wait_for(sending.wait(), 3)
                await coordinator.poll()
                self.assertFalse(work_started.is_set())
                receipt.set()
                await asyncio.wait_for(work_started.wait(), 3)
                self.assertFalse(turn.done())
                self.assertEqual(order, ["ack-start", "ack-receipt", "work"])
                finish_model.set()
                await asyncio.wait_for(turn, 4)
            finally:
                receipt.set()
                finish_model.set()
                if not turn.done():
                    turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)

    async def test_approved_result_is_delivered_while_main_continues_before_finish(self):
        bot, key = self.fixture.bot(stored_cid="stream-result")
        main = bot._get_main_coordinator()
        delivered, finish = asyncio.Event(), asyncio.Event()
        async def send(*_args, **_kwargs):
            delivered.set()
            return {"status": "ok", "data": {"message_id": "result"}}
        bot.send_group_message = AsyncMock(side_effect=send)
        task_ids = []
        async def model(_key, prompt, **_options):
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            request = execute("command", context, text="/status")
            task_ids.append(request["id"])
            main.broker.update(request["id"], "ready", {"success": True,
                "result": {"answer": "实际命令结果全文。", "files": []}})
            execute("deliver", context, request_id=request["id"], finish=True)
            await finish.wait()
            execute("finish", context)
            return ""
        bot.ai.chat = AsyncMock(side_effect=model)
        with patch.object(main, "start"), patch.object(main, "poll", new=AsyncMock()):
            turn = asyncio.create_task(self.fixture.receive(bot, False, "deliver", "展示已完成的结果"))
            try:
                await asyncio.wait_for(delivered.wait(), 3)
                self.assertFalse(turn.done())
                finish.set()
                await asyncio.wait_for(turn, 3)
                self.assertEqual(main.broker.record(task_ids[0])["state"], "delivered")
                bot.send_group_message.assert_awaited_once()
                self.assertEqual(str(bot.send_group_message.await_args.args[1]), "实际命令结果全文。")
            finally:
                finish.set()
                if not turn.done():
                    turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)

    async def test_daily_input_omits_repeated_protocol_speech_and_unrelated_old_images(self):
        bot, key = self.fixture.bot(stored_cid="compact-cid")
        coordinator = bot._get_main_coordinator()
        store = bot._get_image_context_store()
        await bot._save_image_context("12345678", "old-picture", [{"vision_verified": True,
            "visual_description": "osu 成绩详情。" * 1000}], sender_id="456", timestamp=time.time() - 86400)
        bot.ai.chat = ConsoleAgentMock(return_value="收到。")
        with patch.object(coordinator, "start"), patch.object(coordinator, "poll", new=AsyncMock()):
            await self.fixture.receive(bot, False, "first", "你好")
            await self.fixture.receive(bot, False, "second", "去看想看的轻小说吧", elapsed=1)
        prompt = bot.ai.chat.await_args.args[1]
        self.assertNotIn("old-picture", prompt)
        self.assertNotIn("osu 成绩详情", prompt)
        self.assertNotIn('"speech"', prompt)
        self.assertNotIn("instruction", prompt)
        self.assertNotIn("@rules:", prompt)
        self.assertLess(len(prompt), 1600)
        self.assertIn('"type": "message"', prompt)
        self.assertIn("qqbot_reply", (Path(coordinator.client.ai.get_or_create_session(key).chat_state["rules_file"])).read_text(encoding="utf-8"))

    async def test_explicit_work_command_acknowledges_before_worker_preparation(self):
        bot, key = self.fixture.bot(stored_cid="command-ack")
        main = bot._get_main_coordinator()
        sending, receipt, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def send(*_args, **_kwargs):
            sending.set()
            await receipt.wait()
            return {"status": "ok", "data": {"message_id": "ack"}}
        async def work(*_args, **_kwargs):
            self.assertTrue(receipt.is_set())
            started.set()
            from skills.base import SkillResult
            return SkillResult(True, "准备完成", data={"files": []})
        bot.send_group_message = AsyncMock(side_effect=send)
        with patch.object(main, "start"), patch.object(main, "_queue_result"), \
             patch("skills.task_skills.RunTaskSkill.execute", new=AsyncMock(side_effect=work)):
            turn = asyncio.create_task(self.fixture.receive(bot, False, "command", "/task 整理一份资料"))
            try:
                await asyncio.wait_for(sending.wait(), 3)
                await main.poll()
                self.assertFalse(started.is_set())
                receipt.set()
                await asyncio.wait_for(turn, 3)
                await asyncio.wait_for(started.wait(), 3)
                bot.ai.chat.assert_not_awaited()
                self.assertIn("准备", str(bot.send_group_message.await_args.args[1]))
            finally:
                receipt.set()
                if not turn.done():
                    turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)

    async def test_early_speech_keeps_the_turn_until_finish_and_the_next_reply_stays_in_order(self):
        bot, key = self.fixture.bot(stored_cid="fifo-stream")
        main = bot._get_main_coordinator()
        first_sent, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def send(*_args, **_kwargs):
            first_sent.set()
            return {"status": "ok", "data": {"message_id": "sent"}}
        bot.send_group_message = AsyncMock(side_effect=send)
        async def model(_key, prompt, **_options):
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            if "先处理第一件事" in prompt:
                execute("reply", context, text="第一件事已收到。")
                await release.wait()
                execute("finish", context)
            else:
                second_started.set()
                execute("reply", context, text="现在处理第二件事。", finish=True)
            return ""
        bot.ai.chat = AsyncMock(side_effect=model)
        async def receive(message_id, text):
            # Concurrent ingress must not overlap process-global time patches:
            # out-of-order context exits can leave a frozen clock in later tests.
            await bot._handle_group_message({"post_type": "message", "message_type": "group", "self_id": 999,
                "group_id": 12345678, "user_id": 456, "message_id": message_id, "raw_message": text,
                "message": [{"type": "text", "data": {"text": text}}],
                "sender": {"nickname": "Fixture user"}, "time": time.time()})
        with patch.object(main, "start"), patch.object(main, "poll", new=AsyncMock()):
            first = asyncio.create_task(receive("first-fifo", "先处理第一件事"))
            second = None
            try:
                await asyncio.wait_for(first_sent.wait(), 3)
                second = asyncio.create_task(receive("second-fifo", "再处理第二件事"))
                await asyncio.sleep(.05)
                self.assertFalse(second_started.is_set())
                self.assertFalse(first.done())
                release.set()
                await asyncio.wait_for(asyncio.gather(first, second), 4)
                self.assertEqual([str(c.args[1]) for c in bot.send_group_message.await_args_list],
                    ["第一件事已收到。", "现在处理第二件事。"])
                self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "fifo-stream")
            finally:
                release.set()
                pending = [task for task in (first, second) if task is not None]
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    async def test_targeted_image_is_short_and_full_fact_is_readable_only_in_bound_context(self):
        bot, key = self.fixture.bot(stored_cid="picture-context")
        main = bot._get_main_coordinator()
        store = bot._get_image_context_store()
        description = "画面上方显示游戏成绩。" * 1000 + "最后一栏：连击数 998。"
        await bot._save_image_context("12345678", "picture", [{"vision_verified": True,
            "visual_description": description, "semantic_meaning": description}], sender_id="456", timestamp=time.time())
        fact = store.get(bot._image_scope("12345678"), "picture")[0]
        outside = Path(self.fixture.temp.name) / "unlisted.json"
        outside.write_text('{"private":"unlisted"}', encoding="utf-8")
        async def model(_key, prompt, **options):
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            self.assertLess(len(prompt), 8000)  # Includes fixed rules only on bootstrap.
            page = execute("read-memory", context, path=fact.artifact_path, view="raw", query="998", max_chars=16000)
            self.assertIn("998", page["text"])
            with self.assertRaises(PermissionError):
                execute("read-memory", context, path=str(outside), view="raw")
            execute("reply", context, text="最后一栏是 998。", finish=True)
            return ""
        with patch.object(main, "start"), patch.object(main, "poll", new=AsyncMock()):
            bot.ai.chat = ConsoleAgentMock(return_value="你好。")
            await self.fixture.receive(bot, False, "bootstrap", "你好")
            bot.ai.chat = AsyncMock(side_effect=model)
            await self.fixture.receive(bot, False, "follow", "那张图最后一栏是什么", elapsed=1)
        self.assertEqual(str(bot.send_group_message.await_args.args[1]), "最后一栏是 998。")

    async def test_current_and_quoted_images_are_bound_without_pulling_in_other_recent_images(self):
        bot, key = self.fixture.bot(stored_cid="two-pictures")
        for message, description in (("current", "本次图片：红猫。"), ("quoted", "引用图片：蓝猫。"), ("unrelated", "无关旧图：成绩单。")):
            await bot._save_image_context("12345678", message, [{"vision_verified": True,
                "visual_description": description, "semantic_meaning": description}], sender_id="456", timestamp=time.time())
        text, (_, versions) = await bot._image_context_for_chat(key, "12345678", "这两个是什么", message_id="quoted", current_message_id="current")
        self.assertIn("本次图片：红猫", text)
        self.assertIn("引用图片：蓝猫", text)
        self.assertNotIn("成绩单", text)
        self.assertNotIn('"meaning"', text)
        self.assertEqual({message for message, _, _ in versions}, {"current", "quoted"})
        self.assertLessEqual(len(text), 3600)
