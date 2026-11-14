"""QQ input screenshots are mandatory when enabled; these tests never use live QQ."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
from test_ai_fixture import ConsoleAgentMock

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from antigravity_client import AntigravityClient
from config import AppConfig
from conversation_runtime import EventContext, current_event
from group_memory import GroupMemoryManager
from image_handler import has_qq_images, process_multimodal_message, strip_multimodal_tags
from media_downloader import MediaError
from onebot_client import OneBotClient
from persona_manager import PersonaManager
from qq_window_capture import QQCaptureService, QQCaptureTarget, QQWindow, Win32QQ


CDN = "https://multimedia.nt.qq.com.cn/download?fixture=original"


class CapturePriorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_capture_priority_")
        self.previous = os.getcwd()
        os.chdir(self.tmp.name)
        config = AppConfig()
        config.bot.search_enabled = False
        # These cases verify the immediate image work lane. Timed mixed
        # text/image batches are covered in test_persona_batch_recovery.
        config.bot.chat_buffer_seconds = 0
        ai = AntigravityClient(sessions_file="sessions.json")
        ai.chat = ConsoleAgentMock(return_value='{"should_reply":false,"action":"none","reply":""}')
        self.bot = OneBotClient(config, ai, PersonaManager("personas.yaml"), GroupMemoryManager("memories"))
        self.bot.self_id = "999"
        self.bot.send_group_message = AsyncMock()
        self.bot.send_private_message = AsyncMock()
        self.bot.send_action = AsyncMock(return_value={"status": "ok", "data": {"group_name": "Fixture group"}})
        self.bot._qq_capture = MagicMock(capture_message=AsyncMock(side_effect=self.capture))
        self.downloader = patch("image_handler._download_image_sync", side_effect=AssertionError("QQ image downloaded"))
        self.download = self.downloader.start()

    async def asyncTearDown(self):
        await self.bot._close_runtime()
        self.downloader.stop()
        os.chdir(self.previous)
        self.tmp.cleanup()

    async def capture(self, request, item, *, index=0):
        path = Path("data/cache/images/qq_capture") / (str(request["message_id"]) + f"-{index}.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 400), "orange").save(path)
        sticker = item.get("subType") in (1, "1", "sticker") or item.get("type") == "mface"
        return {"file": str(path.resolve()), "url": CDN, "subType": "1" if sticker else "0",
            "capture_mode": "qq_chat_screenshot" if sticker else "qq_image_viewer_screenshot",
            "capture_provenance": {"scope": request["scope"], "message_id": request["message_id"]},
            "visual_description": "orange sticker" if sticker else "orange photograph"}

    def event(self, *, raw=None, group=True, segments=None, message="m1"):
        data = {"post_type": "message", "message_type": "group" if group else "private",
            "user_id": 456, "message_id": message, "sender": {"nickname": "Fixture user"},
            "raw_message": raw if raw is not None else "[CQ:at,qq=999] 看这张图[CQ:image,file=fixture.png,url=" + CDN + "]"}
        if group:
            data["group_id"] = 123
        if segments is not None:
            data["message"] = segments
        return data

    async def process(self, data, **kwargs):
        return await self.bot._process_qq_images(data, content=data.get("raw_message", ""),
            segments=data.get("message"), **kwargs)

    async def test_direct_dispatch_without_runtime_still_captures_and_hides_cdn_url(self):
        self.assertIsNone(current_event.get())
        text, images = await self.process(self.event())
        self.bot._qq_capture.capture_message.assert_awaited_once()
        self.assertEqual(images[0]["capture_mode"], "qq_image_viewer_screenshot")
        self.assertEqual(images[0]["url"], "")
        self.assertNotIn(CDN, text)
        self.assertIn("QQ图片查看器截图", text)
        self.assertNotIn("| 原图:", text)
        self.download.assert_not_called()
        self.assertEqual(strip_multimodal_tags(text), "[CQ:at,qq=999] 看这张图".replace("[CQ:at,qq=999] ", ""))

    async def test_runtime_work_and_chat_use_same_capture_policy(self):
        for lane in ("work", "chat"):
            token = current_event.set(EventContext("g", "m", lane=lane))
            try:
                _, images = await self.process(self.event(message=lane))
            finally:
                current_event.reset(token)
            self.assertEqual(images[0]["capture_mode"], "qq_image_viewer_screenshot")
        self.assertEqual(self.bot._qq_capture.capture_message.await_count, 2)
        self.download.assert_not_called()

    async def test_capture_failure_resolves_original_and_keeps_unverified_content_out_of_prompt(self):
        data = self.event(segments=[{"type": "image", "data": {"file": "fixture.png", "url": CDN}}])
        self.bot._qq_capture.capture_message.side_effect = MediaError("no target QQ window")
        with patch("image_handler._download_image_sync", return_value=False) as fetch:
            text, images = await self.process(data)
        self.assertIsNone(images[0]["local_path"])
        self.assertEqual(images[0]["capture_mode"], "unavailable")
        self.assertNotIn(CDN, text)
        self.assertNotIn("file://", text)
        self.assertNotIn("路径:", text)
        self.assertIn("尚无经过确认的画面描述", text)
        self.assertIn("get_image", [c.args[0] for c in self.bot.send_action.await_args_list])
        self.assertTrue(fetch.called)

    async def test_work_capture_failure_downloads_and_recognizes_in_archived_branch(self):
        self.bot._qq_capture.capture_message.side_effect = MediaError("no target QQ window")
        def download(url, target, timeout):
            Image.new("RGB", (640, 400), "orange").save(target)
            return True
        async def recognize(key, prompt, **kwargs):
            path = prompt.split("指定图片绝对路径：", 1)[1].split("\n", 1)[0]
            kwargs["response_trace"].extend([
                {"type": "PLANNER_RESPONSE", "tool_calls": [
                    {"name": "view_file", "args": {"AbsolutePath": path}}]},
                {"type": "TOOL_RESULT", "status": "DONE", "content": "Displayed image"},
            ])
            return '{"description":"橙色统计页，展示游戏时长与折线图"}'
        self.bot.ai.chat.side_effect = recognize
        self.bot.ai.archive_session = AsyncMock(return_value=True)
        token = current_event.set(EventContext("g", "m1", lane="work"))
        try:
            with patch("image_handler._download_image_sync", side_effect=download) as fetch:
                text, images = await self.process(self.event())
        finally:
            current_event.reset(token)
        fetch.assert_called_once()
        self.bot._qq_capture.capture_message.assert_awaited_once()
        self.assertEqual(images[0]["capture_mode"], "download_fallback")
        self.assertTrue(images[0]["vision_verified"])
        self.assertIn("游戏时长与折线图", text)
        self.assertNotIn(CDN, text)
        self.assertEqual(self.bot.ai.chat.call_args.kwargs["timeout"], 0)
        self.assertIn(":view:", self.bot.ai.chat.call_args.args[0])
        self.bot.ai.archive_session.assert_awaited_once()
        self.assertTrue(Path(self.bot.ai.archive_session.call_args.args[1]).is_file())

    async def test_passive_image_does_not_expose_url_or_block_chat_for_capture(self):
        text, images = await self.process(self.event(), directed=False)
        self.bot._qq_capture.capture_message.assert_not_awaited()
        self.download.assert_not_called()
        self.assertNotIn(CDN, text)
        self.assertEqual(images[0]["capture_policy"], "qq_window")

    async def test_passive_image_guidance_is_not_mistaken_for_a_user_question(self):
        self.bot.app_config.bot.autonomous_enabled = True
        await self.bot._handle_group_message(self.event(raw="[CQ:image,file=fixture.png,url=" + CDN + "]"))
        self.bot.ai.chat.assert_not_awaited()
        self.bot._qq_capture.capture_message.assert_not_awaited()

    async def test_raw_and_segment_stickers_use_chat_capture(self):
        for index, kind in enumerate(("mface", "bface")):
            for raw_only in (True, False):
                data = self.event(raw=f"[CQ:{kind},summary=表情包]", message=f"{kind}-{raw_only}",
                    segments=None if raw_only else [{"type": kind, "data": {"url": CDN}}])
                self.assertTrue(has_qq_images(data["raw_message"], data.get("message")))
                _, images = await self.process(data)
                self.assertEqual(images[0]["capture_mode"], "qq_chat_screenshot")
                self.assertTrue(images[0]["is_sticker"])
        self.assertEqual(self.bot._qq_capture.capture_message.await_count, 4)

    async def test_napcat_sub_type_spelling_is_treated_as_sticker(self):
        for field in ("sub_type", "subtype", "subType"):
            data = self.event(raw="看这个表情", segments=[{"type": "image", "data": {field: 1, "url": CDN}}], message=field)
            _, images = await self.process(data)
            self.assertEqual(images[0]["capture_mode"], "qq_chat_screenshot")

    async def test_alias_mentions_stickers_and_references_are_queued_as_work(self):
        runtime = self.bot._get_runtime()
        lanes = []
        async def handle(data):
            lanes.append(current_event.get().lane)
        self.bot._handle_group_message = handle
        for text in ("@ヨンミン [CQ:mface,summary=表情包]", "[CQ:at,qq=999] [CQ:bface,id=1]",
                     "[CQ:at,qq=999][CQ:reply,id=previous]这张图是什么"):
            self.bot._queue_event(self.event(raw=text, message=str(len(lanes))))
            await runtime.work_worker
        self.assertEqual(lanes, ["work", "work", "work"])

    async def test_alias_group_message_captures_before_model_reply(self):
        data = self.event(raw="@ヨンミン 看看[CQ:image,file=fixture.png,url=" + CDN + "]")
        await self.bot._handle_group_message(data)
        await self.bot._get_runtime().work_worker
        self.bot._qq_capture.capture_message.assert_awaited_once()
        self.download.assert_not_called()
        prompt = self.bot.ai.chat.await_args.args[1]
        self.assertNotIn(CDN, prompt)
        self.assertIn("QQ图片查看器截图", prompt)

    async def test_private_cached_original_is_recaptured_then_reused_as_screenshot(self):
        self.bot._recent_messages_cache["old"] = {"message_id": "old", "private_user_id": "456",
            "user_id": "456", "nickname": "Fixture user", "image_analyses": [
                {"local_path": "data/cache/images/old.png", "url": CDN, "summary": "old original"}]}
        incoming = self.event(group=False, raw="[CQ:reply,id=old]这张图是什么")
        images = await self.bot._private_quoted_images(incoming, "456")
        self.assertEqual(images[0]["capture_mode"], "qq_image_viewer_screenshot")
        request = self.bot._qq_capture.capture_message.await_args.args[0]
        self.assertEqual(request["conversation_id"], "456")
        self.assertEqual(request["message_id"], "old")
        self.assertIn(":private:456", request["scope"])
        await self.bot._private_quoted_images(incoming, "456")
        self.bot._qq_capture.capture_message.assert_awaited_once()

    async def test_group_get_msg_without_group_id_uses_group_capture_and_screenshot_label(self):
        self.bot.send_action.side_effect = lambda action, *args, **kwargs: {"status": "ok", "data":
            {"user_id": 789, "sender": {"nickname": "Original sender"}, "message_type": "group",
             "raw_message": "[CQ:image,file=original.png,url=" + CDN + "]"} if action == "get_msg" else {"group_name": "Fixture group"}}
        await self.bot._handle_group_message(self.event(raw="[CQ:at,qq=999][CQ:reply,id=old]看看这张图"))
        await self.bot._get_runtime().work_worker
        self.bot._qq_capture.capture_message.assert_awaited_once()
        request = self.bot._qq_capture.capture_message.await_args.args[0]
        self.assertEqual(request["conversation_id"], "123")
        self.assertEqual(request["message_id"], "old")
        self.assertIn(":group:123", request["scope"])
        prompt = self.bot.ai.chat.await_args.args[1]
        self.assertIn("QQ图片查看器截图", prompt)
        self.assertNotIn("| 原图:", prompt)
        self.assertNotIn(CDN, prompt)

    async def test_explicitly_disabled_capture_keeps_original_downloader_available(self):
        self.bot.app_config.bot.qq_window_capture = False
        with patch("image_handler._download_image_sync", return_value=False) as download:
            await self.process(self.event())
        download.assert_called_once()
        self.bot._qq_capture.capture_message.assert_not_awaited()

    async def test_capture_required_rejects_unlabelled_local_or_base64_fallback(self):
        for item in ({"file": "base64://eA=="}, {"file": "data/cache/images/saved.png", "url": CDN}):
            text, images = await process_multimodal_message("image", segments=[{"type": "image", "data": item}],
                capture=AsyncMock(return_value=item), require_capture=True, allow_download=True)
            self.assertIsNone(images[0]["local_path"])
            self.assertNotIn(CDN, text)
        self.download.assert_not_called()

    async def test_local_capture_does_not_call_model_or_desktop_navigation(self):
        class Backend:
            viewer = QQWindow(2, 7, "图片查看器", (0, 0, 800, 600))
            def locate_target(self, request, item, *, index=0):
                return QQCaptureTarget(self.viewer, (20, 20, 700, 500), "999", "123", "group", "m", index,
                                       "qq_image_viewer_screenshot", "owned_viewer_source")
            def capture(self, win): return Image.new("RGB", (800, 600), "orange")
        ai = MagicMock(chat=AsyncMock(side_effect=AssertionError("capture must not invoke AI")))
        result = await QQCaptureService(ai, backend=Backend()).capture_message(
            {"scope": "onebot:default:group:123", "account_id": "999", "conversation_id": "123", "message_id": "m"}, {})
        self.assertTrue(Path(result["file"]).is_file())
        self.assertNotIn("visual_description", result)
        self.assertEqual(result["capture_provenance"]["acquisition"], "qqbot_local_algorithm")
        ai.chat.assert_not_awaited()

    async def test_invalid_algorithmic_crop_fails_without_model_or_window_actions(self):
        backend = MagicMock()
        backend.locate_target.return_value = QQCaptureTarget(
            QQWindow(2, 7, "图片查看器", (0, 0, 800, 600)), (-1, 0, 999, 400), "999", "123", "group", "m", 0,
            "qq_image_viewer_screenshot", "owned_viewer_source")
        backend.capture.return_value = Image.new("RGB", (800, 600), "orange")
        ai = MagicMock(chat=AsyncMock())
        with self.assertRaises(MediaError):
            await QQCaptureService(ai, backend=backend).capture_message(
                {"scope": "onebot:default:group:123", "account_id": "999", "conversation_id": "123", "message_id": "m"}, {})
        ai.chat.assert_not_awaited()
        backend.click.assert_not_called()
        backend.search_digits.assert_not_called()


if __name__ == "__main__":
    unittest.main()
