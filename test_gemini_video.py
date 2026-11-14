"""Offline coverage for website video generation, bounded downloads and delivery."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from config import AppConfig, MediaConfig
from console_tools.broker import ConsoleBroker
from console_tools.chat_commands import parse_command
from console_tools.cli import execute
from console_tools.server import handle, tools_schema
from gemini_web_client import GeminiWebError, save_generated_video
from media_jobs import MediaService
from media_models import MessageEnvelope
from skills.base import SkillContext, SkillResult
from skills.video_generation import GenerateVideoSkill


VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20


def video_response(status=200, content=VIDEO_BYTES, **headers):
    return SimpleNamespace(status_code=status, content=content, headers=headers, reason="fixture")


class VideoRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.context = self.broker.issue_context({"namespace": "fixture", "scope": "fixture:group:123",
            "sender_id": "456", "source_message_id": "source", "is_group": True}, self.root / "contexts")

    def test_generation_aliases_and_search_have_separate_task_types(self):
        for alias in ("生成视频", "文生视频", "图生视频", "生视频", "generate-video", "generate_video", "videogen"):
            receipt = execute("command", self.context, text=f"/{alias} --model flash 小猫向前走\n镜头缓慢推进")
            row = self.broker.record(receipt["id"])
            self.assertEqual(row["kind"], "generate_video")
            self.assertEqual(json.loads(row["task"])["prompt"], "小猫向前走\n镜头缓慢推进")
            self.assertEqual(json.loads(row["result"])["model"], "flash")
        self.assertEqual(parse_command("/视频 小猫").name, "video")
        row = self.broker.record(execute("command", self.context, text="/video 小猫")["id"])
        self.assertEqual(row["kind"], "video")

    def test_cli_and_submit_bind_reference_files_and_deduplicate_same_request(self):
        uploaded = self.broker.record_files("fixture", "fixture:group:123", "source", "456",
            [{"native_id": "reference", "name": "原图.png", "size": 20}])
        first = execute("generate-video", self.context, prompt="小猫向前走", model="pro")
        second = execute("generate-video", self.context, prompt="小猫向前走", model="pro")
        self.assertEqual(first["id"], second["id"])
        payload = json.loads(self.broker.record(first["id"])["result"])
        self.assertEqual(payload["input_file_ids"], uploaded)
        self.assertEqual(payload["provider"], "gemini_web")
        submitted = execute("submit", self.context, task_type="generate_video", task="小猫回头")
        self.assertEqual(self.broker.record(submitted["id"])["kind"], "generate_video")

    def test_multiline_prompt_file_is_supported_by_the_actual_cli(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("让小猫向前走\n保留毛色", encoding="utf-8")
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("console_tools") / "cli.py"),
            "generate-video", "--context-file", self.context, "--prompt-file", str(prompt), "--model", "pro"],
            capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.broker.record(json.loads(result.stdout)["id"])
        self.assertEqual(json.loads(row["task"])["prompt"], prompt.read_text(encoding="utf-8"))

    def test_unsupported_api_and_image_flags_fail_before_any_work_is_queued(self):
        for command in ("/生成视频 --provider api 小猫", "/生成视频 --count 2 小猫", "/生成视频 --size 1024x1024 小猫"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                execute("command", self.context, text=command)
        with self.assertRaises(ValueError):
            execute("generate-video", self.context, prompt="小猫", provider="api")
        self.assertEqual(self.broker.pending("fixture"), [])

    def test_mcp_discovers_and_queues_video_generation(self):
        submission = next(row for row in tools_schema() if row["name"] == "qqbot_submit_task")
        self.assertIn("generate_video", submission["inputSchema"]["properties"]["task_type"]["enum"])
        result = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "qqbot_cli", "arguments": {"action": "generate-video", "context_file": self.context,
                "params": {"prompt": "小猫跑步", "model": "pro"}}}})
        data = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(self.broker.record(data["id"])["kind"], "generate_video")


class VideoGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cfg = AppConfig()
        cfg.media.cache_dir = str(self.root / "cache")
        cfg.media.database_path = str(self.root / "media.sqlite3")
        self.service = MediaService(cfg.media)
        self.ctx = MessageEnvelope("onebot", "fixture", "123", "456", "source", "生成视频", AsyncMock(),
            is_group=True, send_action=AsyncMock())
        self.ai = SimpleNamespace(chat=AsyncMock())
        self.context = SkillContext("__work__:group_123", "123", "456", app_config=cfg,
            media_context=self.ctx, media_service=self.service, ai_client=self.ai)
        self.model = SimpleNamespace(is_available=True)
        self.client = SimpleNamespace(resolve_model=MagicMock(return_value=self.model), generate_content=AsyncMock(),
            client=SimpleNamespace(get=AsyncMock(return_value=video_response())))
        from gemini_webapi.types import GeneratedVideo
        self.output = GeneratedVideo(url="https://lh3.googleusercontent.com/generated-video")
        self.client.generate_content.return_value = SimpleNamespace(text="视频完成", images=[], videos=[self.output])

    def website(self):
        @asynccontextmanager
        async def opened(config):
            yield self.client
        return patch("skills.gemini_web.open_gemini_web", opened)

    async def generate(self, **params):
        with self.website():
            return await GenerateVideoSkill().execute({"prompt": "小猫缓慢向前走", **params}, self.context)

    async def test_default_backend_uploads_reference_and_archives_real_video_before_delivery(self):
        self.assertEqual(self.service.config.video_generation_provider, "gemini_web")
        reference = self.root / "original.png"
        Image.new("RGB", (50, 30), "blue").save(reference)
        result = await self.generate(_input_files=[{"local_path": str(reference)}])
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.action_type, "prepared")
        self.client.resolve_model.assert_called_once_with("pro")
        call = self.client.generate_content.await_args
        self.assertIn("视频生成", call.args[0])
        self.assertEqual(call.kwargs["files"][0].read_bytes(), reference.read_bytes())
        asset, = result.data["files"]
        self.assertEqual((asset["kind"], asset["provider"], asset["mime"]), ("video", "gemini_web", "video/mp4"))
        self.assertEqual(Path(asset["local_path"]).read_bytes(), VIDEO_BYTES)
        self.assertTrue(asset["sha256"])
        archive = json.loads(Path(result.data["archive"]).read_text(encoding="utf-8"))
        self.assertEqual(archive["result"]["model"], "pro")
        self.client.generate_content.assert_awaited_once()
        self.ctx.send_action.assert_not_awaited()
        self.ai.chat.assert_not_awaited()
        self.assertEqual(self.service.tasks, {})
        self.assertEqual(self.service.active_leases, {})

    async def test_selected_model_and_auto_override_the_shared_website_default(self):
        result = await self.generate(model="auto")
        self.assertTrue(result.success, result.message)
        self.assertIsNone(self.client.generate_content.await_args.kwargs["model"])
        self.client.resolve_model.assert_not_called()

    async def test_images_or_search_video_links_cannot_replace_a_generated_video(self):
        from gemini_webapi.types import GeneratedImage, Video
        self.client.generate_content.return_value = SimpleNamespace(text="https://example.com/clip.mp4",
            images=[GeneratedImage(url="https://lh3.googleusercontent.com/still")],
            videos=[Video(url="https://example.com/clip.mp4")])
        result = await self.generate()
        self.assertFalse(result.success)
        self.assertIn("没有返回生成视频", result.message)
        self.client.client.get.assert_not_awaited()
        self.client.generate_content.assert_awaited_once()

    async def test_disabled_video_generation_or_sending_never_calls_a_model(self):
        for field in ("enabled", "video_enabled", "video_generation_enabled"):
            setattr(self.service.config, field, False)
            self.assertFalse((await self.generate()).success)
            setattr(self.service.config, field, True)
        self.client.generate_content.assert_not_awaited()
        self.ai.chat.assert_not_awaited()

    async def test_invalid_download_is_retained_but_never_delivered_or_regenerated(self):
        self.client.client.get.return_value = video_response(content=b"<html>not a video</html>")
        result = await self.generate()
        self.assertFalse(result.success)
        self.assertEqual(result.data["diagnostic"]["stage"], "validation")
        self.assertTrue(list(Path(result.data["directory"]).glob("*.mp4")))
        self.client.generate_content.assert_awaited_once()
        self.ctx.send_action.assert_not_awaited()

    async def test_pending_206_polling_has_a_deadline_and_never_starts_generation(self):
        self.client.client.get.return_value = video_response(status=206)
        with self.assertRaises(GeminiWebError) as raised:
            await save_generated_video(self.output, self.root, "pending.mp4", client=self.client, timeout_seconds=0.02)
        self.assertIn("视频已生成，但下载超时", str(raised.exception))
        self.assertEqual(raised.exception.diagnostic["stage"], "download")
        self.client.generate_content.assert_not_awaited()
        self.assertFalse((self.root / "pending.mp4").exists())

    async def test_transient_download_reconnects_to_the_same_output_without_regenerating(self):
        from curl_cffi.requests.exceptions import ConnectionError
        self.client.client.get.side_effect = ConnectionError("private-token", code=7)
        self.client.proxy = "http://fixture-proxy:8080"
        self.client.cookies = {"fixture": "cookie"}
        retry = SimpleNamespace(get=AsyncMock(return_value=video_response()))
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=retry)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("curl_cffi.requests.AsyncSession", factory):
            result = await self.generate()
        self.assertTrue(result.success, result.message)
        self.assertEqual(retry.get.await_args.args[0], self.output.url)
        self.assertEqual(factory.call_args.kwargs["proxy"], self.client.proxy)
        self.assertIs(factory.call_args.kwargs["cookies"], self.client.cookies)
        self.client.generate_content.assert_awaited_once()

    async def test_explicit_native_backend_keeps_video_generation_and_original_inputs(self):
        with patch("skills.task_skills.RunTaskSkill.execute", new_callable=AsyncMock,
                   return_value=SkillResult(True, "prepared")) as worker:
            result = await self.generate(provider="native", _input_files=[{"local_path": "fixture.png"}])
        self.assertTrue(result.success)
        params = worker.await_args.args[0]
        self.assertEqual(params["output_kind"], "video")
        self.assertTrue(params["_video_generation"] and params["_defer_delivery"])
        self.assertEqual(params["_input_files"], [{"local_path": "fixture.png"}])
        self.client.generate_content.assert_not_awaited()


class VideoGuiTests(unittest.TestCase):
    def test_video_default_backend_and_generation_switch_survive_gui_save(self):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from ui.components.media_tab import MediaTabWidget
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            widget = MediaTabWidget(AppConfig(media=MediaConfig(cache_dir=tmp)))
            self.addCleanup(widget.close)
            self.assertEqual(widget.video_generation_provider.currentData(), "gemini_web")
            self.assertTrue(widget.media_checks["video_generation_enabled"].isChecked())
            widget.video_generation_provider.setCurrentIndex(widget.video_generation_provider.findData("native"))
            widget.media_checks["video_generation_enabled"].setChecked(False)
            restored = MediaConfig.model_validate(widget.collect_media_config_values())
            self.assertEqual(restored.video_generation_provider, "native")
            self.assertFalse(restored.video_generation_enabled)


class VideoIngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def run_command(self, text, model_name):
        from gemini_webapi.types import GeneratedVideo
        from test_ai_fixture import drain_runtime
        bot, key = self.fixture.bot(stored_cid="main-cid")
        bot._is_ws_connected = lambda: True
        client = SimpleNamespace(resolve_model=MagicMock(return_value=SimpleNamespace(is_available=True)),
            client=SimpleNamespace(get=AsyncMock(return_value=video_response())), generate_content=AsyncMock(
                return_value=SimpleNamespace(text="已生成", images=[], videos=[GeneratedVideo(
                    url="https://lh3.googleusercontent.com/generated-video")])))
        @asynccontextmanager
        async def opened(config):
            yield client
        async def main_reply(session_key, prompt, **options):
            self.assertEqual(session_key, key)
            event = next(json.loads(line) for line in prompt.splitlines() if line.startswith('{"type": "task_result"'))
            self.assertTrue(event["success"], event)
            context = list(bot._get_main_coordinator().requests)[-1]
            execute("deliver", context, request_id=event["request_id"], text="视频完成", finish=True)
            execute("finish", context)
            return "内部结束"
        bot.ai.chat = AsyncMock(side_effect=main_reply)
        with patch("skills.gemini_web.open_gemini_web", opened):
            await self.fixture.receive(bot, False, "video-command", text)
            await drain_runtime(bot)
        coordinator = bot._get_main_coordinator()
        _, token = ConsoleBroker.from_context(list(coordinator.requests)[-1])
        rows = [coordinator.broker.record(identity) for identity in coordinator.broker.list_requests(token)]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["state"]), ("generate_video", "delivered"))
        client.resolve_model.assert_called_once_with(model_name)
        client.generate_content.assert_awaited_once()
        bot.ai.chat.assert_awaited_once()
        result = json.loads(rows[0]["result"])
        self.assertEqual(result["result"]["files"][0]["provider"], "gemini_web")

    async def test_qq_generation_defaults_to_web_then_main_approval_and_delivery(self):
        await self.run_command("/生成视频 一只小猫向前走", "pro")

    async def test_qq_generation_model_override_reaches_web_without_changing_main_model(self):
        await self.run_command("/图生视频 --model flash 保留主体，向前走", "flash")


if __name__ == "__main__":
    unittest.main()
