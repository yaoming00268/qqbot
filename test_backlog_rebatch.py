"""Backlog interruption preserves inputs, transport receipts and native work."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from config import AppConfig
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.files import ConsoleFileError
from conversation_runtime import ConversationRuntime, EventContext, RuntimeStore, current_event
from test_ai_fixture import drain_runtime, prompt_object


class BacklogQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(str(Path(self.temp.name) / "runtime.sqlite3"), "test")
        self.runtime = ConversationRuntime(self.store, settle_seconds=0, backlog_interrupt_messages=3)
        self.stamp = 100

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.db.close()
        self.temp.cleanup()

    def event(self, mid, *, scope="g", persona="p", user="u"):
        self.stamp += 1
        return EventContext(scope, mid, self.stamp, owner=scope + ":" + user,
                            text="原话 " + mid, persona=persona, sender_name=user)

    async def test_rebatch_counts_original_fragments_and_preserves_all_sources(self):
        entered, release = asyncio.Event(), asyncio.Event()
        batches, prepared = [], []
        original = self.event("original")
        async def first():
            original.reply_generating = True
            entered.set()
            await release.wait()
            self.assertTrue(self.runtime.request_rebatch(original))
            original.interrupted_replies.append({"source_message_id": "original", "messages": []})
        async def next_turn():
            event = current_event.get()
            batches.append(event)
        self.runtime.submit_chat(original, first, coalesce=True)
        await entered.wait()
        for mid in ("one", "two", "three"):
            self.runtime.submit_chat(self.event(mid), next_turn, coalesce=True,
                                     record_only=lambda mid=mid: prepared.append(mid))
        self.assertEqual(len(self.runtime.inboxes["g"]), 1)
        release.set()
        await self.runtime.chat_workers["g"]
        self.assertEqual(len(batches), 1)
        self.assertEqual([row["id"] for row in batches[0].batch], ["original", "one", "two", "three"])
        self.assertEqual(prepared, ["one", "two"])
        self.assertEqual(batches[0].interrupted_replies, original.interrupted_replies)
        self.assertTrue(all(row["received_at"] > 0 for row in batches[0].batch))

    async def test_other_scope_persona_and_command_boundary_cannot_trigger(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.event("original")
        async def first():
            original.reply_generating = True
            entered.set()
            await release.wait()
        self.runtime.submit_chat(original, first, batchable=True)
        await entered.wait()
        async def nothing():
            pass
        for index in range(3):
            self.runtime.submit_chat(self.event(str(index), scope="other"), nothing, batchable=True)
        self.assertFalse(self.runtime.request_rebatch(original))
        self.runtime.submit_chat(self.event("command"), nothing, batchable=False)
        for index in range(3):
            self.runtime.submit_chat(self.event("pending" + str(index)), nothing, batchable=True)
        self.assertFalse(self.runtime.request_rebatch(original))
        self.runtime.inboxes["g"].popleft()
        self.runtime.inboxes["g"][0].event.persona = "other-persona"
        self.assertFalse(self.runtime.request_rebatch(original))
        release.set()
        await self.runtime.chat_workers["g"]

    async def test_disabled_threshold_and_duplicate_ids_do_not_interrupt(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.event("original")
        async def first():
            original.reply_generating = True
            entered.set()
            await release.wait()
        async def nothing():
            pass
        self.runtime.submit_chat(original, first, batchable=True)
        await entered.wait()
        for mid in ("one", "one", "two"):
            self.runtime.submit_chat(self.event(mid), nothing, batchable=True)
        self.assertFalse(self.runtime.request_rebatch(original))
        self.runtime.submit_chat(self.event("three"), nothing, batchable=True)
        self.runtime.backlog_interrupt_messages = 0
        self.assertFalse(self.runtime.request_rebatch(original))
        release.set()
        await self.runtime.chat_workers["g"]

    async def test_repeated_interruptions_keep_original_inputs_and_previous_receipts(self):
        history = []
        async def handle():
            event = current_event.get()
            history.append(event)
            if len(history) == 3:
                return
            event.reply_generating = True
            for index in range(3):
                self.runtime.submit_chat(self.event(f"round{len(history)}-{index}"), handle, coalesce=True)
            self.assertTrue(self.runtime.request_rebatch(event))
            event.interrupted_replies.append({"source_message_id": event.message_id,
                "messages": [{"state": "accepted", "text": f"已发送{len(history)}"}]})
        self.runtime.submit_chat(self.event("original"), handle, coalesce=True)
        await asyncio.wait_for(self.runtime.chat_workers["g"], 2)
        self.assertEqual(len(history), 3)
        self.assertEqual([row["id"] for row in history[-1].batch],
                         ["original", "round1-0", "round1-1", "round1-2", "round2-0", "round2-1", "round2-2"])
        self.assertEqual([row["messages"][0]["text"] for row in history[-1].interrupted_replies],
                         ["已发送1", "已发送2"])

    def test_config_validates_and_preserves_legacy_defaults(self):
        self.assertEqual(AppConfig().bot.backlog_interrupt_messages, 3)
        for value in (-1, 101):
            with self.assertRaises(ValueError):
                AppConfig(bot={"backlog_interrupt_messages": value})


class BacklogIngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def message(self, mid, text, official, private=False, user="456", group="12345678"):
        if official:
            return SimpleNamespace(id=mid, content=text, group_openid=group,
                author=SimpleNamespace(member_openid=user, user_openid=user, username="用户" + user, bot=False),
                mentions=[], attachments=[], message_reference=None,
                reply=AsyncMock(return_value={"id": "reply-" + mid}))
        return {"post_type": "message", "message_type": "private" if private else "group",
                **({} if private else {"group_id": int(group)}), "user_id": int(user),
                "message_id": mid, "raw_message": text, "sender": {"nickname": "用户" + user}}

    async def queue(self, bot, message, official, private=False):
        if not official:
            bot._queue_event(message)
        elif private:
            await bot.on_c2c_message_create(message)
        else:
            bot._queue_official_group(message, True)

    async def cycle(self, official=False, private=False, *, partial=False, fixed=False):
        bot, group_key = self.fixture.bot(official, stored_cid="backlog-cid")
        bot.app_config.bot.chat_buffer_seconds = .025 if fixed else 0
        if private:
            key = bot._chat_session_key(("c2c_" if official else "onebot_private_") + "456")
            bot.ai.get_or_create_session(key).conversation_id = "private-cid"
        else:
            key = group_key
        entered, interrupted = asyncio.Event(), asyncio.Event()
        prompts, contexts = [], []
        async def model(session_key, prompt, **options):
            prompts.append(prompt)
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            contexts.append(context)
            if len(prompts) == 1:
                if partial:
                    execute("reply", context, text="已经发送的前半段")
                entered.set()
                while options["stop_requested"]() != "rebatched":
                    await asyncio.sleep(.005)
                interrupted.set()
                with self.assertRaises(ConsoleFileError):
                    execute("reply", context, text="中断后不应发送的旧内容", finish=True)
                return "旧的最终文本不能发送"
            execute("reply", context, text="结合补充后的答复", finish=True)
            return ""
        bot.ai.chat = AsyncMock(side_effect=model)
        first = self.message("original", "原来的问题", official, private)
        await self.queue(bot, first, official, private)
        await asyncio.wait_for(entered.wait(), 3)
        if partial:
            for _ in range(150):
                broker, token = ConsoleBroker.from_context(contexts[0])
                with broker.connect() as db:
                    states = [row[0] for row in db.execute("SELECT state FROM outgoing WHERE token=?", (token,))]
                if "accepted" in states:
                    break
                await asyncio.sleep(.005)
            self.assertIn("accepted", states)
        messages = []
        for index in range(1, 4):
            mid, text = "new" + str(index), "补充内容" + str(index)
            message = self.message(mid, text, official, private, user="456" if private or index != 2 else "789")
            messages.append(message)
            await self.queue(bot, message, official, private)
            if index == 2:
                await asyncio.sleep(.06)
                self.assertFalse(interrupted.is_set())
        await asyncio.wait_for(interrupted.wait(), 3)
        await drain_runtime(bot)
        self.assertEqual(len(prompts), 2)
        turn = prompt_object(prompts[-1], "turn")["turn"]
        self.assertEqual([row["id"] for row in turn["messages"]], ["original", "new1", "new2"])
        self.assertTrue(all(row["received_at"] for row in turn["messages"]))
        self.assertEqual(turn["source_message_id"], "new3")
        self.assertIn("由你判断", turn["instruction"])
        sent = turn["interrupted_replies"][0]["messages"]
        self.assertEqual([row["text"] for row in sent], ["已经发送的前半段"] if partial else [])
        if partial:
            self.assertEqual(sent[0]["state"], "accepted")
        broker, token = ConsoleBroker.from_context(contexts[-1])
        self.assertEqual([row["id"] for row in broker.context(token)["reply_sources"]],
                         ["original", "new1", "new2", "new3"])
        self.assertEqual(bot.ai.get_or_create_session(key).generation, 0)
        if official:
            messages[-1].reply.assert_awaited_once()
        return bot

    async def test_onebot_group(self):
        await self.cycle()

    async def test_official_group(self):
        await self.cycle(official=True)

    async def test_onebot_private(self):
        await self.cycle(private=True)

    async def test_official_private(self):
        await self.cycle(official=True, private=True)

    async def test_fixed_window_skipped_after_interruption(self):
        await self.cycle(fixed=True)

    async def test_onebot_partial_reply_is_given_to_next_turn(self):
        await self.cycle(partial=True)

    async def test_official_partial_reply_is_given_to_next_turn(self):
        await self.cycle(official=True, partial=True)

    async def bubble_cycle(self, official, *, chunks_before_interrupt=1):
        from onebot_client import OneBotClient
        bot, _ = self.fixture.bot(official, stored_cid="bubble-cid")
        bot.app_config.bot.max_reply_length = 6
        first_send, release_send = asyncio.Event(), asyncio.Event()
        sent, prompts = [], []
        async def accept(text):
            sent.append(text)
            if len(sent) == chunks_before_interrupt:
                first_send.set()
                await release_send.wait()
            return {"status": "ok", "retcode": 0, "id": str(len(sent)), "data": {"message_id": str(len(sent))}}
        first = self.message("old", "一个问题", official)
        if official:
            async def official_send(**kwargs):
                return await accept(kwargs["content"])
            first.reply.side_effect = official_send
        else:
            bot._is_ws_connected = lambda: True
            bot.send_group_message = OneBotClient.send_group_message.__get__(bot)
            async def onebot_send(action, params):
                return await accept(params["message"][-1]["data"]["text"])
            bot.send_action.side_effect = onebot_send
        async def model(key, prompt, **options):
            prompts.append(prompt)
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            if len(prompts) == 1:
                execute("reply", context, text="第一段。\n\n第二段。\n\n不再发送的第三段。")
                while options["stop_requested"]() != "rebatched":
                    await asyncio.sleep(.005)
            else:
                execute("reply", context, text="根据补充回复", finish=True)
            return ""
        bot.ai.chat = AsyncMock(side_effect=model)
        await self.queue(bot, first, official)
        await asyncio.wait_for(first_send.wait(), 3)
        for index in range(3):
            await self.queue(bot, self.message("extra" + str(index), "补充" + str(index), official), official)
        release_send.set()
        await asyncio.wait_for(drain_runtime(bot), 4)
        old_segments = ["第一段。\n\n", "第二段。\n\n"][:chunks_before_interrupt]
        self.assertEqual(sent, old_segments if official else [*old_segments, "根据补充回复"])
        report = prompt_object(prompts[-1], "turn")["turn"]["interrupted_replies"]
        self.assertEqual([message["text"] for message in report[0]["messages"]], ["".join(old_segments)])

    async def test_onebot_interrupts_between_bubbles(self):
        await self.bubble_cycle(False)

    async def test_official_interrupts_between_bubbles(self):
        await self.bubble_cycle(True)

    async def test_partial_transcript_preserves_multiple_bubbles(self):
        for official in (False, True):
            with self.subTest(official=official):
                await self.bubble_cycle(official, chunks_before_interrupt=2)

    async def test_native_interrupt_keeps_conversation_and_background_work(self):
        from antigravity_client import AntigravityClient
        from test_explicit_turn_finish import inbound
        bot, key = self.fixture.bot(stored_cid="backlog-native")
        bot.ai.chat = AntigravityClient.chat.__get__(bot.ai)
        bot.ai._is_ready = True
        bot.ai._brain_dir = str(Path("backlog-brain").resolve())
        bot.ai._cancel_native_conversation = AsyncMock()
        bot.ai._ensure_conversation_present = AsyncMock()
        path = Path(bot.ai._brain_dir) / "backlog-native/.system_generated/logs/transcript.jsonl"
        path.parent.mkdir(parents=True)
        entered = asyncio.Event()
        calls = 0
        async def command(args, **options):
            nonlocal calls
            calls += 1
            context = prompt_object(args[-1], "console_context_file")["console_context_file"]
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(inbound(calls * 10, args[-1].splitlines()[0])) + "\n")
            if calls == 1:
                entered.set()
                await asyncio.Event().wait()
            execute("reply", context, text="原生模型合并后回复", finish=True)
            return {}
        bot.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        try:
            await self.queue(bot, self.message("native", "原生模型的问题", False), False)
            await asyncio.wait_for(entered.wait(), 3)
            for index in range(3):
                await self.queue(bot, self.message("later" + str(index), "继续补充" + str(index), False), False)
            await asyncio.wait_for(drain_runtime(bot), 5)
            self.assertEqual(calls, 2)
            self.assertTrue(bot.ai._cancel_native_conversation.await_args_list)
            for call in bot.ai._cancel_native_conversation.await_args_list:
                self.assertEqual(call.args, ("backlog-native",))
                self.assertFalse(call.kwargs["kill_background_tasks"])
            self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "backlog-native")
            bot.send_group_message.assert_awaited_once()
        finally:
            await asyncio.get_running_loop().shutdown_default_executor()
