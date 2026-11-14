"""Offline downloads, real image decoding and recovery of poisoned media contexts."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from agent_transcript import native_diagnostic_rows
from agent_work import recover_media_context, run_agent_work
from antigravity_client import (AntigravityClient, ContentBlockedError, InvalidMediaError,
                               SessionInvalidatedError)
from media_downloader import MediaError, download_url, image_metadata, sha256_file
from media_previews import original_for_preview, prepare_image_preview
from media_worker import download_images
from visual_verification import _path, inspect_image, viewed_image_hashes


def view_trace(path):
    return [{"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "read_file", "id": "image",
                "args": {"Path": str(path)}}]},
            {"type": "TOOL_RESULT", "tool_call_id": "image", "status": "DONE",
             "content": "Showing binary file of type image/png"}]


def image_bytes(mode="RGB", format="JPEG"):
    output = io.BytesIO()
    Image.new(mode, (84, 120), 127 if mode == "L" else "orange").save(output, format)
    return output.getvalue()


class MediaPreviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-invalid-media-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def image(self, mode="L", format="JPEG"):
        path = self.root / ("original-" + mode + ".image")
        path.write_bytes(image_bytes(mode, format))
        return path

    def test_gray_cmyk_rgba_and_webp_are_rgb_while_original_bytes_stay_intact(self):
        for mode, format in (("L", "JPEG"), ("CMYK", "JPEG"), ("RGBA", "PNG"), ("RGB", "WEBP")):
            with self.subTest(mode=mode, format=format):
                original = self.image(mode, format)
                before = original.read_bytes()
                result = prepare_image_preview(original, self.root)
                with Image.open(result["preview_path"]) as picture:
                    self.assertEqual(picture.mode, "RGB")
                    self.assertEqual(picture.size, (84, 120))
                    self.assertIn(picture.format, ("PNG", "JPEG"))
                    self.assertFalse(picture.info.get("icc_profile"))
                self.assertEqual(original.read_bytes(), before)
                self.assertEqual(original_for_preview(result["preview_path"], self.root), (original, sha256_file(original)))

    def test_html_empty_and_truncated_jpeg_never_generate_preview(self):
        for content in (b"", b"<html>403 forbidden</html>", image_bytes()[:-20]):
            path = self.root / "bad.jpg"
            path.write_bytes(content)
            with self.assertRaises(MediaError):
                prepare_image_preview(path)
        self.assertFalse((self.root / ".media_previews").exists())

    def test_replacing_original_or_forging_manifest_invalidates_view_proof(self):
        original = self.image()
        result = prepare_image_preview(original, self.root)
        preview = Path(result["preview_path"])
        trace = view_trace(preview)
        self.assertEqual(viewed_image_hashes(trace, self.root)[_path(original)], sha256_file(original))
        original.write_bytes(image_bytes("RGB"))
        self.assertIsNone(original_for_preview(preview, self.root))
        record = preview.with_suffix(".json")
        forged = json.loads(record.read_text(encoding="utf-8"))
        forged["sha256"] = sha256_file(original)
        record.write_text(json.dumps(forged), encoding="utf-8")
        self.assertIsNone(original_for_preview(preview, self.root))
        self.assertNotIn(_path(original), viewed_image_hashes(trace, self.root))

    def test_viewing_original_does_not_prove_viewing_another_preview(self):
        original = self.image()
        result = prepare_image_preview(original)
        other = self.root / "other.png"
        other.write_bytes(image_bytes("RGB", "PNG"))
        self.assertNotIn(_path(original), viewed_image_hashes(view_trace(other), self.root))
        self.assertNotEqual(_path(result["preview_path"]), _path(original))

    async def test_vision_branch_reads_rgb_preview_and_binds_result_to_original(self):
        original = self.image()
        async def chat(key, prompt, **options):
            path = Path(prompt.split("指定图片绝对路径：", 1)[1].splitlines()[0])
            self.assertNotEqual(path, original)
            with Image.open(path) as picture:
                self.assertEqual(picture.mode, "RGB")
            options["response_trace"].extend(view_trace(path))
            return '{"matched":true,"description":"a grey rectangle"}'
        class AI:
            pass
        ai = AI()
        ai.chat = AsyncMock(side_effect=chat)
        ai.archive_session = AsyncMock(return_value=True)
        result = await inspect_image(ai, "vision", original, "identify")
        self.assertEqual(result["view_evidence"]["sha256"], sha256_file(original))
        self.assertEqual(Path(result["view_evidence"]["path"]), original)
        ai.archive_session.assert_awaited_once()

    async def test_vision_rejects_original_modified_during_inspection(self):
        original = self.image()
        async def chat(key, prompt, **options):
            path = prompt.split("指定图片绝对路径：", 1)[1].splitlines()[0]
            options["response_trace"].extend(view_trace(path))
            original.write_bytes(image_bytes("RGB"))
            return '{"matched":true,"description":"rectangle"}'
        class AI:
            pass
        ai = AI()
        ai.chat = AsyncMock(side_effect=chat)
        ai.archive_session = AsyncMock(return_value=True)
        with self.assertRaisesRegex(MediaError, "发生变化"):
            await inspect_image(ai, "vision", original, "identify")


class MediaDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-download-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    async def test_failed_replacement_leaves_completed_original_and_no_partial(self):
        target = self.root / "cover.jpg"
        previous = image_bytes()
        target.write_bytes(previous)
        async def fake(url, *, target, **options):
            target.write_bytes(b"<html>forbidden</html>")
            return b"", "text/html", url
        with patch("media_downloader.http_request", side_effect=fake):
            with self.assertRaises(MediaError):
                await download_url("https://example.com/cover.jpg", target, max_bytes=1_000_000,
                                   timeout=10, expected_kind="image")
        self.assertEqual(target.read_bytes(), previous)
        self.assertEqual(list(self.root.iterdir()), [target])

    async def test_signature_checks_reject_html_with_image_mime_and_truncation(self):
        for payload in (b"<html>not an image</html>", image_bytes()[:-30], b""):
            async def fake(url, *, target, **options):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                return b"", "image/jpeg", url
            with patch("media_downloader.http_request", side_effect=fake):
                with self.assertRaises(MediaError):
                    await download_url("https://example.com/picture.jpg", self.root/"picture.jpg",
                                       max_bytes=1_000_000, timeout=10, expected_kind="image")
            self.assertFalse((self.root/"picture.jpg").exists())
            self.assertFalse(list(self.root.glob("*.download")))

    async def test_batch_uses_referer_resumes_valid_bytes_and_refetches_changed_file(self):
        calls = []
        async def fake(url, *, target, headers, **options):
            calls.append((url, headers))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(image_bytes("L"))
            return b"", "image/jpeg", url
        items = [{"url": "https://cdn.example.com/cover.jpg", "referer": "https://example.com/chapter",
                  "path": "volume/cover.jpg"}]
        with patch("media_downloader.http_request", side_effect=fake):
            first = await download_images(items, self.root)
            second = await download_images(items, self.root)
            self.assertTrue(first["ok"] and second["ok"])
            self.assertTrue(second["files"][0]["reused"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1]["Referer"], items[0]["referer"])
            (self.root/"volume/cover.jpg").write_bytes(image_bytes("RGB"))
            third = await download_images(items, self.root)
            self.assertTrue(third["ok"])
            self.assertEqual(len(calls), 2)
        self.assertEqual(image_metadata(self.root/"volume/cover.jpg")["mode"], "L")

    async def test_one_bad_download_does_not_discard_other_completed_items(self):
        async def fake(url, *, target, **options):
            target.write_bytes(b"<html>captcha</html>" if "bad" in url else image_bytes())
            return b"", "image/jpeg", url
        items = [{"url": "https://example.com/"+name+".jpg", "path": name+".jpg"} for name in ("good", "bad")]
        with patch("media_downloader.http_request", side_effect=fake):
            result = await download_images(items, self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(result["files"][0]["ok"])
        self.assertFalse(result["files"][1]["ok"])
        self.assertTrue((self.root/"good.jpg").is_file())
        self.assertFalse((self.root/"bad.jpg").exists())

    async def test_manifest_cannot_overwrite_outside_workspace_or_duplicate_target(self):
        with patch("media_worker.download_url", AsyncMock()) as download:
            result = await download_images([{"url": "https://example.com/a.jpg", "path": "../outside.jpg"}], self.root)
        self.assertFalse(result["ok"])
        download.assert_not_awaited()

    async def test_cancellation_removes_partial_staging_without_touching_original(self):
        target = self.root/"kept.jpg"
        target.write_bytes(image_bytes())
        before = target.read_bytes()
        async def fake(url, *, target, **options):
            target.write_bytes(b"partial")
            raise asyncio.CancelledError()
        with patch("media_downloader.http_request", side_effect=fake):
            with self.assertRaises(asyncio.CancelledError):
                await download_url("https://example.com/a.jpg", target, max_bytes=10000, timeout=10, expected_kind="image")
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(list(self.root.glob("*.download")))


class MediaTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-media-http-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.requests = []
        self.payload = image_bytes("L")
        async def serve(request):
            self.requests.append(dict(request.headers))
            if request.path == "/403.jpg":
                return web.Response(status=403, body=b"<html>access denied</html>")
            if request.path == "/fake.jpg":
                return web.Response(content_type="image/jpeg", body=b"<html>captcha</html>")
            if request.path == "/empty.jpg":
                return web.Response(content_type="image/jpeg", body=b"")
            if request.path == "/partial.jpg":
                return web.Response(content_type="image/jpeg", body=self.payload[:-25])
            response = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
            await response.prepare(request)
            for offset in range(0, len(self.payload), 31):
                await response.write(self.payload[offset:offset+31])
            await response.write_eof()
            return response
        app = web.Application()
        app.router.add_get("/{name}", serve)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.base = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])

    async def test_real_chunked_download_checks_bytes_and_retains_headers(self):
        # Only this test-owned loopback fixture bypasses the public URL gate.
        with patch("media_downloader.public_url", side_effect=lambda value: value):
            await download_url(self.base+"/valid.jpg", self.root/"valid.jpg", max_bytes=10000, timeout=2,
                               headers={"Referer": "https://example.com/chapter"}, expected_kind="image")
        self.assertEqual((self.root/"valid.jpg").read_bytes(), self.payload)
        self.assertIn("Mozilla", self.requests[-1]["User-Agent"])
        self.assertEqual(self.requests[-1]["Referer"], "https://example.com/chapter")

    async def test_real_403_fake_image_empty_and_partial_are_not_published(self):
        with patch("media_downloader.public_url", side_effect=lambda value: value):
            for name in ("403", "fake", "empty", "partial"):
                with self.subTest(name=name), self.assertRaises(MediaError):
                    await download_url(self.base+"/"+name+".jpg", self.root/(name+".jpg"),
                                       max_bytes=10000, timeout=2, expected_kind="image")
        self.assertFalse(list(self.root.iterdir()))


class NativeMediaErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-native-error-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ai = AntigravityClient(sessions_file=str(self.root/"sessions.json"))
        self.ai._brain_dir = str(self.root/"brain")
        self.ai._CONVERSATION_CHECK_DELAY = 0
        self.ai._CONVERSATION_CHECK_INTERVAL = 0
        self.path = self.root/"brain/fixture/.system_generated/logs/transcript.jsonl"
        self.path.parent.mkdir(parents=True)
        self.ai._cancel_native_conversation = AsyncMock()

    def log(self, *rows):
        self.path.write_text("".join(json.dumps(row)+"\n" for row in rows), encoding="utf-8")

    def inbound(self, index=1, marker="current"):
        return {"step_index": index, "type": "USER_INPUT", "status": "DONE", "content": marker}

    def native(self, *, marker="current", error="INVALID_MEDIA", with_final=False):
        steps = [{"type": "CORTEX_STEP_TYPE_USER_INPUT", "status": "CORTEX_STEP_STATUS_DONE",
            "userInput": {"items": [{"text": marker}]}},
            {"type": "CORTEX_STEP_TYPE_ERROR_MESSAGE", "status": "CORTEX_STEP_STATUS_DONE",
             "metadata": {"source": "CORTEX_STEP_SOURCE_SYSTEM", "sourceTrajectoryStepInfo": {"stepIndex": 2}},
             "errorMessage": {"error": {"shortError": "INVALID_ARGUMENT (code 400)",
                "rpcErrorDetails": [json.dumps({"reason": error})], "thinkingSignature": "must not escape"}}}]
        if with_final:
            steps.append({"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "status": "CORTEX_STEP_STATUS_DONE"})
        return native_diagnostic_rows({"steps": steps})

    async def test_unlogged_native_terminal_media_error_releases_wait(self):
        self.log(self.inbound(), {"step_index": 2, "type": "GENERIC", "source": "TOOL", "status": "DONE"})
        native = self.native()
        native[-1]["step_index"] = 3
        self.ai._retry_run_status = AsyncMock(return_value={"status": "CASCADE_RUN_STATUS_IDLE",
            "numTotalSteps": 4, "diagnostic_rows": native})
        with self.assertRaises(InvalidMediaError):
            await self.ai._wait_for_model_response("fixture", timeout=1, request_marker="current")
        self.ai._cancel_native_conversation.assert_not_awaited()

    async def test_old_native_error_cannot_fail_a_new_bound_request(self):
        self.log(self.inbound(3), {"step_index": 4, "type": "GENERIC", "source": "TOOL", "status": "DONE"})
        self.ai._retry_run_status = AsyncMock(return_value={"status": "CASCADE_RUN_STATUS_IDLE",
            "numTotalSteps": 5, "diagnostic_rows": self.native(marker="old")})
        async def finish():
            await asyncio.sleep(.15)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"step_index": 5, "type": "PLANNER_RESPONSE", "source": "MODEL",
                                        "status": "DONE", "content": "new final"})+"\n")
        writer = asyncio.create_task(finish())
        try:
            self.assertEqual(await self.ai._wait_for_model_response("fixture", timeout=1,
                expected_step_min=1, request_marker="current"), "new final")
        finally:
            await writer

    async def test_running_snapshot_does_not_preempt_backend_retry(self):
        self.log(self.inbound())
        self.ai._retry_run_status = AsyncMock(return_value={"status": "CASCADE_RUN_STATUS_RUNNING",
            "diagnostic_rows": self.native()})
        with self.assertRaises(TimeoutError):
            await self.ai._wait_for_model_response("fixture", timeout=.15, request_marker="current")

    async def test_native_error_preserves_reason_without_thinking_or_media(self):
        rows = self.native()
        self.assertIn("INVALID_MEDIA", rows[-1]["error"])
        self.assertNotIn("must not escape", json.dumps(rows))
        self.assertEqual(rows[0]["content"], "current")
        self.assertFalse([row for row in self.native(with_final=True) if row["type"] == "ERROR_MESSAGE"])

    async def test_nested_agentapi_error_is_typed_and_policy_is_distinct(self):
        with self.assertRaises(InvalidMediaError):
            self.ai._check_agentapi_response(["send-message", "fixture"], {"error": {
                "code": 400, "details": [{"reason": "INVALID_MEDIA"}]}})
        with self.assertRaises(ContentBlockedError):
            self.ai._check_agentapi_response(["send-message", "fixture"], {"error": "content blocked by safety policy"})

    async def test_raw_rpc_error_missing_from_local_log_is_detected(self):
        from aiohttp import web
        requested = []
        async def serve(request):
            requested.append(await request.json())
            return web.json_response({"status": "CASCADE_RUN_STATUS_IDLE", "numTotalSteps": 4,
                "trajectory": {"steps": [{"type": "CORTEX_STEP_TYPE_USER_INPUT", "status": "CORTEX_STEP_STATUS_DONE",
                    "userInput": {"items": [{"text": "current"}]}},
                    {"type": "CORTEX_STEP_TYPE_ERROR_MESSAGE", "status": "CORTEX_STEP_STATUS_DONE",
                    "metadata": {"source": "CORTEX_STEP_SOURCE_SYSTEM", "sourceTrajectoryStepInfo": {"stepIndex": 3}},
                    "errorMessage": {"error": {"shortError": "INVALID_ARGUMENT (code 400)",
                        "details": json.dumps({"reason": "INVALID_MEDIA", "error_number": "2011"})}}}]}})
        app = web.Application()
        app.router.add_post("/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory", serve)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.ai.ls_address = "127.0.0.1:"+str(site._server.sockets[0].getsockname()[1])
        self.ai.csrf_token = "fixture"
        self.log({"step_index": 2, "type": "GENERIC", "status": "DONE", "source": "TOOL"})
        with self.assertRaises(InvalidMediaError):
            await self.ai._wait_for_model_response("fixture", timeout=1, expected_step_min=0, request_marker="current")
        self.assertEqual(requested[0], {"cascadeId": "fixture"})


class WorkerMediaRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-worker-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ai = AntigravityClient(sessions_file=str(self.root/"sessions.json"))
        self.key = "__work__:task:original"
        self.session = self.ai.get_or_create_session(self.key)
        self.session.conversation_id = "poisoned-native-context"

    async def test_work_continues_once_in_same_identity_and_reuses_download(self):
        work = self.root/"work"
        calls = []
        async def ask(prompt, trace, timeout):
            calls.append(prompt)
            if len(calls) == 1:
                (work/"download.jpg").write_bytes(image_bytes("L"))
                (work/"completed.zip").write_bytes(b"existing completed archive")
                trace.extend(view_trace(work/"download.jpg"))
                raise InvalidMediaError("fixture INVALID_MEDIA")
            self.assertIsNone(self.session.conversation_id)
            self.assertIn("不要重新下载全部文件", prompt)
            self.assertTrue((work/"completed.zip").is_file())
            return '{"answer":"done"}'
        async def repair(error):
            return await recover_media_context(self.ai, self.key, work, error)
        result = await run_agent_work(ask, "download", work, [], lambda data, trace: data["answer"], recover=repair)
        self.assertEqual(result[1], "done")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.ai.get_or_create_session(self.key), self.session)
        checkpoint = json.loads(next(work.glob("media_recovery_*.json")).read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["previous_conversation_id"], "poisoned-native-context")
        self.assertEqual(len(checkpoint["previews"]), 1)
        self.assertEqual(image_metadata(work/"download.jpg")["mode"], "L")

    async def test_repeated_invalid_media_does_not_loop(self):
        ask = AsyncMock(side_effect=InvalidMediaError("bad media"))
        repair = AsyncMock(return_value="repair")
        with self.assertRaises(InvalidMediaError):
            await run_agent_work(ask, "task", self.root/"work", [], lambda *args: None, recover=repair)
        self.assertEqual(ask.await_count, 2)
        repair.assert_awaited_once()

    async def test_policy_cancel_and_unknown_service_failure_are_not_replayed(self):
        for error in (ContentBlockedError("blocked"), asyncio.CancelledError(), RuntimeError("unknown")):
            ask, repair = AsyncMock(side_effect=error), AsyncMock()
            with self.assertRaises(type(error)):
                await run_agent_work(ask, "task", self.root/"work", [], lambda *args: None, recover=repair)
            ask.assert_awaited_once()
            repair.assert_not_awaited()

    async def test_cancelled_or_reset_worker_cannot_replace_conversation(self):
        def stale():
            raise SessionInvalidatedError("reset")
        with self.assertRaises(SessionInvalidatedError):
            await recover_media_context(self.ai, self.key, self.root, InvalidMediaError(), check_current=stale)
        self.assertEqual(self.session.conversation_id, "poisoned-native-context")
        with self.assertRaises(InvalidMediaError):
            await recover_media_context(self.ai, "onebot_group_main", self.root, InvalidMediaError())


class PersistentMediaRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_subagent_runtime import SubagentRuntimeTests
        self.fixture = SubagentRuntimeTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def run_worker(self, *, repeated=False):
        from console_tools.cli import execute
        from test_ai_fixture import prompt_object
        bot, key, coordinator, _ = await self.fixture.prepare()
        context = self.fixture.context(coordinator, key)
        created = execute("agents", context, operation="create", params={"name": "下载者", "text": "下载插图"})
        seen = []
        async def chat(session_key, prompt, **options):
            worker = bot.ai.get_or_create_session(session_key)
            workspace = Path(prompt_object(prompt, "output_dir")["output_dir"])
            seen.append((session_key, workspace, worker.conversation_id))
            if len(seen) == 1:
                worker.conversation_id = "poisoned-worker-native"
                (workspace/"original.jpg").write_bytes(image_bytes("L"))
                options["response_trace"].extend(view_trace(workspace/"original.jpg"))
                raise InvalidMediaError("fixture invalid media")
            if repeated:
                raise InvalidMediaError("same invalid media")
            self.assertIsNone(worker.conversation_id)
            self.assertTrue((workspace/"original.jpg").is_file())
            worker.conversation_id = "recovered-worker-native"
            return "已有插图核对完成"
        bot.ai.chat.side_effect = chat
        agent = coordinator.agents.mailbox.agent(created["id"])
        message = coordinator.agents.mailbox.claim(agent["id"])
        with patch.object(coordinator.agents, "schedule_reviews"):
            await coordinator.agents._run(agent, message)
        return coordinator, agent, message, seen, bot, key

    async def test_recovery_keeps_agent_mail_workspace_and_main_conversation(self):
        coordinator, agent, message, seen, bot, key = await self.run_worker()
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][:2], seen[1][:2])
        self.assertEqual(len(coordinator.agents.mailbox.all(coordinator.namespace)), 1)
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "persistent-main")
        with coordinator.broker.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM agent_mail WHERE seq=?", (message["seq"],)).fetchone()[0], "completed")
        saved = json.loads((seen[0][1]/("run-"+str(message["seq"])+".json")).read_text(encoding="utf-8"))
        self.assertIsNone(saved["failure"])
        checkpoint = json.loads(next(seen[0][1].glob("media_recovery_*.json")).read_text(encoding="utf-8"))
        self.assertIn("read_file", Path(checkpoint["trace_file"]).read_text(encoding="utf-8"))

    async def test_repeat_failure_stops_and_is_reported_with_specific_code(self):
        coordinator, agent, message, seen, bot, key = await self.run_worker(repeated=True)
        self.assertEqual(len(seen), 2)
        with coordinator.broker.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM agent_mail WHERE seq=?", (message["seq"],)).fetchone()[0], "failed")
        saved = json.loads((seen[0][1]/("run-"+str(message["seq"])+".json")).read_text(encoding="utf-8"))
        self.assertEqual(saved["failure"]["code"], "INVALID_MEDIA")
        self.assertTrue((seen[0][1]/"original.jpg").is_file())
        pending = coordinator.agents.pending(key)
        self.assertTrue(any(json.loads(row["payload"]).get("code") == "INVALID_MEDIA" for row in pending))
