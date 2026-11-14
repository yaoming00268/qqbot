"""Offline races between media work, new input and multi-part replies."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from conversation_runtime import EventContext, current_event
from onebot_client import OneBotClient
from test_ai_fixture import prompt_object


class ReplyQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_turns_and_independent_conversations(self):
        from reply_queue import ReplyQueue
        queue, order = ReplyQueue(), []
        async def reply(number, key="a"):
            async with queue.turn(key):
                order.append(number)
                await asyncio.sleep(0)
        async with queue.turn("a"):
            tasks = []
            for index in range(4):
                tasks.append(asyncio.create_task(reply(index)))
                await asyncio.sleep(0)
            await asyncio.wait_for(reply("other", "b"), 1)
            self.assertEqual(order, ["other"])
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        self.assertEqual(order, ["other", 0, 1, 2, 3])
        self.assertFalse(queue._slots)

    async def test_nested_turn_is_reentrant_but_child_task_still_waits(self):
        from reply_queue import ReplyQueue
        queue, entered = ReplyQueue(), asyncio.Event()
        async def child():
            async with queue.turn("a"):
                entered.set()
        async with queue.turn("a"):
            async with queue.turn("a"):
                task = asyncio.create_task(child())
                await asyncio.sleep(0)
                self.assertFalse(entered.is_set())
            self.assertFalse(entered.is_set())
        await asyncio.wait_for(task, 1)
        self.assertTrue(entered.is_set())
        self.assertFalse(queue._slots)

    async def test_cancelled_waiter_does_not_block_later_reply(self):
        from reply_queue import ReplyQueue
        queue, order = ReplyQueue(), []
        async def reply(number):
            async with queue.turn("a"):
                order.append(number)
        async with queue.turn("a"):
            abandoned = asyncio.create_task(reply(1))
            await asyncio.sleep(0)
            following = asyncio.create_task(reply(2))
            await asyncio.sleep(0)
            abandoned.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await abandoned
        await asyncio.wait_for(following, 1)
        self.assertEqual(order, [2])
        self.assertFalse(queue._slots)

    async def test_failed_or_cancelled_owner_releases_turn(self):
        from reply_queue import ReplyQueue
        queue, entered = ReplyQueue(), asyncio.Event()
        async def owner():
            async with queue.turn("a"):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(owner())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(ValueError):
            async with queue.turn("a"):
                raise ValueError("transport failed")
        async with queue.turn("a"):
            pass
        self.assertFalse(queue._slots)


class ReplyQueueIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def bot(self, official=False):
        bot, key = self.fixture.bot(official, stored_cid="existing-main")
        if not official:
            bot._is_ws_connected = lambda: True
            bot.send_group_message = OneBotClient.send_group_message.__get__(bot)
        return bot, key

    def message(self, mid, text, official=False, group="12345678", user="456"):
        if official:
            return SimpleNamespace(id=mid, group_openid=group, content=text,
                author=SimpleNamespace(member_openid=user, username="Fixture user", bot=False),
                mentions=[], attachments=None, message_reference=None,
                reply=AsyncMock(return_value={"id": "receipt_" + mid}))
        return {"post_type": "message", "message_type": "group", "group_id": int(group),
                "user_id": int(user), "message_id": mid, "raw_message": text,
                "sender": {"nickname": "Fixture user"}}

    async def process(self, bot, message, official=False, *, lane="chat"):
        group = message.group_openid if official else str(message["group_id"])
        mid = message.id if official else message["message_id"]
        token = current_event.set(EventContext(bot._image_scope(group), mid, owner="456", lane=lane))
        try:
            if official:
                await bot._handle_group_message(message, True)
            else:
                await bot._handle_group_message(message)
        finally:
            current_event.reset(token)

    async def test_active_analysis_keeps_reply_when_same_sender_adds_message(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, _ = self.bot(official)
                entered, release = asyncio.Event(), asyncio.Event()
                async def model(key, prompt, **options):
                    entered.set()
                    await release.wait()
                    return "视频分析第一段。\n\n视频分析第二段。"
                bot.ai.chat.side_effect = model
                first = self.message("analysis", "请完整分析视频的内容", official)
                scope = bot._image_scope("12345678")
                bot._chat_turns.observe(scope, "analysis", "456", "请完整分析视频的内容")
                task = asyncio.create_task(self.process(bot, first, official))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    bot._chat_turns.observe(scope, "followup", "456", "另外还想问一件事")
                finally:
                    release.set()
                    await asyncio.wait_for(task, 3)
                if official:
                    parts = [call.kwargs["content"] for call in first.reply.await_args_list]
                else:
                    parts = [call.args[1]["message"][-1]["data"]["text"]
                             for call in bot.send_action.await_args_list if call.args[0] == "send_group_msg"]
                self.assertEqual(parts, ["视频分析第一段。", "视频分析第二段。"])

    async def test_background_reply_finishes_all_bubbles_before_new_chat_starts(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, _ = self.bot(official)
                first_send, release, second_model, second_prepared = (asyncio.Event() for _ in range(4))
                sent = []
                original_context = bot._image_context_for_chat
                async def prepare_context(*args, **kwargs):
                    result = await original_context(*args, **kwargs)
                    if current_event.get().message_id == "new":
                        second_prepared.set()
                    return result
                bot._image_context_for_chat = prepare_context
                async def accept(text):
                    sent.append(text)
                    if len(sent) == 1:
                        first_send.set()
                        await release.wait()
                    return {"status": "ok", "retcode": 0, "data": {"message_id": str(len(sent))}}
                first = self.message("media", "请分析这张图片", official)
                second = self.message("new", "[CQ:at,qq=999] 再回答一个问题", official, user="789")
                if official:
                    async def official_send(*, content, **kw):
                        return await accept(content)
                    first.reply.side_effect = second.reply.side_effect = official_send
                else:
                    async def onebot_send(action, params):
                        return await accept(params["message"][-1]["data"]["text"])
                    bot.send_action.side_effect = onebot_send
                model_calls = 0
                async def model(key, prompt, **options):
                    nonlocal model_calls
                    model_calls += 1
                    if model_calls == 1:
                        return "图片回复第一段。\n\n图片回复第二段。\n\n图片回复第三段。"
                    second_model.set()
                    return "新消息的回复。"
                bot.ai.chat.side_effect = model
                initial = asyncio.create_task(self.process(bot, first, official, lane="work"))
                other = None
                try:
                    await asyncio.wait_for(first_send.wait(), 2)
                    other = asyncio.create_task(self.process(bot, second, official))
                    await asyncio.wait_for(second_prepared.wait(), 2)
                    await asyncio.sleep(.05)
                    self.assertFalse(second_model.is_set(), "new input entered the model before the active reply finished")
                finally:
                    release.set()
                    await asyncio.wait_for(asyncio.gather(initial, *([other] if other else [])), 3)
                self.assertEqual(sent, ["图片回复第一段。", "图片回复第二段。", "图片回复第三段。", "新消息的回复。"])

    async def test_reset_invalidates_reply_waiting_in_queue(self):
        from antigravity_client import SessionInvalidatedError
        bot, key = self.bot()
        ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
                                      message_id="old", text="old", is_group=True)
        entered = asyncio.Event()
        async def waiting():
            entered.set()
            async with bot._reply_turn(key, ctx):
                self.fail("stale reply was admitted")
        async with bot._reply_turn(key):
            task = asyncio.create_task(waiting())
            await entered.wait()
            await asyncio.sleep(0)
            bot.ai.reset_session(key)
        with self.assertRaises(SessionInvalidatedError):
            await asyncio.wait_for(task, 2)
        bot.send_action.assert_not_awaited()

    async def test_foreground_input_interrupts_silent_housekeeping_before_queue_wait(self):
        bot, key = self.bot()
        console, entered = bot._get_main_coordinator(), asyncio.Event()
        async def thought():
            async with console.replies.turn(key):
                entered.set()
                await asyncio.Event().wait()
        task = console.self_activity_tasks[key] = asyncio.create_task(thought())
        await entered.wait()
        bot.ai.chat.return_value = "回来继续聊。"
        await asyncio.wait_for(self.process(bot, self.message("foreground", "[CQ:at,qq=999] 回来啦")), 2)
        self.assertTrue(task.cancelled())
        self.assertEqual(bot.send_action.await_count, 1)

    async def test_video_result_delivery_finishes_before_new_reply(self):
        from console_tools.cli import execute
        bot, key = self.bot()
        console = bot._get_main_coordinator()
        ctx = bot._make_media_context(session_key=key, user_id="456", conversation_id="12345678",
            message_id="video", text="分析视频", is_group=True)
        _, token, metadata = console.issue_chat_context(ctx, key)
        request = console.broker._enqueue(token, metadata, "process", "分析视频", {})
        console.broker.update(request["id"], "ready", {"success": True,
            "result": {"answer": "完整视频分析", "files": []}})
        notice = "视频分析的完整正文。" * 40
        bot.app_config.bot.max_reply_length = 90
        first_send, release, second_prepared = (asyncio.Event() for _ in range(3))
        sent, model_calls = [], []
        original_context = bot._image_context_for_chat
        async def prepare_context(*args, **kwargs):
            result = await original_context(*args, **kwargs)
            second_prepared.set()
            return result
        bot._image_context_for_chat = prepare_context
        async def send(action, params):
            sent.append(params["message"][-1]["data"]["text"])
            if len(sent) == 1:
                first_send.set()
                await release.wait()
            return {"status": "ok", "retcode": 0, "data": {"message_id": str(len(sent))}}
        bot.send_action.side_effect = send
        async def model(key, prompt, **options):
            model_calls.append(prompt)
            if '"type": "task_result"' in prompt:
                binding = prompt_object(prompt, "console_context_file")
                execute("deliver", binding["console_context_file"], request_id=request["id"], text=notice, finish=True)
                return ""
            return "新消息的回复。"
        bot.ai.chat.side_effect = model
        delivery = console._queue_result(console.broker.record(request["id"]), ctx, metadata)
        other = None
        try:
            await asyncio.wait_for(first_send.wait(), 3)
            other = asyncio.create_task(self.process(bot, self.message("later", "[CQ:at,qq=999] 继续聊", user="789")))
            await asyncio.wait_for(second_prepared.wait(), 2)
            await asyncio.sleep(.05)
            self.assertEqual(len(model_calls), 1)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(delivery, *([other] if other else [])), 4)
        self.assertEqual("".join(sent[:-1]), notice)
        self.assertEqual(sent[-1], "新消息的回复。")
        self.assertEqual(console.broker.record(request["id"])["state"], "delivered")


if __name__ == "__main__":
    unittest.main()
