"""Offline failure, expressiveness, latency contract and DSP regressions."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
from array import array
import io
import json
import math
from pathlib import Path
import random
import shutil
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

import aiohttp
from aiohttp import web

from audio_processing import DenoiseConfig, clean_reference
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.files import ConsoleFileError
from singing_service import SingingService, file_hash
from voice_config import VoiceConfig
from voice_model_cache import engine_identity, select_models
from voice_service import VoiceService
from voice_stream import PCMWriter, WavStreamDecoder, native_sovits_stream, voicestudio_stream
from voice_text import VoiceError, inline_emotions, prepare_chunks


def wav_bytes(seconds=.5):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        stream.writeframes(b"".join(struct.pack("<h", round(6000 * math.sin(i * math.tau * 440 / 24000)))
                                   for i in range(round(24000 * seconds))))
    return output.getvalue()


class EmotionTests(unittest.TestCase):
    def test_inline_switches_preserve_scopes_and_reset_without_extra_pause(self):
        config = VoiceConfig(default_language="mixed")
        chunks = prepare_chunks("[ja]今日は[emotion:sad]少し寂しい[emotion:]けど大丈夫。[/ja]"
                                "[emotion:firm][en]We can do it.[/en]", config, "neutral")
        self.assertEqual([c.emotion for c in chunks], ["neutral", "sad", "neutral", "firm"])
        self.assertEqual([c.language for c in chunks], ["ja", "ja", "ja", "en"])
        self.assertEqual([c.pause_before_ms for c in chunks[:3]], [0, 0, 0])
        self.assertEqual(chunks[3].pause_before_ms, config.pause_ms)
        self.assertNotIn("emotion", "".join(c.text for c in chunks))
        self.assertNotRegex("".join(c.text for c in chunks[:3]), r"[\u3400-\u9fff]")

    def test_switch_is_persistent_across_sentences_and_languages(self):
        chunks = prepare_chunks("[emotion:sad][ja]かなしい。まだかなしい。[/ja][en]Still sad.[/en]"
                                "[emotion:][zh]现在好了。[/zh]", VoiceConfig(default_language="mixed"), "neutral")
        self.assertEqual([c.emotion for c in chunks], ["sad", "sad", "sad", "neutral"])
        self.assertEqual(chunks[1].pause_before_ms, 120)

    def test_bad_or_unknown_tag_rejected_before_any_committed_message(self):
        with tempfile.TemporaryDirectory() as folder:
            broker = ConsoleBroker(Path(folder) / "db.sqlite3")
            context = broker.issue_context({"namespace": "test", "scope": "test", "sender_id": "1",
                "transport": "onebot", "voice": {"enabled": True, "emotions": ["neutral", "sad"]}}, Path(folder) / "contexts")
            _, token = ConsoleBroker.from_context(context)
            for text in ("hello [emotion:missing]bye", "hello [emotion:sad", "[emotion:sad]" * 65):
                with self.subTest(text=text[:40]), self.assertRaises((ConsoleFileError, ValueError)):
                    execute("reply", context, text=text, voice=True, emotion="neutral")
                self.assertEqual(broker.committed_messages(token), [])
            execute("reply", context, text="hello[emotion:sad]bye[emotion:]again", voice=True, emotion="neutral")
            self.assertEqual(len(broker.committed_messages(token)), 1)

    def test_all_text_survives_long_segments(self):
        text = "Hello there " * 30
        chunks = prepare_chunks(text, VoiceConfig(default_language="en", chunk_chars=20), "neutral")
        self.assertEqual("".join(c.text for c in chunks), text.strip())
        self.assertTrue(all(len(c.text) <= 20 for c in chunks))
        self.assertEqual(inline_emotions("[emotion:sad]a[emotion:]b", "neutral"), ["neutral", "sad"])


class WavDecoderTests(unittest.TestCase):
    def test_arbitrary_http_boundaries_and_zero_sized_header(self):
        original = wav_bytes()
        for streaming in (False, True):
            data = bytearray(original)
            if streaming:
                data[40:44] = b"\0" * 4
            decoder, pcm = WavStreamDecoder(), bytearray()
            for offset in range(0, len(data), 7):
                pcm.extend(decoder.feed(data[offset:offset + 7]))
            decoder.finish()
            self.assertEqual(bytes(pcm), original[44:])
            self.assertEqual(decoder.format, (24000, 1))

    def test_truncated_or_non_pcm_wave_fails(self):
        for data in (wav_bytes()[:-3], wav_bytes()[:32], b"not a wav" * 5):
            with self.subTest(length=len(data)), self.assertRaises(VoiceError):
                decoder = WavStreamDecoder()
                decoder.feed(data)
                decoder.finish()


class LocalServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.mode = "ok"
        self.requests = []
        self.sent_first = asyncio.Event()
        self.allow_done = asyncio.Event()
        self.disconnected = asyncio.Event()
        app = web.Application()
        app.router.add_get("/ws/tts", self.websocket)
        app.router.add_post("/tts", self.native)
        app.router.add_get("/set_gpt_weights", self.weights)
        app.router.add_get("/set_sovits_weights", self.weights)
        app.router.add_get("/engines/tts", self.engines)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.reference = self.root / "reference.wav"
        self.reference.write_bytes(wav_bytes())
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(json.dumps({"version": 1, "characters": {"test": {"gpt_weights": "g.ckpt", "sovits_weights": "s.pth",
            "emotions": {tag: {"reference_audio": str(self.reference), "prompt_text": "こんにちは。"}
                         for tag in ("neutral", "sad")}}}}), encoding="utf-8")
        self.config = VoiceConfig(enabled=True, provider="voicestudio", character="test", endpoint=self.url,
            catalog_path=str(self.catalog), cache_dir=str(self.root / "cache"), reuse_audio=False,
            denoise={"enabled": False}, voicestudio={"backend_endpoint": self.url})

    async def asyncTearDown(self):
        self.allow_done.set()
        await self.runner.cleanup()

    async def weights(self, request):
        self.requests.append(request.path)
        return web.json_response({"message": "success"})

    async def engines(self, request):
        self.requests.append(request.headers.get("Authorization"))
        return web.json_response([])

    async def native(self, request):
        body = await request.json()
        self.requests.append(body)
        if self.mode in ("early-recover", "early-fail"):
            seconds = .2 if body["seed"] == 42 or self.mode == "early-fail" else 2
            return web.Response(body=wav_bytes(seconds), content_type="audio/wav")
        return web.Response(body=wav_bytes(), content_type="audio/wav")

    async def websocket(self, request):
        if self.mode in ("503", "401"):
            return web.Response(status=int(self.mode))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        try:
            async for message in ws:
                self.requests.append(json.loads(message.data))
                if self.mode == "content-error":
                    await ws.send_json({"type": "error", "detail": "bad emotion"})
                    continue
                await ws.send_json({"type": "start", "sample_rate": 24000, "channels": 1, "format": "pcm16"})
                pcm = wav_bytes()[44:]
                await ws.send_bytes(pcm[:111])
                await ws.send_bytes(pcm[111:])
                self.sent_first.set()
                if self.mode == "truncated":
                    await ws.close()
                    break
                if self.mode == "hold":
                    await self.allow_done.wait()
                if self.mode == "cancel":
                    await ws.receive()
                    break
                if self.mode == "partial-sample":
                    await ws.send_bytes(b"x")
                await ws.send_json({"type": "done", "audio_bytes": len(pcm) + (1 if self.mode == "partial-sample" else 0)})
        finally:
            self.disconnected.set()
        return ws

    async def test_first_audio_delivered_before_done_and_saved_wave_is_complete(self):
        self.mode = "hold"
        progress = []
        async with aiohttp.ClientSession() as client:
            ws = await client.ws_connect(self.url + "/ws/tts")
            task = asyncio.create_task(voicestudio_stream(ws, {"text": "hello"}, self.root / "audio.wav", self.config,
                                                        progress=progress.append))
            await asyncio.wait_for(self.sent_first.wait(), 3)
            for _ in range(100):
                if progress:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(progress)
            self.assertFalse(task.done())
            self.allow_done.set()
            info = await asyncio.wait_for(task, 3)
            self.assertEqual(info["bytes"], 24000)
            await ws.close()
        with wave.open(str(self.root / "audio.wav")) as audio:
            self.assertEqual(audio.getnframes(), 12000)

    async def test_midstream_failure_is_not_ready_and_does_not_fallback(self):
        for mode in ("truncated", "partial-sample", "content-error"):
            self.mode = mode
            self.requests.clear()
            with self.subTest(mode=mode), self.assertRaises(VoiceError):
                await VoiceService(self.config).synthesize("こんにちは。", "neutral")
            self.assertEqual(len(self.requests), 1)
            self.assertNotIn("/set_gpt_weights", self.requests)
        self.assertTrue(all(json.loads(path.read_text(encoding="utf-8"))["state"] == "failed"
                            for path in (self.root / "cache").glob("*/manifest.json")))

    async def test_connection_unavailable_can_fallback_but_auth_failure_cannot(self):
        self.config.streaming = False
        self.mode = "503"
        result = await VoiceService(self.config).synthesize("こんにちは。", "neutral")
        self.assertEqual(result["effective_provider"], "gpt_sovits_v2")
        self.assertIn("fallback_reason", result)
        self.mode = "401"
        self.requests.clear()
        with self.assertRaises(aiohttp.WSServerHandshakeError):
            await VoiceService(self.config).synthesize("こんにちは。", "neutral")
        self.assertEqual(self.requests, [])

    async def test_cancel_closes_connection_and_keeps_cancelled_manifest(self):
        self.mode = "cancel"
        task = asyncio.create_task(VoiceService(self.config).synthesize("こんにちは。", "neutral"))
        await asyncio.wait_for(self.sent_first.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(self.disconnected.wait(), 3)
        manifest = json.loads(next((self.root / "cache").glob("*/manifest.json")).read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "cancelled")
        self.assertNotIn("silk_path", manifest)

    async def test_audio_cache_reuses_verified_files_and_recovers_from_corruption(self):
        self.config.reuse_audio = True
        service = VoiceService(self.config)
        first = await service.synthesize("こんにちは。", "neutral")
        second = await service.synthesize("こんにちは。", "neutral")
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertNotEqual(first["manifest_path"], second["manifest_path"])
        self.assertEqual(len(self.requests), 1)
        cache = next((self.root / "cache/artifacts").glob("*/voice.silk"))
        cache.write_bytes(b"broken")
        third = await service.synthesize("こんにちは。", "neutral")
        self.assertFalse(third.get("cache_hit", False))
        self.assertEqual(len(self.requests), 2)

    async def test_experimental_short_audio_is_retried_before_any_pcm_is_published(self):
        self.mode = "early-recover"
        catalog = json.loads(self.catalog.read_text(encoding="utf-8"))
        catalog["characters"]["test"]["experimental"] = True
        self.catalog.write_text(json.dumps(catalog), encoding="utf-8")
        self.config.provider = "gpt_sovits_v2"
        events = []
        result = await VoiceService(self.config).synthesize("さいごまできいてくれてありがとう。", "neutral", progress=events.append)
        checks = result["chunks"][0]["duration_checks"]
        self.assertEqual([c["suspected_early_end"] for c in checks], [True, False])
        bodies = [r for r in self.requests if isinstance(r, dict)]
        self.assertEqual([r["seed"] for r in bodies], [42, 17])
        self.assertEqual([r["parallel_infer"] for r in bodies], [True, False])
        audio_events = [e for e in events if e["type"] == "audio"]
        import base64
        self.assertEqual(sum(len(base64.b64decode(e["pcm_base64"])) for e in audio_events), 2*24000*2)
        self.assertTrue(any(e.get("stage") == "retrying" for e in events))

    async def test_repeated_short_audio_is_failed_without_silk_or_stream_commit(self):
        self.mode = "early-fail"
        catalog = json.loads(self.catalog.read_text(encoding="utf-8"))
        catalog["characters"]["test"]["experimental"] = True
        self.catalog.write_text(json.dumps(catalog), encoding="utf-8")
        self.config.provider = "gpt_sovits_v2"
        self.config.reuse_audio = True
        events = []
        with self.assertRaisesRegex(VoiceError, "连续生成异常短"):
            await VoiceService(self.config).synthesize("さいごまできいてくれてありがとう。", "neutral", progress=events.append)
        self.assertFalse(any(e["type"] == "audio" for e in events))
        self.assertFalse(list((self.root/"cache").rglob("*.silk")))
        manifest = json.loads(next((self.root/"cache").glob("*/manifest.json")).read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "failed")
        self.assertEqual(len(manifest["chunks"][0]["duration_checks"]), 2)

    async def test_remote_authenticated_http_uses_same_token_as_websocket(self):
        with patch.dict("os.environ", {"VOICESTUDIO_API_KEY": "test-token"}):
            service = VoiceService(self.config)
            async with aiohttp.ClientSession() as client:
                await service._request(client, "GET", "/engines/tts")
        self.assertEqual(self.requests, ["Bearer test-token"])

    async def test_full_voicestudio_profile_needs_no_local_model_or_reference(self):
        self.config.voicestudio.local_bridge = False
        self.config.voicestudio.engine = "other-engine"
        self.catalog.write_text(json.dumps({"version": 1, "characters": {"test": {"emotions": {
            "neutral": {"voicestudio_profile_id": "saved-profile", "voicestudio": {"emo_text": "happy", "emo_alpha": .8}}
        }}}}), encoding="utf-8")
        result = await VoiceService(self.config).synthesize("こんにちは。", "neutral")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(self.requests[0]["voice"], "saved-profile")
        self.assertEqual(self.requests[0]["engine"], "other-engine")
        self.assertEqual(self.requests[0]["emo_text"], "happy")
        self.assertEqual(self.requests[0]["emo_alpha"], .8)

    async def test_native_http_stream_is_valid_and_bound_by_duration(self):
        async with aiohttp.ClientSession() as client:
            info = await native_sovits_stream(client, self.url, {"text": "hello"}, self.root / "native.wav", self.config)
        self.assertEqual(info["bytes"], 24000)
        self.assertTrue(self.requests[0]["streaming_mode"])
        writer = PCMWriter(self.root / "limited.wav", self.config.model_copy(update={"max_duration_seconds": 1}))
        writer.start(24000, 1)
        try:
            with self.assertRaises(VoiceError):
                writer.write(b"\0" * 48002)
        finally:
            writer.close()


class ModelCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_selection_cache_is_bound_to_process_and_weight_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = {key: str(root / key) for key in ("gpt_weights", "sovits_weights")}
            for value in paths.values():
                Path(value).write_bytes(b"weights")
            request = AsyncMock(return_value=b'{"message":"success"}')
            identity = {"pid": 100, "create_time": 1}
            endpoint = "http://127.0.0.1:18967"
            with patch("voice_model_cache.engine_identity", return_value=identity):
                self.assertFalse((await select_models(paths, endpoint, request))["cache_hit"])
                self.assertTrue((await select_models(paths, endpoint, request))["cache_hit"])
                self.assertEqual(request.await_count, 2)
                Path(paths["gpt_weights"]).write_bytes(b"new weights")
                self.assertFalse((await select_models(paths, endpoint, request))["cache_hit"])
            with patch("voice_model_cache.engine_identity", return_value=None):
                self.assertFalse((await select_models(paths, endpoint, request))["cache_hit"])
                self.assertFalse((await select_models(paths, endpoint, request))["cache_hit"])
            self.assertEqual(request.await_count, 8)

    async def test_process_identity_rejects_reused_pid_and_other_command(self):
        state = {"pid": 10, "endpoint": "http://local", "create_time": 1, "python": "python.exe", "script": "api.py", "config": "infer.yaml"}
        process = Mock()
        process.cmdline.return_value = ["python.exe", "api.py", "infer.yaml"]
        process.exe.return_value = str(Path("python.exe").resolve())
        process.create_time.return_value = 1
        with patch("voice_model_cache.Path.read_text", return_value=json.dumps(state)), patch("psutil.Process", return_value=process):
            self.assertIsNotNone(engine_identity("http://local"))
            process.create_time.return_value = 2
            self.assertIsNone(engine_identity("http://local"))
            process.create_time.return_value = 1
            process.cmdline.return_value = ["python.exe", "unrelated.py"]
            self.assertIsNone(engine_identity("http://local"))


@unittest.skipUnless(shutil.which("ffmpeg"), "local ffmpeg required")
class DenoiseTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_filter_reduces_noise_preserves_voice_and_repairs_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "noisy.wav"
            randomizer = random.Random(42)
            values = array("h", (round(randomizer.uniform(-500, 500) + (4000 * math.sin(i * math.tau * 440 / 24000) if i >= 24000 else 0))
                                 for i in range(72000)))
            with wave.open(str(source), "wb") as stream:
                stream.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                stream.writeframes(values.tobytes())
            config = DenoiseConfig(strength_db=12, noise_floor_db=-35)
            target, first = await clean_reference(source, config, cache_root=root / "cache")
            _, second = await clean_reference(source, config, cache_root=root / "cache")
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])
            with wave.open(target) as stream:
                cleaned = array("h", stream.readframes(stream.getnframes()))
            def rms(data):
                return math.sqrt(sum(v*v for v in data) / len(data))
            self.assertLess(rms(cleaned[12000:18000]), rms(values[12000:18000]) * .9)
            self.assertGreater(rms(cleaned[36000:60000]), rms(values[36000:60000]) * .6)
            self.assertEqual(len(values), len(cleaned))
            Path(target).write_bytes(b"corrupted")
            _, repaired = await clean_reference(source, config, cache_root=root / "cache")
            self.assertFalse(repaired["cache_hit"])
            self.assertEqual(source.read_bytes()[44:], values.tobytes())

    async def test_separation_cache_skips_worker_and_corrupt_stem_is_recomputed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            song = root / "song.wav"
            song.write_bytes(wav_bytes())
            config = VoiceConfig(singing={"cache_dir": str(root / "cache"), "engine_directory": str(root / "engine")})
            service = SingingService(config)
            async def separate(stage, folder, values, **kwargs):
                for name in ("vocals", "instrumental"):
                    shutil.copyfile(song, values[name])
                return {name: values[name] for name in ("vocals", "instrumental")}
            manifest = {"inputs": {"song": {"sha256": file_hash(song)}}}
            with patch.object(service, "_worker", side_effect=separate) as worker:
                for index, expected in enumerate((False, True, False)):
                    folder = root / str(index)
                    folder.mkdir()
                    await service._separate_cached(song, folder, manifest)
                    self.assertEqual(manifest["separation"]["cache_hit"], expected)
                    if index == 1:
                        next((root / "cache/separation").glob("*/vocals.wav")).write_bytes(b"bad")
                self.assertEqual(worker.await_count, 2)
