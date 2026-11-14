"""Offline integration coverage for website routing, original images and delivery."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
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

from command_backends import parse_backend_options
from config import AppConfig, GeminiWebConfig, MediaConfig
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from gemini_web_client import (GeminiWebError, _ImageDownloadSession, available_models, cookie_values,
                               open_gemini_web, public_error, save_generated_image)
from media_downloader import MediaError
from media_jobs import MediaService
from media_models import MessageEnvelope
from skills.base import SkillContext
from skills.image_generation import GenerateImageSkill


class GeminiRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-gemini-route-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.context = self.broker.issue_context({"namespace": "onebot:a", "scope": "onebot:a:group:123",
            "sender_id": "456", "source_message_id": "source", "is_group": True,
            "input_images": [{"local_path": "placeholder.png"}]}, self.root / "contexts")

    def record(self, receipt):
        row = self.broker.record(receipt["id"])
        return row, json.loads(row["result"])

    def test_qq_generation_preserves_multiline_prompt_model_count_and_original_inputs(self):
        receipt = execute("command", self.context,
            text='/图片 --provider gemini_web --model "Gemini Pro" --count 2 --size 1024x1024 保留人物\n背景换成樱花')
        row, payload = self.record(receipt)
        data = json.loads(row["task"])
        self.assertEqual(row["kind"], "generate_image")
        self.assertEqual(data["prompt"], "保留人物\n背景换成樱花")
        self.assertEqual((data["model"], data["count"], data["size"]), ("Gemini Pro", 2, "1024x1024"))
        self.assertEqual(payload["provider"], "gemini_web")
        self.assertEqual(payload["model"], "Gemini Pro")

    def test_model_only_routes_translation_and_search_without_switching_main_chat(self):
        for command, kind in (("/翻译 --model flash 转成英语：你好", "task"),
                              ("/搜索 --provider gemini_web 新闻", "search"),
                              ("/处理 --model pro 分析附件", "process")):
            row, payload = self.record(execute("command", self.context, text=command))
            self.assertEqual((row["kind"], payload["provider"]), (kind, "gemini_web"))
            self.assertNotIn("--model", row["task"])
        row, payload = self.record(execute("command", self.context, text="/搜索 默认搜索"))
        self.assertNotIn("provider", payload)
        # Empty /process still accepts the current attachment as before.
        row, _ = self.record(execute("command", self.context, text="/处理"))
        self.assertEqual(row["kind"], "process")

    def test_model_is_part_of_idempotency_but_repeat_does_not_duplicate_work(self):
        receipts = [execute("search", self.context, query="新闻", provider="gemini_web", model=model)
                    for model in ("pro", "pro", "flash")]
        self.assertEqual(receipts[0]["id"], receipts[1]["id"])
        self.assertNotEqual(receipts[0]["id"], receipts[2]["id"])

    def test_unsupported_routes_and_invalid_flags_do_not_create_tasks(self):
        for command in ("/搜索 --provider api 新闻", "/图片 --model pro --model flash 猫",
                        "/图片 --count 0 猫", "/图片 --provider missing 猫", "/图片 --model pro"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                execute("command", self.context, text=command)
        with self.assertRaises(ValueError):
            execute("submit", self.context, task_type="code", task="写计算器", provider="gemini_web")
        self.assertEqual(self.broker.pending("onebot:a"), [])

    def test_prompt_flags_after_description_and_option_separator_are_preserved(self):
        self.assertEqual(parse_backend_options("画一个写着 --model pro 的牌子", "generate_image"),
                         ("画一个写着 --model pro 的牌子", {}))
        self.assertEqual(parse_backend_options("-- --model 是示例正文", "task"), ("--model 是示例正文", {}))

    def test_cli_accepts_model_and_provider_and_persists_route(self):
        cli = str(Path(__file__).with_name("console_tools") / "cli.py")
        result = subprocess.run([sys.executable, cli, "generate-image", "--context-file", self.context,
            "--prompt", "保留人物", "--provider", "gemini_web", "--model", "flash"],
            capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        row, payload = self.record(json.loads(result.stdout))
        self.assertEqual((payload["provider"], payload["model"]), ("gemini_web", "flash"))
        self.assertEqual(json.loads(row["task"])["prompt"], "保留人物")


class GeminiCredentialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-gemini-cookies-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cookies.json"
        self.config = GeminiWebConfig(cookie_file=str(self.path), secure_1psid_env="TEST_QQBOT_PSID",
                                      secure_1psidts_env="TEST_QQBOT_PSIDTS", proxy_env="TEST_QQBOT_PROXY", auto_browser=False)
        env = patch.dict(os.environ, {"TEST_QQBOT_PSID": "", "TEST_QQBOT_PSIDTS": "", "TEST_QQBOT_PROXY": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_cookie_object_and_browser_export_and_environment_priority(self):
        self.path.write_text(json.dumps({"__Secure-1PSID": "file-session", "__Secure-1PSIDTS": "file-ts"}), encoding="utf-8")
        self.assertEqual(cookie_values(self.config), ("file-session", "file-ts"))
        self.path.write_text(json.dumps([{"domain": ".google.com", "name": "__Secure-1PSID", "value": "export-session"},
            {"domain": ".google.com", "name": "__Secure-1PSIDTS", "value": "export-ts"},
            {"domain": "other.example", "name": "__Secure-1PSID", "value": "foreign"}]), encoding="utf-8")
        self.assertEqual(cookie_values(self.config), ("export-session", "export-ts"))
        with patch.dict(os.environ, {"TEST_QQBOT_PSID": "env-session"}):
            self.assertEqual(cookie_values(self.config), ("env-session", ""))

    async def test_missing_login_fails_before_any_network_or_guest_generation(self):
        with patch("gemini_webapi.GeminiClient") as factory:
            with self.assertRaisesRegex(MediaError, "登录态"):
                async with open_gemini_web(self.config):
                    self.fail("guest session was accepted")
            factory.assert_not_called()

    async def test_probe_reads_account_models_closes_connection_and_does_not_generate(self):
        from gemini_webapi.constants import AccountStatus
        client = SimpleNamespace(init=AsyncMock(), close=AsyncMock(), account_status=AccountStatus.AVAILABLE,
            list_models=lambda: [SimpleNamespace(model_name="gemini-pro", display_name="Pro", is_available=True)],
            generate_content=AsyncMock())
        with patch.dict(os.environ, {"TEST_QQBOT_PSID": "test-session"}), \
             patch("gemini_webapi.GeminiClient", return_value=client):
            self.assertEqual(await available_models(self.config),
                             [{"name": "gemini-pro", "display_name": "Pro", "available": True}])
        client.close.assert_awaited_once()
        client.generate_content.assert_not_awaited()

    async def test_third_party_errors_are_sanitized_and_connections_close(self):
        client = SimpleNamespace(init=AsyncMock(side_effect=RuntimeError("private-cookie-value")), close=AsyncMock())
        with patch.dict(os.environ, {"TEST_QQBOT_PSID": "test-session"}), \
             patch("gemini_webapi.GeminiClient", return_value=client):
            with self.assertRaises(MediaError) as raised:
                async with open_gemini_web(self.config):
                    self.fail("initialization error ignored")
        self.assertNotIn("private-cookie-value", str(raised.exception))
        self.assertEqual(raised.exception.diagnostic["stage"], "connect")
        client.close.assert_awaited_once()

    async def test_task_error_is_not_misclassified_as_a_login_failure_by_connection_scope(self):
        from gemini_webapi.constants import AccountStatus
        client = SimpleNamespace(init=AsyncMock(), close=AsyncMock(), account_status=AccountStatus.AVAILABLE)
        error = OSError("private-local-path")
        with patch.dict(os.environ, {"TEST_QQBOT_PSID": "test-session"}), \
             patch("gemini_webapi.GeminiClient", return_value=client):
            with self.assertRaises(OSError) as raised:
                async with open_gemini_web(self.config):
                    raise error
        self.assertIs(raised.exception, error)
        client.close.assert_awaited_once()

    def test_wrapped_transport_timeout_keeps_its_cause_without_exposing_error_text(self):
        from gemini_webapi.exceptions import APIError
        from curl_cffi.requests.exceptions import Timeout
        error = APIError("private-response-body")
        error.__cause__ = Timeout("https://private.example/?token=secret", code=28)
        message = public_error(error, stage="generation")
        self.assertIn("超时", message)
        self.assertIn("结果尚未确认", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("private", message)

    def test_broken_stream_after_http_headers_is_reported_as_a_transport_error(self):
        from curl_cffi.requests.exceptions import ConnectionError
        error = ConnectionError("private-stream-url", code=56, response=SimpleNamespace(status_code=200))
        message = public_error(error, stage="generation")
        self.assertIn("生成请求连接失败", message)
        self.assertNotIn("HTTP 200", message)
        self.assertNotIn("private", message)


class GeminiImageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-gemini-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cfg = AppConfig()
        cfg.media.image_generation_provider = "gemini_web"
        cfg.media.cache_dir = str(self.root / "cache")
        cfg.media.database_path = str(self.root / "media.sqlite3")
        self.service = MediaService(cfg.media)
        self.ctx = MessageEnvelope("onebot", "fixture", "123", "456", "source", "图生图", AsyncMock(),
            is_group=True, send_action=AsyncMock())
        self.ai = SimpleNamespace(chat=AsyncMock())
        self.context = SkillContext("__work__:group_123", "123", "456", app_config=cfg,
            media_context=self.ctx, media_service=self.service, ai_client=self.ai)
        self.reference = self.root / "original.png"
        Image.new("RGB", (180, 120), "blue").save(self.reference)
        self.model = SimpleNamespace(model_name="gemini-pro", is_available=True)
        self.client = SimpleNamespace(resolve_model=MagicMock(return_value=self.model), generate_content=AsyncMock())

    def website(self):
        @asynccontextmanager
        async def opened(config):
            yield self.client
        return patch("skills.gemini_web.open_gemini_web", opened)

    def generated(self):
        from gemini_webapi.types import GeneratedImage
        return GeneratedImage(url="https://lh3.googleusercontent.com/generated")

    @staticmethod
    async def save_generated(image, path, filename, **kwargs):
        target = Path(path) / filename
        Image.new("RGB", (240, 160), "orange").save(target)
        return str(target)

    async def test_original_bytes_uploaded_to_selected_model_and_validated_files_wait_for_console(self):
        from gemini_webapi.types import GeneratedImage
        self.client.generate_content.return_value = SimpleNamespace(text="完成", images=[self.generated()])
        with self.website(), patch.object(GeneratedImage, "save", self.save_generated):
            result = await GenerateImageSkill().execute({"prompt": "保留人物，换成樱花背景", "model": "pro",
                "_input_files": [{"local_path": str(self.reference)}]}, self.context)
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.action_type, "prepared")
        call = self.client.generate_content.await_args
        self.assertIs(call.kwargs["model"], self.model)
        uploaded, = call.kwargs["files"]
        self.assertEqual(uploaded.read_bytes(), self.reference.read_bytes())
        self.assertNotEqual(uploaded, self.reference)
        self.assertIn("原图", call.args[0])
        file, = result.data["files"]
        self.assertEqual((file["provider"], file["width"], file["height"]), ("gemini_web", 240, 160))
        self.assertTrue(file["sha256"])
        archive = json.loads(Path(result.data["archive"]).read_text(encoding="utf-8"))
        self.assertEqual(archive["result"]["model"], "pro")
        self.ctx.send_action.assert_not_awaited()
        self.ai.chat.assert_not_awaited()
        self.assertEqual(self.service.tasks, {})
        self.assertEqual(self.service.active_leases, {})

    def download_session(self):
        session = SimpleNamespace()
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        return session, factory

    async def test_transient_download_recovers_same_image_with_fresh_authenticated_proxy_session(self):
        from gemini_webapi.types import GeneratedImage
        from curl_cffi.requests.exceptions import ConnectionError
        picture = self.generated()
        original_url = picture.url
        self.client.proxy = "http://fixture-proxy:8086"
        self.client.cookies = {"session": "fixture-session"}
        self.client.impersonate = "chrome"
        self.client.generate_content.return_value = SimpleNamespace(text="完成", images=[picture])
        session, factory = self.download_session()
        attempts = []

        async def save(image, path, filename, **kwargs):
            attempts.append(image.url)
            if len(attempts) == 1:
                # Full-size URL resolution in the SDK mutates the descriptor.
                image.url = "https://lh3.googleusercontent.com/partial-fullsize"
                raise ConnectionError("private-download-token", code=7)
            self.assertIs(kwargs["client"].session, session)
            self.assertTrue(kwargs["full_size"])
            return await self.save_generated(image, path, filename)

        with self.website(), patch.object(GeneratedImage, "save", save), \
             patch("curl_cffi.requests.AsyncSession", factory), self.assertLogs("GeminiWeb", level="WARNING") as logs:
            result = await GenerateImageSkill().execute({"prompt": "换成和服", "model": "pro",
                "_input_files": [{"local_path": str(self.reference)}]}, self.context)
        self.assertTrue(result.success, result.message)
        self.assertEqual(attempts, [original_url, original_url])
        self.assertEqual(picture.url, original_url)
        self.assertEqual(factory.call_args.kwargs["proxy"], self.client.proxy)
        self.assertIs(factory.call_args.kwargs["cookies"], self.client.cookies)
        factory.return_value.__aexit__.assert_awaited_once()
        self.assertNotIn("private-download-token", "".join(logs.output))
        self.client.generate_content.assert_awaited_once()
        self.assertTrue(Path(result.data["files"][0]["local_path"]).is_file())
        self.ctx.send_action.assert_not_awaited()

    async def test_real_image_downloader_follows_google_redirects_without_native_safe_redirect_mode(self):
        picture = self.generated()
        picture.client_ref = None
        self.client.client = SimpleNamespace(get=AsyncMock(side_effect=[
            SimpleNamespace(status_code=302, headers={"location": "/image/full-size"}),
            SimpleNamespace(status_code=200, headers={"content-type": "image/png"}, content=b"image-bytes")]))
        with patch("curl_cffi.requests.AsyncSession") as factory:
            saved = await save_generated_image(picture, self.root, "cdn.png", client=self.client, timeout_seconds=1)
        self.assertEqual(Path(saved).read_bytes(), b"image-bytes")
        calls = self.client.client.get.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].args[0], "https://lh3.googleusercontent.com/image/full-size")
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in calls))
        factory.assert_not_called()

    async def test_image_redirect_cannot_send_requests_to_untrusted_hosts_or_local_services(self):
        for location in ("http://127.0.0.1:8086/private", "https://untrusted.example/image",
                         "https://lh3.googleusercontent.com.evil.example/image", "https://user:secret@lh3.googleusercontent.com/image"):
            with self.subTest(location=location):
                session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=302,
                    headers={"location": location})))
                with self.assertRaisesRegex(MediaError, "异常图片下载地址"):
                    await _ImageDownloadSession(session).get("https://lh3.googleusercontent.com/generated")
                session.get.assert_awaited_once()

    async def test_google_image_redirect_loop_is_bounded(self):
        session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=302, headers={"location": "/again"})))
        with self.assertRaisesRegex(MediaError, "跳转次数过多"):
            await _ImageDownloadSession(session).get("https://lh3.googleusercontent.com/generated")
        self.assertEqual(session.get.await_count, 6)

    async def test_persistent_download_failure_is_safe_and_does_not_submit_generation_again(self):
        from gemini_webapi.types import GeneratedImage
        from curl_cffi.requests.exceptions import ConnectionError
        self.client.generate_content.return_value = SimpleNamespace(text="完成", images=[self.generated()])
        _, factory = self.download_session()
        save = AsyncMock(side_effect=ConnectionError("https://private.example/?token=download-secret", code=7))
        with self.website(), patch.object(GeneratedImage, "save", save), patch("curl_cffi.requests.AsyncSession", factory):
            result = await GenerateImageSkill().execute({"prompt": "生成一只猫"}, self.context)
        self.assertFalse(result.success)
        self.assertIn("已生成", result.message)
        self.assertIn("下载连接失败", result.message)
        self.assertNotIn("登录", result.message)
        self.assertEqual(result.data["diagnostic"]["stage"], "download")
        self.assertEqual(result.data["diagnostic"]["curl_code"], 7)
        self.assertEqual(save.await_count, 2)
        self.client.generate_content.assert_awaited_once()
        job = self.service.store.job(self.ctx.owner, result.data["job_id"])
        self.assertEqual(job["state"], "failed")
        for text in (json.dumps(result.data), job["detail"], result.message):
            self.assertNotIn("download-secret", text)
            self.assertNotIn("private.example", text)
        self.assertEqual(self.service.tasks, {})
        self.assertEqual(self.service.active_leases, {})
        self.ctx.send_action.assert_not_awaited()

    async def test_permanent_http_error_and_local_write_error_do_not_retry_download(self):
        from gemini_webapi.types import GeneratedImage
        from curl_cffi.requests.exceptions import HTTPError
        for index, error in enumerate((HTTPError("private-url", code=22,
                response=SimpleNamespace(status_code=403)), PermissionError("private-path"))):
            with self.subTest(error=type(error).__name__):
                picture = self.generated()
                save = AsyncMock(side_effect=error)
                with patch.object(GeneratedImage, "save", save), patch("curl_cffi.requests.AsyncSession") as factory:
                    with self.assertRaises(GeminiWebError) as raised:
                        await save_generated_image(picture, self.root, f"failed-{index}.png",
                            client=self.client, timeout_seconds=1)
                save.assert_awaited_once()
                factory.assert_not_called()
                self.assertEqual(raised.exception.diagnostic["stage"], "download")
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("登录", str(raised.exception))
                if index == 0:
                    self.assertIn("HTTP 403", str(raised.exception))

    async def test_download_has_an_overall_deadline_and_does_not_resubmit_generation(self):
        from gemini_webapi.types import GeneratedImage

        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()

        with patch.object(GeneratedImage, "save", stalled), patch("curl_cffi.requests.AsyncSession") as factory:
            with self.assertRaises(GeminiWebError) as raised:
                await save_generated_image(self.generated(), self.root, "stalled.png",
                    client=self.client, timeout_seconds=0.01)
        self.assertIn("下载超时", str(raised.exception))
        self.assertEqual(raised.exception.diagnostic["stage"], "download")
        factory.assert_not_called()

    async def test_cancellation_during_download_does_not_retry_or_send(self):
        from gemini_webapi.types import GeneratedImage
        self.client.generate_content.return_value = SimpleNamespace(text="完成", images=[self.generated()])
        with self.website(), patch.object(GeneratedImage, "save", AsyncMock(side_effect=asyncio.CancelledError())), \
             patch("curl_cffi.requests.AsyncSession") as factory, self.assertRaises(asyncio.CancelledError):
            await GenerateImageSkill().execute({"prompt": "生成一只猫"}, self.context)
        factory.assert_not_called()
        self.client.generate_content.assert_awaited_once()
        self.ctx.send_action.assert_not_awaited()
        self.assertEqual(self.service.tasks, {})
        self.assertEqual(self.service.active_leases, {})

    async def test_generation_failure_retains_sanitized_transport_diagnostics(self):
        from gemini_webapi.exceptions import APIError
        from curl_cffi.requests.exceptions import ConnectionError
        error = APIError("private-response")
        error.__cause__ = ConnectionError("private-generation-token", code=56)
        self.client.generate_content.side_effect = error
        with self.website():
            result = await GenerateImageSkill().execute({"prompt": "生成一只猫"}, self.context)
        self.assertFalse(result.success)
        self.assertIn("生成请求连接失败", result.message)
        self.assertEqual(result.data["diagnostic"]["stage"], "generation")
        self.assertEqual(result.data["diagnostic"]["curl_code"], 56)
        self.assertEqual(result.data["diagnostic"]["error_types"], ["APIError", "ConnectionError"])
        self.assertNotIn("private", json.dumps(result.data) + result.message)
        self.client.generate_content.assert_awaited_once()
        self.ctx.send_action.assert_not_awaited()

    async def test_local_validation_failure_does_not_claim_a_login_problem_or_leak_raw_errors(self):
        from gemini_webapi.types import GeneratedImage
        self.client.generate_content.return_value = SimpleNamespace(text="完成", images=[self.generated()])
        with self.website(), patch.object(GeneratedImage, "save", self.save_generated), \
             patch.object(self.service.processor, "accept_artifact", AsyncMock(side_effect=RuntimeError("private-validation-token"))):
            result = await GenerateImageSkill().execute({"prompt": "生成一只猫"}, self.context)
        self.assertFalse(result.success)
        self.assertIn("本地图片校验失败", result.message)
        self.assertEqual(result.data["diagnostic"]["stage"], "validation")
        self.assertNotIn("private", json.dumps(result.data) + result.message)
        self.client.generate_content.assert_awaited_once()
        self.ctx.send_action.assert_not_awaited()

    async def test_search_images_cannot_count_as_generated_images(self):
        from gemini_webapi.types import WebImage
        self.client.generate_content.return_value = SimpleNamespace(text="搜到的图片", images=[WebImage(url="https://example.com/a.png")])
        with self.website():
            result = await GenerateImageSkill().execute({"prompt": "生成一只猫"}, self.context)
        self.assertFalse(result.success)
        self.assertIn("没有返回生成图片", result.message)
        self.assertEqual(self.client.generate_content.await_count, 1)
        self.ctx.send_action.assert_not_awaited()
        self.assertEqual(self.service.tasks, {})

    async def test_count_mismatch_keeps_original_outputs_without_delivering_or_repeating(self):
        from gemini_webapi.types import GeneratedImage
        self.client.generate_content.return_value = SimpleNamespace(text="完成", images=[self.generated()])
        with self.website(), patch.object(GeneratedImage, "save", self.save_generated):
            result = await GenerateImageSkill().execute({"prompt": "生成两只猫", "count": 2}, self.context)
        self.assertFalse(result.success)
        self.assertIn("数量", result.message)
        self.assertTrue(Path(result.data["files"][0]["local_path"]).is_file())
        self.assertEqual(self.client.generate_content.await_count, 1)
        self.ctx.send_action.assert_not_awaited()

    async def test_cancel_releases_task_and_never_submits_to_qq(self):
        self.client.generate_content.side_effect = asyncio.CancelledError()
        with self.website(), self.assertRaises(asyncio.CancelledError):
            await GenerateImageSkill().execute({"prompt": "生成一只猫"}, self.context)
        self.assertEqual(self.service.tasks, {})
        self.assertEqual(self.service.active_leases, {})
        self.ctx.send_action.assert_not_awaited()

    async def test_selected_model_unavailable_fails_before_generation(self):
        self.model.is_available = False
        with self.website():
            result = await GenerateImageSkill().execute({"prompt": "生成一只猫"}, self.context)
        self.assertFalse(result.success)
        self.assertIn("无法使用", result.message)
        self.client.generate_content.assert_not_awaited()

    async def test_text_task_uploads_document_and_archives_full_answer(self):
        from skills.gemini_web import GeminiWebTaskSkill
        document = self.root / "reference.txt"
        document.write_text("facts", encoding="utf-8")
        self.client.generate_content.return_value = SimpleNamespace(text="完整答案\n来源：https://example.com", images=[])
        with self.website():
            result = await GeminiWebTaskSkill().execute({"task": "整理附件", "model": "auto",
                "_input_files": [{"local_path": str(document)}]}, self.context)
        self.assertTrue(result.success, result.message)
        self.assertEqual(Path(result.data["files"][0]["local_path"]).read_text(encoding="utf-8"),
                         "完整答案\n来源：https://example.com")
        self.assertIsNone(self.client.generate_content.await_args.kwargs["model"])
        self.assertEqual(self.client.generate_content.await_args.kwargs["files"][0].read_bytes(), document.read_bytes())


class GeminiIngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def run_command(self, command, *, image=False):
        from gemini_webapi.types import GeneratedImage
        from test_ai_fixture import drain_runtime
        bot, key = self.fixture.bot(stored_cid="main-cid")
        bot._is_ws_connected = lambda: True
        model = SimpleNamespace(is_available=True)
        client = SimpleNamespace(resolve_model=MagicMock(return_value=model), generate_content=AsyncMock(
            return_value=SimpleNamespace(text="来自网页端的完整结果", images=[GeneratedImage(
                url="https://lh3.googleusercontent.com/generated")] if image else [])))

        @asynccontextmanager
        async def opened(config):
            yield client

        async def main_reply(session_key, prompt, **kwargs):
            self.assertEqual(session_key, key)
            event = next(json.loads(line) for line in prompt.splitlines() if line.startswith('{"type": "task_result"'))
            self.assertTrue(event["success"], event)
            coordinator = bot._get_main_coordinator()
            context = list(coordinator.requests)[-1]
            execute("deliver", context, request_id=event["request_id"], text="成果完成", finish=True)
            execute("finish", context)
            return "内部结束"

        bot.ai.chat = AsyncMock(side_effect=main_reply)
        with patch("skills.gemini_web.open_gemini_web", opened), \
             patch.object(GeneratedImage, "save", GeminiImageDeliveryTests.save_generated):
            await self.fixture.receive(bot, False, "web-command", command)
            await drain_runtime(bot)
        coordinator = bot._get_main_coordinator()
        _, token = ConsoleBroker.from_context(list(coordinator.requests)[-1])
        rows = [coordinator.broker.record(identity) for identity in coordinator.broker.list_requests(token)]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "delivered")
        client.resolve_model.assert_called_once_with("flash")
        client.generate_content.assert_awaited_once()
        bot.ai.chat.assert_awaited_once()
        return json.loads(rows[0]["result"])

    async def test_qq_image_model_override_reaches_web_backend_then_main_approval_and_delivery(self):
        result = await self.run_command("/图片 --model flash 一只猫", image=True)
        self.assertEqual(result["result"]["files"][0]["provider"], "gemini_web")

    async def test_qq_search_can_opt_into_web_without_changing_default_backend(self):
        result = await self.run_command("/搜索 --provider gemini_web --model flash 查询新闻")
        self.assertEqual(result["result"]["answer"], "来自网页端的完整结果")
        self.assertEqual(Path(result["result"]["files"][0]["local_path"]).suffix, ".md")


class GeminiGuiTests(unittest.TestCase):
    def test_web_settings_roundtrip_and_live_model_list_preserves_selection(self):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from ui.components.media_tab import MediaTabWidget
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(media=MediaConfig(cache_dir=tmp, image_generation_provider="gemini_web"))
            widget = MediaTabWidget(cfg)
            self.addCleanup(widget.close)
            widget.gemini_web_model.setCurrentText("flash")
            widget.gemini_web_strings["cookie_file"].setText("my-cookies.json")
            restored = MediaConfig.model_validate(widget.collect_media_config_values())
            self.assertEqual(restored.image_generation_provider, "gemini_web")
            self.assertEqual(restored.gemini_web.model, "flash")
            self.assertEqual(restored.gemini_web.cookie_file, "my-cookies.json")
            widget._receive_gemini_models([{"name": "gemini-flash", "available": True},
                                          {"name": "unavailable", "available": False}], "")
            self.assertEqual(widget.gemini_web_model.currentText(), "flash")
            self.assertGreaterEqual(widget.gemini_web_model.findText("gemini-flash"), 0)
            self.assertEqual(widget.gemini_web_model.findText("unavailable"), -1)


if __name__ == "__main__":
    unittest.main()
