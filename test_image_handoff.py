"""Image branches hand detailed observations back to normal conversations."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
from test_ai_fixture import ConsoleAgentMock

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from antigravity_client import AntigravityClient
from bot_client import GeminiQQBot
from config import AppConfig
from group_memory import GroupMemoryManager
from onebot_client import OneBotClient
from persona_manager import PersonaManager


DESCRIPTION = "统计页显示累计游玩 33 小时 36 分钟；总游戏次数 8；最高连续天数 5；折线图在星期三达到 5 小时峰值。最下方明细写着最后游玩日期为 2026 年 3 月 18 日。"
UNCERTAINTIES = "右下角标签太小，无法确定它是平均时长还是单次时长；不能据此断言帐号最后登录时间。"


class ImageHandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qqbot_image_handoff_")
        self.previous = os.getcwd()
        os.chdir(self.temporary.name)
        self.clients = []
        self.ais = []
        self.timeline = []
        self.picture = Path("data/cache/images/qq_capture/statistics.png")
        self.picture.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 400), "orange").save(self.picture)
        self.downloader = patch("image_handler._download_image_sync", side_effect=AssertionError("unexpected image download"))
        self.downloader.start()

    async def asyncTearDown(self):
        for client in self.clients:
            await client._close_runtime()
        self.downloader.stop()
        os.chdir(self.previous)
        self.temporary.cleanup()

    def config(self):
        config = AppConfig()
        config.bot.chat_buffer_seconds = 0
        config.bot.search_enabled = False
        config.bot.memory_enabled = False
        config.bot.autonomous_enabled = True
        config.bot.reply_probability = 0.1
        config.bot.cooldown_seconds = 1
        config.bot.mention_probability = 0
        return config

    def ai(self):
        ai = AntigravityClient(sessions_file="sessions-" + str(len(self.ais)) + ".json")
        async def chat(key, prompt, **options):
            session = ai.get_or_create_session(key)
            if not session.conversation_id:
                session.conversation_id = "cid_" + uuid.uuid4().hex[:20]
            if ":view:" in key:
                self.timeline.append("vision")
                path = prompt.split("指定图片绝对路径：", 1)[1].split("\n", 1)[0]
                self.assertTrue(Path(path).is_file())
                options["response_trace"].extend([
                    {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": path}}]},
                    {"type": "TOOL_RESULT", "status": "DONE", "content": "Displayed image"}])
                return json.dumps({"description": DESCRIPTION, "uncertainties": UNCERTAINTIES}, ensure_ascii=False)
            self.timeline.append(key)
            return '{"should_reply":true,"action":"none","reply":"图里的累计游玩时长是33小时36分钟。"}'
        async def archive(key, path, **options):
            self.assertTrue(Path(path).is_file())
            self.timeline.append("archive:" + key)
            session = ai._sessions.get(key)
            if session:
                session.conversation_id = None
            return True
        ai.chat = ConsoleAgentMock(side_effect=chat)
        ai.archive_session = AsyncMock(side_effect=archive)
        self.ais.append(ai)
        return ai

    def onebot(self, *, ai=None):
        client = OneBotClient(self.config(), ai or self.ai(), PersonaManager("personas.yaml"), GroupMemoryManager("memories"))
        client.self_id = "999"
        client.send_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"group_name": "测试群", "message_id": "receipt"}})
        client.send_group_message = AsyncMock(return_value={"status": "ok", "retcode": 0})
        client.send_private_message = AsyncMock(return_value={"status": "ok", "retcode": 0})
        client._is_ws_connected = MagicMock(return_value=True)
        async def capture(request, item, *, index=0):
            self.timeline.append("capture")
            return {"file": str(self.picture.resolve()), "url": "", "subType": "0",
                "capture_mode": "qq_image_viewer_screenshot",
                "capture_provenance": {"scope": request["scope"], "message_id": request["message_id"]}}
        client._qq_capture = SimpleNamespace(capture_message=AsyncMock(side_effect=capture))
        self.clients.append(client)
        return client

    def event(self, message_id, text, *, group="123", user="456", image=False, at=False, reference=""):
        prefix = ("[CQ:reply,id=" + reference + "]" if reference else "") + ("[CQ:at,qq=999] " if at else "")
        raw = prefix + text + ("[CQ:image,file=fixture.png,url=https://example.com/image.png]" if image else "")
        return {"post_type": "message", "message_type": "group" if group else "private",
            **({"group_id": group} if group else {}), "user_id": user, "message_id": message_id,
            "time": time.time(), "sender": {"nickname": "Alice"}, "raw_message": raw}

    async def drain(self, client):
        for _ in range(10):
            runtime = client._get_runtime()
            tasks = [task for task in getattr(client, "_event_tasks", ()) if not task.done()]
            tasks.extend(task for task in runtime.chat_workers.values() if not task.done())
            if runtime.work_worker and not runtime.work_worker.done():
                tasks.append(runtime.work_worker)
            if not tasks:
                return
            await asyncio.gather(*tasks)
        self.fail("background events did not drain")

    async def receive(self, client, event):
        client._queue_event(event)
        await self.drain(client)

    def calls(self, ai, key):
        from persona_isolation import base_session
        return [call for call in ai.chat.await_args_list if base_session(call.args[0]) == key]

    def assert_facts(self, prompt):
        self.assertIn(DESCRIPTION, prompt)
        self.assertIn(UNCERTAINTIES, prompt)
        self.assertIn("full_result_file", prompt)
        self.assertIn("original-1", prompt)

    async def test_group_branch_details_reach_unmentioned_first_turn_and_followup_without_reinspection(self):
        client = self.onebot()
        await self.receive(client, self.event("original-1", "看这张统计图", image=True, at=True))
        self.assertEqual(self.timeline[:2], ["capture", "vision"])
        self.assertTrue(any(value.startswith("archive:") and ":view:" in value for value in self.timeline))
        self.assert_facts(self.calls(client.ai, "onebot_group_123")[0].args[1])
        client._last_autonomous_reply_time["123"] = time.time() - 3
        with patch("onebot_client.random.random", return_value=0.99):
            await self.receive(client, self.event("normal-2", "大家今天过得怎么样"))
        main = self.calls(client.ai, "onebot_group_123")
        self.assertEqual(len(main), 2)
        self.assertNotIn(DESCRIPTION, main[-1].args[1])
        first_conversation = client.ai.get_or_create_session(client._chat_session_key("onebot_group_123")).conversation_id
        client._last_autonomous_reply_time["123"] = time.time() - 3
        with patch("onebot_client.random.random", return_value=0.99):
            await self.receive(client, self.event("normal-3", "那图里的最高连续天数是多少呢？"))
        main = self.calls(client.ai, "onebot_group_123")
        self.assertEqual(len(main), 3)
        self.assertIn("full_result_file", main[-1].args[1])
        self.assertNotIn(DESCRIPTION, main[-1].args[1])
        self.assertEqual(client.ai.get_or_create_session(client._chat_session_key("onebot_group_123")).conversation_id, first_conversation)
        self.assertEqual(self.timeline.count("vision"), 1)
        client._qq_capture.capture_message.assert_awaited_once()
        self.assertNotIn("get_image", [call.args[0] for call in client.send_action.await_args_list])

    async def test_restart_quote_restores_full_result_without_get_msg_capture_or_vision(self):
        first = self.onebot()
        await self.receive(first, self.event("original-1", "看这张统计图", image=True, at=True))
        await first._close_runtime()
        self.clients.remove(first)
        second = self.onebot()
        await self.receive(second, self.event("quoted-2", "那图里的数据呢？", at=True, reference="original-1"))
        replies = [call for call in second.ai.chat.await_args_list if ":view:" not in call.args[0]]
        self.assertEqual(len(replies), 1)
        self.assert_facts(replies[0].args[1])
        second._qq_capture.capture_message.assert_not_awaited()
        self.assertNotIn("get_msg", [call.args[0] for call in second.send_action.await_args_list])
        self.assertNotIn("get_image", [call.args[0] for call in second.send_action.await_args_list])
        self.assertEqual(self.timeline.count("vision"), 1)

    async def test_group_facts_do_not_leak_to_other_group_or_private_peer(self):
        client = self.onebot()
        await client._save_image_context("123", "original-1", [{"vision_verified": True,
            "visual_description": DESCRIPTION, "uncertainties": UNCERTAINTIES}], sender_id="456")
        await self.receive(client, self.event("other-group", "图里的时间呢？", group="789", at=True))
        await self.receive(client, self.event("other-private", "图里的时间呢？", group="", user="456"))
        for key in ("onebot_group_789", "onebot_private_456"):
            calls = self.calls(client.ai, key)
            self.assertEqual(len(calls), 1)
            self.assertNotIn(DESCRIPTION, calls[0].args[1])
            self.assertNotIn(UNCERTAINTIES, calls[0].args[1])

    async def test_private_branch_details_reach_normal_private_turn_without_reinspection(self):
        client = self.onebot()
        await self.receive(client, self.event("original-1", "看这张统计图", group="", image=True))
        await self.receive(client, self.event("normal-private", "那图里的最高连续天数是多少呢？", group=""))
        normal = self.calls(client.ai, "onebot_private_456")
        self.assertEqual(len(normal), 2)
        self.assert_facts(normal[0].args[1])
        self.assertEqual(self.timeline.count("vision"), 1)
        client._qq_capture.capture_message.assert_awaited_once()

    async def test_official_group_saved_branch_facts_reach_next_normal_turn(self):
        ai = self.ai()
        client = GeminiQQBot(config=self.config(), antigravity_client=ai,
            persona_manager=PersonaManager("personas.yaml"), memory_manager=GroupMemoryManager("official-memories"))
        self.clients.append(client)
        await client._save_image_context("123", "original-1", [{"vision_verified": True,
            "visual_description": DESCRIPTION, "uncertainties": UNCERTAINTIES,
            "visual_verification": {"view_evidence": {"archive": str(self.picture.parent / "archived.json")}}}], sender_id="456")
        message = SimpleNamespace(id="official-normal", group_openid="123", content="刚才那张统计图里的最高连续天数是多少？",
            author=SimpleNamespace(member_openid="456", user_openid="456", username="Alice", bot=False),
            attachments=[], message_reference=None, reply=AsyncMock(return_value={"id": "official-receipt"}))
        await client.on_group_message_create(message)
        await self.drain(client)
        normal = self.calls(ai, "group_123")
        self.assertEqual(len(normal), 1)
        self.assert_facts(normal[0].args[1])
        message.reply.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
