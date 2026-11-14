"""Native agent artifact workflows. Network, agent and QQ use offline fixtures."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
import os
from test_ai_fixture import ConsoleAgentMock

from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from config import AppConfig
from media_downloader import MediaError, sha256_file
from media_jobs import MediaService
from media_models import MediaIntent, MessageEnvelope
from media_router import parse_media_intent
from messaging import SendUnknown
from skills.base import SkillContext
from skills.media_skills import SendImageSkill, SendVideoSkill
from skills.task_skills import RunTaskSkill


def task_result(prompt, *, response_trace, kind="image", count=1, dimensions=(600, 200),
                matched=True, viewed=True, source="https://example.com/discovered/by/script", padding=0):
    """Fixture creates fresh artifacts like an actual agent, with tool evidence."""
    directory = Path(re.search(r"本任务输出目录：([^\n]+)", prompt)[1])
    response_trace.extend([
        {"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [{"name": "run_command",
            "args": {"CommandLine": "fixture downloads a resource using a script"}}]},
        {"type": "GENERIC", "status": "DONE", "content": "Download completed; source discovered in script."}])
    files = []
    for index in range(count):
        path = directory / (f"artifact-{index}." + {"image": "png", "video": "mp4", "file": "bin"}[kind])
        if kind == "image":
            Image.new("RGB", dimensions, "orange").save(path)
            if padding:
                with path.open("ab") as stream:
                    stream.write(b"x" * padding)
            if viewed:
                response_trace.extend([
                    {"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [{"name": "read_file",
                        "args": {"Path": str(path)}}]},
                    {"type": "GENERIC", "status": "DONE", "content": "Showing binary file of type image/png"}])
        elif kind == "video":
            path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * (padding or 512))
        else:
            path.write_bytes(b"fresh agent artifact\0" + b"f" * padding)
        files.append({"path": str(path), "kind": kind, "title": path.name, "source_url": source,
            "verification": {"matched": matched, "description": "orange wide character artwork",
                             "reason": "checked actual downloaded subject and source"}})
    return json.dumps({"answer": "Verified task completed", "sources": [{"title": "source", "url": source}],
                       "files": files})


class AgentArtifactTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_agent_artifacts_")
        self.previous = os.getcwd()
        os.chdir(self.tmp.name)
        self.config = AppConfig()
        self.config.media.cache_dir = str(Path("cache").resolve())
        self.config.media.database_path = "media.sqlite3"
        self.service = MediaService(self.config.media, namespace="onebot:fixture")
        self.ai = MagicMock()
        self.ai.chat = ConsoleAgentMock(side_effect=lambda key, prompt, **kwargs:
            task_result(prompt, response_trace=kwargs["response_trace"]))
        self.ctx = MessageEnvelope("onebot", "fixture", "123", "456", "source-message", "找一张角色立绘发我",
            AsyncMock(), is_group=True, ai_client=self.ai, ai_session_key="group_123",
            send_action=AsyncMock(return_value={"status": "ok", "data": {"message_id": 888}}))
        self.service.searcher.search = AsyncMock(side_effect=AssertionError("old search pipeline used"))
        self.service.processor.prepare = AsyncMock(side_effect=AssertionError("old downloader used"))

    def tearDown(self):
        self.service.cancel_all()
        os.chdir(self.previous)
        self.tmp.cleanup()

    def context(self):
        return SkillContext("__work__:group_123", "123", "456", media_context=self.ctx,
            app_config=self.config, ai_client=self.ai, media_service=self.service)

    async def handle(self, intent=None):
        outcome = {}
        await self.service.handle(self.ctx, intent or parse_media_intent(self.ctx.text), outcome=outcome)
        return outcome

    async def test_native_default_agent_downloads_and_sender_gets_unchanged_local_file(self):
        with patch("media_downloader.download_url", new=AsyncMock(side_effect=AssertionError("bot downloaded"))):
            result = await self.handle()
        self.assertTrue(result["success"])
        self.service.searcher.search.assert_not_awaited()
        self.service.processor.prepare.assert_not_awaited()
        self.ai.native_retrieve.assert_not_called()
        self.ai.chat.assert_awaited_once()
        kwargs = self.ai.chat.await_args.kwargs
        self.assertEqual(kwargs["timeout"], 0)
        self.assertFalse(kwargs.get("native_only", False))
        path = next(Path(self.service.config.cache_dir).rglob("artifact-0.png"))
        with Image.open(path) as image:
            self.assertEqual(image.size, (600, 200))
        sent = self.ctx.send_action.await_args.args[1]["message"]
        self.assertEqual([item["type"] for item in sent], ["image"])
        self.assertEqual(sent[0]["data"]["file"], path.as_uri())
        archive = json.loads(next(path.parent.glob("delivery.json")).read_text(encoding="utf-8"))
        self.assertEqual(archive["files"][0]["sha256"], sha256_file(path))
        self.assertEqual(archive["files"][0]["source_url"], "https://example.com/discovered/by/script")

    async def test_old_file_size_count_and_task_time_limits_do_not_apply(self):
        self.service.config.max_image_mb = 1
        self.service.config.max_image_count = 1
        self.service.config.download_timeout_seconds = 1
        self.service.config.task_timeout_seconds = 1
        async def agent(key, prompt, **kwargs):
            await asyncio.sleep(1.05)
            return task_result(prompt, response_trace=kwargs["response_trace"], count=4, padding=1024 * 1024 + 1)
        self.ai.chat.side_effect = agent
        result = await SendImageSkill().execute({"query": "角色立绘", "count": 4}, self.context())
        self.assertTrue(result.success)
        self.assertEqual(result.data["accepted"], 4)
        self.assertEqual(self.ctx.send_action.await_count, 4)

    async def test_pixel_dimensions_do_not_use_public_downloader_40mp_rule(self):
        self.ai.chat.side_effect = lambda key, prompt, **kwargs: task_result(prompt,
            response_trace=kwargs["response_trace"], dimensions=(10001, 4000))
        outcome = await self.handle()
        self.assertTrue(outcome["success"])
        path = next(Path(self.service.config.cache_dir).rglob("artifact-0.png"))
        with Image.open(path) as image:
            self.assertEqual(image.size, (10001, 4000))

    async def test_full_original_request_and_explicit_count_reach_agent(self):
        self.ctx.text = "帮我找12张角色横图立绘，保留原始分辨率，逐张核对出处后作为文件发我"
        intent = parse_media_intent(self.ctx.text)
        self.assertEqual(intent.count, 12)
        self.assertTrue(intent.count_requested)
        self.assertTrue((await self.handle(intent))["success"])
        prompt = self.ai.chat.await_args.args[1]
        self.assertIn(self.ctx.text, prompt)
        self.assertIn("请求数量：12", prompt)
        self.assertEqual(self.ctx.send_action.await_args.args[0], "upload_group_file")

    def test_artwork_phone_and_unspecified_count_do_not_imply_dimension_or_number_conditions(self):
        from skills.task_skills import media_task
        for text in ("发角色立绘", "找手机壁纸发我", "搜几张角色插画"):
            intent = parse_media_intent(text)
            self.assertEqual(intent.orientation, "")
            self.assertFalse(intent.count_requested)
            task = media_task(intent, self.ctx)
            self.assertNotIn("请求数量：", task)
            self.assertNotIn("用户指定方向：", task)

    async def test_search_video_and_url_all_use_same_agent_workflow(self):
        for index, text in enumerate(("/image 角色立绘", "/video 长视频", "/file 一份公开资料", "下载 https://example.com/page")):
            self.ctx.text, self.ctx.message_id = text, str(index)
            intent = parse_media_intent(text)
            self.ai.chat.side_effect = lambda key, prompt, **kwargs: task_result(prompt,
                response_trace=kwargs["response_trace"], kind=intent.kind)
            self.assertTrue((await self.handle(intent))["success"], text)
        self.assertEqual(self.ai.chat.await_count, 4)
        self.service.processor.prepare.assert_not_awaited()

    async def test_video_has_no_default_duration_or_automatic_transcoding(self):
        self.ai.chat.side_effect = lambda key, prompt, **kwargs: task_result(prompt,
            response_trace=kwargs["response_trace"], kind="video")
        with patch.object(self.service.processor, "video", new=AsyncMock(side_effect=AssertionError("transcoded"))):
            result = await SendVideoSkill().execute({"query": "完整演唱会视频"}, self.context())
        self.assertTrue(result.success)
        self.assertNotIn("用户指定最大时长：", self.ai.chat.await_args.args[1])

    async def test_missing_and_outside_task_files_are_not_sent(self):
        outside = Path("outside.png").resolve()
        Image.new("RGB", (10, 10)).save(outside)
        for index, path in enumerate((outside, Path(self.service.config.cache_dir) / "missing.png")):
            self.ctx.message_id = str(index)
            async def invalid(key, prompt, **kwargs):
                result = json.loads(task_result(prompt, response_trace=kwargs["response_trace"]))
                result["files"][0]["path"] = str(path)
                return json.dumps(result)
            self.ai.chat.side_effect = invalid
            self.assertFalse((await self.handle())["success"])
        self.ctx.send_action.assert_not_awaited()

    async def test_actual_image_view_is_reused_and_wrong_content_is_not_sent(self):
        self.ai.chat.side_effect = lambda key, prompt, **kwargs: task_result(prompt,
            response_trace=kwargs["response_trace"], matched=False)
        self.assertFalse((await self.handle())["success"])
        self.ctx.send_action.assert_not_awaited()
        self.assertEqual(self.ai.chat.await_count, 2)

    async def test_missing_view_evidence_triggers_real_image_inspection(self):
        self.ai.chat.side_effect = lambda key, prompt, **kwargs: task_result(prompt,
            response_trace=kwargs["response_trace"], viewed=False)
        with patch("visual_verification.verify_media_image", new=AsyncMock(side_effect=MediaError("missing image view"))) as verify:
            self.assertFalse((await self.handle())["success"])
        self.assertEqual(verify.await_count, 2)
        self.ctx.send_action.assert_not_awaited()

    async def test_partial_validation_retry_reuses_successful_image_without_parent_recheck(self):
        manifest = None
        prompts, checked = [], []
        async def agent(key, prompt, **kwargs):
            nonlocal manifest
            prompts.append(prompt)
            if manifest is None:
                manifest = task_result(prompt, response_trace=kwargs["response_trace"], viewed=False, count=2)
            return manifest
        async def verify(ctx, asset, intent):
            checked.append(Path(asset.local_path).name)
            if checked == ["artifact-0.png", "artifact-1.png"]:
                raise MediaError("second image needs correction")
            result = {"matched": True, "description": "orange test image", "reason": "viewed fixture",
                      "view_evidence": {"path": asset.local_path, "sha256": asset.sha256}}
            asset.metadata["visual_verification"] = result
            return result
        self.ai.chat.side_effect = agent
        with patch("visual_verification.verify_media_image", new=AsyncMock(side_effect=verify)):
            outcome = await self.handle()
        self.assertTrue(outcome["success"])
        self.assertEqual(checked, ["artifact-0.png", "artifact-1.png", "artifact-1.png"])
        self.assertEqual(len(prompts), 2)
        self.assertIn("artifact-0.png", prompts[1])
        self.assertIn("只修正本次失败项", prompts[1])
        self.assertEqual(self.ctx.send_action.await_count, 2)

    async def test_modified_file_cannot_reuse_old_parent_view_or_cached_decision(self):
        manifest = None
        async def agent(key, prompt, **kwargs):
            nonlocal manifest
            if manifest is None:
                manifest = json.loads(task_result(prompt, response_trace=kwargs["response_trace"], count=2))
                manifest["files"][1]["verification"]["matched"] = False
            else:
                Image.new("RGB", (600, 200), "blue").save(manifest["files"][0]["path"])
                manifest["files"] = manifest["files"][:1]
            return json.dumps(manifest)
        async def verify(ctx, asset, intent):
            result = {"matched": True, "description": "blue test image", "view_evidence": {
                "path": asset.local_path, "sha256": asset.sha256}}
            asset.metadata["visual_verification"] = result
            return result
        self.ai.chat.side_effect = agent
        with patch("visual_verification.verify_media_image", new=AsyncMock(side_effect=verify)) as check:
            outcome = await self.handle()
        self.assertTrue(outcome["success"])
        check.assert_awaited_once()
        self.assertEqual(self.ai.chat.await_count, 2)
        self.ctx.send_action.assert_awaited_once()

    async def test_unknown_platform_submission_is_not_retried_on_status(self):
        self.ctx.send_action.side_effect = SendUnknown("unknown submission")
        first = await self.handle()
        self.assertEqual(first["state"], "unknown")
        self.ctx.message_id = "status"
        await self.handle(MediaIntent(operation="status"))
        self.assertEqual(self.ctx.send_action.await_count, 1)

    async def test_expired_official_window_keeps_artifact_for_later_collection(self):
        self.ctx.transport = "official"
        self.ctx.received_at = time.time() - 400
        self.ctx.api = MagicMock()
        result = await self.handle()
        self.assertEqual(result["state"], "ready")
        self.assertTrue(next(Path(self.service.config.cache_dir).rglob("artifact-0.png")).is_file())
        self.ctx.api.post_group_file.assert_not_called()

    async def test_next_and_pick_reuse_native_workflow_without_old_downloader(self):
        self.assertTrue((await self.handle())["success"])
        first_path = self.ctx.send_action.await_args.args[1]["message"][0]["data"]["file"]
        self.ctx.message_id = "pick"
        self.assertTrue((await self.handle(MediaIntent(operation="pick", index=1)))["success"])
        self.assertEqual(self.ctx.send_action.await_args.args[1]["message"][0]["data"]["file"], first_path)
        self.ctx.message_id = "next"
        self.assertTrue((await self.handle(MediaIntent(operation="next")))["success"])
        self.assertIn("换一批", self.ai.chat.await_args.args[1])
        self.assertEqual(self.ai.chat.await_count, 2)
        self.service.processor.prepare.assert_not_awaited()

    async def test_direct_run_task_registers_job_and_delivers_large_generic_file(self):
        self.service.config.max_file_mb = 1
        self.ai.chat.side_effect = lambda key, prompt, **kwargs: task_result(prompt,
            response_trace=kwargs["response_trace"], kind="file", padding=1024 * 1024 + 1)
        result = await RunTaskSkill().execute({"task": "下载完整资料", "output_kind": "file"}, self.context())
        self.assertTrue(result.success)
        self.assertEqual(self.ctx.send_action.await_args.args[0], "upload_group_file")
        second = await RunTaskSkill().execute({"task": "下载完整资料", "output_kind": "file"}, self.context())
        self.assertFalse(second.success)
        self.assertEqual(self.ai.chat.await_count, 1)

    async def test_cancel_stops_agent_and_no_late_artifacts_are_sent(self):
        entered = asyncio.Event()
        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.ai.chat.side_effect = wait
        running = asyncio.create_task(self.handle())
        await asyncio.wait_for(entered.wait(), 2)
        self.ctx.message_id = "cancel"
        await self.handle(MediaIntent(operation="cancel"))
        await running
        self.assertEqual(self.service.store.job(self.ctx.owner)["state"], "cancelled")
        self.ctx.send_action.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
