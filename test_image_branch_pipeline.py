"""Incoming pictures use isolated, archived vision work and screenshot-first fallback."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from image_handler import process_multimodal_message
from media_downloader import MediaError
from visual_verification import inspect_image


class VisionAgent:
    def __init__(self):
        self.calls = []
        self.archived = []

    async def chat(self, key, prompt, **options):
        self.calls.append((key, prompt, options))
        image_path = prompt.split("指定图片绝对路径：", 1)[1].split("\n", 1)[0]
        options["response_trace"].extend([
            {"type": "PLANNER_RESPONSE", "tool_calls": [
                {"name": "view_file", "args": {"AbsolutePath": image_path}}]},
            {"type": "TOOL_RESULT", "status": "DONE", "content": "Displayed image"},
        ])
        return '{"description":"图中是统计页，显示游玩时长和折线图","uncertainties":"小字看不清"}'

    async def archive_session(self, key, path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        assert payload["session_key"] == key
        if payload["completed"]:
            assert payload["result"]["view_evidence"]["sha256"]
        else:
            assert payload["result"] is None
        self.archived.append((key, path))
        return True


class ImageBranchPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qqbot_image_branch_")
        self.previous = os.getcwd()
        os.chdir(self.temporary.name)
        self.cache = Path("images")
        self.cache.mkdir()
        self.picture = self.cache / "fixture.png"
        Image.new("RGB", (640, 400), "orange").save(self.picture)
        self.agent = VisionAgent()

    def tearDown(self):
        os.chdir(self.previous)
        self.temporary.cleanup()

    async def test_repeated_views_own_different_branches_and_archive_before_cleanup(self):
        for _ in range(2):
            result = await inspect_image(self.agent, "group:123", self.picture, "describe")
            self.assertTrue(Path(result["view_evidence"]["archive"]).is_file())
        keys = [row[0] for row in self.agent.calls]
        self.assertEqual(len(set(keys)), 2)
        self.assertTrue(all(key.startswith("group:123:view:") for key in keys))
        self.assertEqual([row[0] for row in self.agent.archived], keys)
        self.assertTrue(all(row[2]["timeout"] == 0 for row in self.agent.calls))

    async def test_failed_view_archives_diagnostics_then_cleans_up_branch(self):
        self.agent.chat = AsyncMock(return_value='{"description":"unverified"}')
        with self.assertRaises(MediaError):
            await inspect_image(self.agent, "group:123", self.picture, "describe")
        self.assertEqual(len(self.agent.archived), 1)
        archive = next((self.cache / "vision_archives").glob("*.json"))
        self.assertFalse(json.loads(archive.read_text(encoding="utf-8"))["completed"])

    async def test_invalid_json_and_failed_result_persistence_still_cleanup_leaf(self):
        original = self.agent.chat
        async def invalid(*args, **kwargs):
            await original(*args, **kwargs)
            return "invalid JSON"
        self.agent.chat = invalid
        with self.assertRaises((MediaError, ValueError)):
            await inspect_image(self.agent, "__vision__:task", self.picture, "describe")
        self.assertEqual(len(self.agent.archived), 1)
        self.agent.chat = original
        callback = AsyncMock(side_effect=OSError("could not save facts"))
        with self.assertRaises(OSError):
            await inspect_image(self.agent, "__vision__:task", self.picture, "describe", on_result=callback)
        self.assertEqual(len(self.agent.archived), 2)

    async def test_screenshot_description_is_reused_without_download_or_main_chat_view(self):
        capture = AsyncMock(return_value={"file": str(self.picture.resolve()),
            "capture_mode": "qq_image_viewer_screenshot", "visual_description": "统计页与折线图"})
        with patch("image_handler._download_image_sync") as download:
            text, images = await process_multimodal_message("怎么看", segments=[
                {"type": "image", "data": {"url": "https://example.com/image.png"}}],
                cache_dir=str(self.cache), capture=capture, require_capture=True,
                fallback_download=True, ai_client=self.agent)
        capture.assert_awaited_once()
        download.assert_not_called()
        self.assertFalse(self.agent.calls)
        self.assertTrue(images[0]["vision_verified"])
        self.assertIn("统计页与折线图", text)
        self.assertNotIn(str(self.picture.resolve()), text)
        self.assertNotIn("请实际调用原生图像查看工具", text)

    async def test_failed_capture_downloads_then_recognizes_and_archives(self):
        order = []
        async def capture(*args, **kwargs):
            order.append("capture")
            raise MediaError("QQ 截图为空白")
        def download(url, target, timeout):
            order.append("download")
            Image.new("RGB", (640, 400), "orange").save(target)
            return True
        with patch("image_handler._download_image_sync", side_effect=download):
            text, images = await process_multimodal_message("多久没玩了", segments=[
                {"type": "image", "data": {"url": "https://example.com/image.png"}}],
                cache_dir=str(self.cache), capture=capture, require_capture=True,
                fallback_download=True, ai_client=self.agent, vision_session_key="job:original-message")
        self.assertEqual(order, ["capture", "download"])
        self.assertEqual(images[0]["capture_mode"], "download_fallback")
        self.assertEqual(images[0]["capture_error"], "QQ 截图为空白")
        self.assertTrue(images[0]["vision_verified"])
        self.assertIn("统计页", text)
        self.assertNotIn("https://example.com", text)
        self.assertEqual(len(self.agent.archived), 1)
        self.assertTrue(self.agent.calls[0][0].startswith("job:original-message:view:"))

    async def test_missing_screenshot_file_falls_back_to_download(self):
        capture = AsyncMock(return_value={"file": str((self.cache / "missing.png").resolve()),
            "capture_mode": "qq_image_viewer_screenshot"})
        def download(url, target, timeout):
            Image.new("RGB", (640, 400), "orange").save(target)
            return True
        with patch("image_handler._download_image_sync", side_effect=download) as fetch:
            _, images = await process_multimodal_message("image", segments=[
                {"type": "image", "data": {"url": "https://example.com/image.png"}}],
                cache_dir=str(self.cache), capture=capture, require_capture=True,
                fallback_download=True, ai_client=self.agent)
        fetch.assert_called_once()
        self.assertEqual(images[0]["capture_mode"], "download_fallback")
        self.assertTrue(images[0]["vision_verified"])

    async def test_passive_image_cannot_download_or_run_vision(self):
        with patch("image_handler._download_image_sync") as fetch:
            text, images = await process_multimodal_message("", segments=[
                {"type": "image", "data": {"url": "https://example.com/image.png"}}],
                cache_dir=str(self.cache), require_capture=True, allow_download=False,
                fallback_download=True, ai_client=self.agent)
        fetch.assert_not_called()
        self.assertFalse(self.agent.calls)
        self.assertIsNone(images[0]["local_path"])
        self.assertNotIn("https://example.com", text)

    async def test_download_without_qq_window_still_uses_vision_branch(self):
        text, images = await process_multimodal_message("请看图", segments=[
            {"type": "image", "data": {"file": str(self.picture.resolve())}}],
            cache_dir=str(self.cache), ai_client=self.agent, vision_session_key="official:123")
        self.assertTrue(images[0]["vision_verified"])
        self.assertIn("统计页", text)
        self.assertEqual(len(self.agent.archived), 1)

    async def test_cq_encoded_url_and_sticker_urls_survive_download_fallback(self):
        cases = [("[CQ:image,file=fixture,url=https://example.com/a.png?a=1&amp;b=2]", None,
                  "https://example.com/a.png?a=1&b=2"),
                 ("[CQ:mface,summary=表情,url=https://example.com/sticker.png]", None,
                  "https://example.com/sticker.png"),
                 ("", [{"type": "mface", "data": {"url": "https://example.com/sticker.png"}}],
                  "https://example.com/sticker.png")]
        async def unavailable(*args, **kwargs):
            raise MediaError("capture unavailable")
        for raw, segments, expected in cases:
            def download(url, target, timeout):
                self.assertEqual(url, expected)
                Image.new("RGB", (640, 400), "orange").save(target)
                return True
            with patch("image_handler._download_image_sync", side_effect=download) as fetch:
                _, images = await process_multimodal_message(raw, segments=segments,
                    cache_dir=str(self.cache), capture=unavailable, require_capture=True,
                    fallback_download=True, ai_client=self.agent)
            self.assertTrue(images[0]["vision_verified"])
            if "sticker" in expected:
                self.assertTrue(images[0]["is_sticker"])


if __name__ == "__main__":
    unittest.main()
