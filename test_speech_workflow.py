"""Exercise speech holds through real chat ingress and public MCP/CLI."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import json
import unittest
from unittest.mock import AsyncMock

from console_tools.cli import execute
from console_tools.server import handle
from test_ai_fixture import prompt_object


class SpeechWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def private(self, bot, official, mid, text):
        if official:
            message = SimpleNamespace(id=mid, content=text,
                author=SimpleNamespace(user_openid="456", username="Alice"),
                attachments=[], message_reference=None, reply=AsyncMock(return_value={"id": "receipt-" + mid}))
            await bot._process_c2c_message(message)
        else:
            message = {"message_type": "private", "user_id": 456, "message_id": mid,
                "raw_message": text, "sender": {"nickname": "Alice"},
                "message": [{"type": "text", "data": {"text": text}}]}
            await bot._handle_private_message(message)
        return message

    async def check_cycle(self, official, private=False):
        bot, _ = self.fixture.bot(official, stored_cid="speech-cid")
        snapshots, bindings = [], []
        # OneBot private chat puts image context before the message JSON.
        if private:
            original = bot._image_context_for_chat
            async def image_context(*args, **options):
                text, receipt = await original(*args, **options)
                return "[已验证图像资料]\n" + text, receipt
            bot._image_context_for_chat = image_context
        async def model(key, prompt, **options):
            event = prompt_object(prompt, "console_context_file")
            speech = execute("speech", event["console_context_file"], operation="status")
            snapshots.append(speech)
            bindings.append(event["console_context_file"])
            current = speech["current_messages"][-1]["text"]
            if len(snapshots) < 3:
                response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": "qqbot_cli", "arguments": {"action": "speech", "operation": "hold",
                        "context_file": bindings[-1], "params": {"text": "表达尚未完成"}}}})
                self.assertFalse(response["result"].get("isError"), response)
                self.assertEqual(options["stop_requested"](), "wait")
            else:
                self.assertEqual(current, "所以想养一只，你觉得呢？")
                execute("reply", bindings[-1], text="我们先看看养猫需要哪些准备。", finish=True)
            return "这是内部文字，不可以发送。"
        bot.ai.chat = AsyncMock(side_effect=model)
        first = await (self.private(bot, official, "one", "因为") if private else
                       self.fixture.receive(bot, official, "one", "因为"))
        second = await (self.private(bot, official, "two", "我喜欢猫") if private else
                        self.fixture.receive(bot, official, "two", "我喜欢猫", elapsed=1))
        bot.send_action.assert_not_awaited() if not official else None
        bot.send_group_message.assert_not_awaited() if not official else None
        if official:
            first.reply.assert_not_awaited()
            second.reply.assert_not_awaited()
        third = await (self.private(bot, official, "three", "所以想养一只，你觉得呢？") if private else
                       self.fixture.receive(bot, official, "three", "所以想养一只，你觉得呢？", elapsed=2))
        self.assertEqual(len(snapshots), 3)
        self.assertEqual(snapshots[0]["cached_messages"], [])
        self.assertEqual([r["text"] for r in snapshots[1]["cached_messages"]], ["因为"])
        self.assertEqual([r["text"] for r in snapshots[2]["cached_messages"]], ["因为", "我喜欢猫"])
        if official:
            third.reply.assert_awaited_once()
            self.assertEqual(third.reply.await_args.kwargs["content"], "我们先看看养猫需要哪些准备。")
        elif private:
            bot.send_action.assert_awaited_once()
            self.assertIn("我们先看看养猫需要哪些准备。", str(bot.send_action.await_args))
        else:
            bot.send_group_message.assert_awaited_once()
        broker, token = bot._get_main_coordinator().broker.from_context(bindings[-1])
        context = broker.context(token)
        self.assertFalse(broker.has_pending_speech(context["namespace"], context["scope"], context["session_key"], context["generation"]))

    async def test_onebot_group_repeated_holds(self):
        await self.check_cycle(False)

    async def test_official_group_repeated_holds(self):
        await self.check_cycle(True)

    async def test_onebot_private_with_image_prefix(self):
        await self.check_cycle(False, private=True)

    async def test_official_private_with_image_context(self):
        await self.check_cycle(True, private=True)

    async def test_held_turn_cannot_send_and_model_failure_keeps_previous_cache(self):
        bot, _ = self.fixture.bot(stored_cid="speech-cid")
        calls = []
        async def model(key, prompt, **options):
            event = prompt_object(prompt, "console_context_file")
            calls.append(event)
            context_file = event["console_context_file"]
            if len(calls) == 1:
                execute("speech", context_file, operation="hold")
                result = handle({"id": 1, "method": "tools/call", "params": {"name": "qqbot_cli",
                    "arguments": {"action": "reply", "context_file": context_file,
                                  "params": {"text": "已缓存回合不能追问", "finish": True}}}})
                self.assertTrue(result["result"]["isError"])
                self.assertEqual(options["stop_requested"](), "wait")
                return ""
            if len(calls) == 2:
                execute("message", context_file, text="未提交草稿，失败时取消")
                raise RuntimeError("fixture model failed before submitting")
            execute("speech", context_file, operation="hold")
            return ""
        bot.ai.chat = AsyncMock(side_effect=model)
        for index, text in enumerate(("因为", "我喜欢猫", "继续解释")):
            await self.fixture.receive(bot, False, str(index), text, elapsed=index)
        self.assertEqual([r["text"] for r in calls[-1]["speech"]["cached_messages"]], ["因为"])
        bot.send_group_message.assert_not_awaited()

    async def test_proactive_event_stays_silent_while_any_member_has_unfinished_speech(self):
        bot, key = self.fixture.bot(stored_cid="speech-cid")
        async def hold(key, prompt, **options):
            execute("speech", prompt_object(prompt, "console_context_file")["console_context_file"], operation="hold")
            return ""
        bot.ai.chat = AsyncMock(side_effect=hold)
        await self.fixture.receive(bot, False, "hold", "因为")
        console = bot._get_main_coordinator()
        ctx = replace(console.contexts[key], proactive_event=True, sender_id="")
        await console.chat(key, "空闲主动话题", ctx=ctx, idle_event=True)
        bot.ai.chat.assert_awaited_once()
        bot.send_group_message.assert_not_awaited()

    async def test_hold_stops_native_generation_without_waiting_for_final_text(self):
        from antigravity_client import AntigravityClient
        from test_explicit_turn_finish import inbound
        bot, key = self.fixture.bot(stored_cid="speech-native")
        bot.ai.chat = AntigravityClient.chat.__get__(bot.ai)
        bot.ai._is_ready = True
        bot.ai._brain_dir = str(Path("speech-brain").resolve())
        bot.ai._cancel_native_conversation = AsyncMock()
        bot.ai._ensure_conversation_present = AsyncMock()
        log = Path(bot.ai._brain_dir) / "speech-native/.system_generated/logs/transcript.jsonl"
        log.parent.mkdir(parents=True)
        async def command(args, **options):
            event = prompt_object(args[-1], "console_context_file")
            log.write_text(json.dumps(inbound(10, args[-1].splitlines()[0])) + "\n", encoding="utf-8")
            execute("speech", event["console_context_file"], operation="hold")
            await asyncio.Event().wait()
        bot.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        try:
            await asyncio.wait_for(self.fixture.receive(bot, False, "native-hold", "因为"), 3)
            bot.ai._cancel_native_conversation.assert_awaited_once_with("speech-native", kill_background_tasks=False)
            self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "speech-native")
            bot.send_group_message.assert_not_awaited()
            console = bot._get_main_coordinator()
            self.assertTrue(console.broker.has_pending_speech(console.namespace, console.contexts[key].scope,
                                                             key, bot.ai.get_or_create_session(key).generation))
        finally:
            await asyncio.get_running_loop().shutdown_default_executor()


if __name__ == "__main__":
    unittest.main()
