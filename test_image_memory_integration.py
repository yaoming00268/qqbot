"""Real ingress stays responsive while the bot acquires files for its agents."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
from test_ai_fixture import ConsoleAgentMock

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from antigravity_client import AntigravityClient
from bot_client import GeminiQQBot
from config import AppConfig
from conversation_runtime import current_event
from group_memory import GroupMemoryManager
from media_downloader import MediaError
from onebot_client import OneBotClient


class ImageMemoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_image_memory_")
        self.previous = Path.cwd()
        os.chdir(self.tmp.name)
        self.cfg = AppConfig()
        self.cfg.bot.chat_settle_seconds = 0  # Focus on visual work remaining independent of the chat lane.
        self.cfg.bot.chat_buffer_seconds = 0
        self.cfg.bot.search_enabled = False
        self.cfg.bot.memory_enabled = False
        self.ai = AntigravityClient(sessions_file="sessions.json")
        self.ai.chat = ConsoleAgentMock(return_value='{"should_reply":true,"reply":"正常聊天"}')
        self.memory = GroupMemoryManager("memories")
        self.bot = OneBotClient(self.cfg, self.ai, memory_manager=self.memory)
        self.bot.self_id = "999"
        self.bot._is_ws_connected = lambda: True
        self.bot.send_action = AsyncMock(return_value={"status": "ok", "data": {"message_id": "receipt"}})
        self.bot.send_group_message = AsyncMock()
        self.bot.send_private_message = AsyncMock()

    async def asyncTearDown(self):
        await self.bot._close_runtime()
        os.chdir(self.previous)
        self.tmp.cleanup()

    def event(self, *, image=True, private=False, mid="42"):
        result = {"post_type": "message", "message_type": "private" if private else "group",
                  "message_id": mid, "user_id": 456, "sender": {"nickname": "Fixture"},
                  "raw_message": ("" if private else "[CQ:at,qq=999] ") +
                  ("这是什么[CQ:image,file=opaque-token]" if image else "晚上好")}
        if not private:
            result["group_id"] = 123
        return result

    async def test_direct_image_handlers_queue_before_dedup(self):
        for private in (False, True):
            data = self.event(private=private, mid=str(private))
            queued = []
            with patch.object(self.bot, "_queue_event", side_effect=queued.append):
                await (self.bot._handle_private_message(data) if private else self.bot._handle_group_message(data))
            self.assertEqual(queued, [data])
            self.assertNotIn(data["message_id"], self.bot._processed_msg_ids)
        self.ai.chat.assert_not_awaited()

    async def test_capture_file_exists_before_agent_and_no_source_resolution_on_success(self):
        order = []
        async def capture(request, item, **kwargs):
            self.assertEqual(request["account_id"], "999")
            picture = Path("data/cache/images/capture.png")
            picture.parent.mkdir(parents=True)
            Image.new("RGB", (640, 400), "orange").save(picture)
            order.append("saved")
            return {"file": str(picture.resolve()), "capture_mode": "qq_chat_screenshot"}
        async def identify(key, prompt, **kwargs):
            order.append("view")
            picture = prompt.split("指定图片绝对路径：")[1].splitlines()[0]
            self.assertTrue(Path(picture).is_file())
            self.assertIn("QQBot 已完成图片采集", prompt)
            kwargs["response_trace"].extend([
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": picture}}]},
                {"type": "TOOL_RESULT", "status": "DONE", "content": "Displayed image"}])
            return '{"description":"橙色画面"}'
        self.bot._qq_capture = SimpleNamespace(capture_message=capture)
        self.ai.chat.side_effect = identify
        self.ai.archive_session = AsyncMock(return_value=True)
        data = self.event()
        with patch("qq_image_source.QQImageSourceResolver.resolve", new_callable=AsyncMock) as resolve:
            text, images = await self.bot._process_qq_images(data, content=data["raw_message"], segments=None)
        self.assertEqual(order, ["saved", "view"])
        resolve.assert_not_awaited()
        self.assertTrue(images[0]["vision_verified"])
        self.assertNotIn("QQ 来图处理", text)
        self.assertNotIn("data/cache", text)
        self.ai.archive_session.assert_awaited_once()

    async def test_opaque_source_resolves_in_background_while_same_group_chats(self):
        self.bot.send_group_message = AsyncMock(wraps=OneBotClient.send_group_message.__get__(self.bot))
        entered, release = asyncio.Event(), asyncio.Event()
        async def capture(*args, **kwargs):
            raise MediaError("window unavailable")
        self.bot._qq_capture = SimpleNamespace(capture_message=capture)
        source = Path("napcat-source.png").resolve()
        Image.new("RGB", (640, 400), "orange").save(source)
        async def action(name, params, **kwargs):
            if name == "get_image":
                self.assertEqual(current_event.get().lane, "work")
                self.assertEqual(params["file"], "opaque-token")
                entered.set()
                await release.wait()
                return {"status": "ok", "data": {"file": str(source)}}
            return {"status": "ok", "data": {"message_id": "receipt"}}
        self.bot.send_action.side_effect = action
        async def agent(key, prompt, **kwargs):
            if ":view:" not in key:
                return '{"should_reply":true,"reply":"正常聊天"}'
            picture = prompt.split("指定图片绝对路径：")[1].splitlines()[0]
            self.assertTrue(Path(picture).is_file())
            self.assertNotEqual(Path(picture), source)
            kwargs["response_trace"].extend([
                {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": picture}}]},
                {"type": "TOOL_RESULT", "status": "DONE", "content": "Displayed image"}])
            return '{"description":"实际橙色画面"}'
        self.ai.chat.side_effect = agent
        self.ai.archive_session = AsyncMock(return_value=True)
        await self.bot._handle_group_message(self.event())
        await asyncio.wait_for(entered.wait(), 2)
        self.bot._queue_event(self.event(image=False, mid="43"))
        runtime = self.bot._get_runtime()
        await asyncio.wait_for(runtime.chat_workers["onebot:account_1:group:123"], 2)
        self.assertFalse(runtime.work_worker.done())
        release.set()
        await asyncio.wait_for(runtime.work_worker, 3)
        self.assertTrue(self.bot.send_group_message.await_count >= 2)
        prompts = [c.args[1] for c in self.ai.chat.await_args_list if ":view:" not in c.args[0]]
        self.assertTrue(any("实际橙色画面" in p for p in prompts))
        self.assertTrue(all("QQ 来图处理" not in p and "opaque-token" not in p for p in prompts))
        sends = [c for c in self.bot.send_action.await_args_list if c.args[0] == "send_group_msg"]
        self.assertEqual(len(sends), 2)
        self.assertFalse(any(seg["type"] == "reply" for seg in sends[0].args[1]["message"]))
        # New chat does not force a quote on the completed image answer.
        self.assertFalse(any(segment["type"] == "reply" for segment in sends[1].args[1]["message"]))

    async def test_normal_onebot_chat_passes_manifest_without_memory_body(self):
        self.cfg.bot.memory_enabled = True
        memory = self.memory.get_group_memory("123")
        memory.permanent_preset = "SENTINEL_MEMORY_BODY"
        self.memory.save_group_memory(memory)
        self.memory.get_memory_prompt = MagicMock(side_effect=AssertionError("memory body injected"))
        self.memory.get_permanent_preset = MagicMock(side_effect=AssertionError("preset body injected"))
        await self.bot._handle_group_message(self.event(image=False))
        call = self.ai.chat.await_args
        self.assertNotIn("SENTINEL_MEMORY_BODY", call.args[1])
        self.assertEqual(call.kwargs["memory_manifest"]["group_openid"], "123")
        self.assertTrue(all(Path(f["path"]).is_absolute() for f in call.kwargs["memory_manifest"]["files"]))

    async def test_official_startup_and_reply_use_file_manifest(self):
        self.cfg.bot.memory_enabled = True
        memory = self.memory.get_group_memory("123")
        memory.permanent_preset = "SENTINEL_MEMORY_BODY"
        self.memory.save_group_memory(memory)
        self.memory.preload_recent_memories = MagicMock(side_effect=AssertionError("full startup preload"))
        self.memory.get_memory_prompt = MagicMock(side_effect=AssertionError("memory body injected"))
        self.memory.get_permanent_preset = MagicMock(side_effect=AssertionError("preset body injected"))
        official = GeminiQQBot(config=self.cfg, antigravity_client=self.ai, memory_manager=self.memory)
        official._connection = MagicMock()
        official._connection.state.robot = SimpleNamespace(id="999", name="FixtureBot")
        message = SimpleNamespace(id="43", group_openid="123", content="晚上好",
                    author=SimpleNamespace(member_openid="456", username="Fixture"), attachments=None,
                    message_reference=None, reply=AsyncMock(return_value={"id": "receipt"}))
        try:
            await official._handle_group_message(message, True)
            call = self.ai.chat.await_args
            self.assertNotIn("SENTINEL_MEMORY_BODY", call.args[1])
            self.assertEqual(call.kwargs["memory_manifest"]["group_openid"], "123")
        finally:
            await official._close_runtime()
