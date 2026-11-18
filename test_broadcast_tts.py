"""Exercise live HTTP/WS audio without QQ encoding, real models or accounts."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import base64
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import aiohttp
import wave

from aiohttp import web

from broadcast_tts import BroadcastVoiceService
from voice_config import BroadcastVoiceConfig
from voice_text import VoiceError


def wav_bytes(seconds=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\xe8\x03" * round(24000 * seconds))
    return output.getvalue()


class LiveTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mode, self.requests, self.events = "normal", [], []
        self.first, self.finish = asyncio.Event(), asyncio.Event()
        catalog = {"version": 1, "characters": {"live": {
            "gpt_weights": "live.ckpt", "sovits_weights": "live.pth",
            "emotions": {"neutral": {"reference_audio": "ref.wav", "prompt_text": "こんにちは。"},
                         "sad": {"reference_audio": "sad.wav", "prompt_text": "さびしい。"}}}}}
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(json.dumps(catalog), encoding="utf-8")
        app = web.Application()
        app.router.add_get("/set_gpt_weights", self.select)
        app.router.add_get("/set_sovits_weights", self.select)
        app.router.add_post("/tts", self.native)
        app.router.add_get("/ws/tts", self.websocket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.config = BroadcastVoiceConfig(enabled=True, character="live", endpoint=self.url,
            catalog_path=str(self.catalog), cache_dir=str(self.root / "live-cache"), denoise={"enabled": False})
        self.config.voicestudio.backend_endpoint = self.url
        for item in (patch("voice_service.encode_silk", side_effect=AssertionError("live must never encode SILK")),
                     patch("voice_service.VoiceService.synthesize", side_effect=AssertionError("QQ synthesis invoked"))):
            item.start()
            self.addCleanup(item.stop)

    async def asyncTearDown(self):
        self.finish.set()
        await self.runner.cleanup()

    async def select(self, request):
        self.requests.append(request.path)
        return web.json_response({"message": "success"})

    async def native(self, request):
        body = await request.json()
        self.requests.append(body)
        seconds = .2 if self.mode == "guarded" and body["seed"] == 42 else 2
        raw = wav_bytes(seconds)
        if self.mode == "hold":
            response = web.StreamResponse(headers={"Content-Type": "audio/wav"})
            await response.prepare(request)
            await response.write(raw[:4000])
            await self.finish.wait()
            await response.write(raw[4000:])
            return response
        return web.Response(body=raw[:-1] if self.mode == "truncated" else raw, content_type="audio/wav")

    async def websocket(self, request):
        if self.mode == "unauthorized":
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            self.requests.append(json.loads(message.data))
            await ws.send_json({"type": "start", "sample_rate": 24000, "channels": 1, "format": "pcm16"})
            await ws.send_bytes(wav_bytes()[44:])
            if self.mode == "truncated":
                await ws.close()
                break
            await ws.send_json({"type": "done", "audio_bytes": 48000})
        return ws

    async def sink(self, event):
        self.events.append(event)
        self.first.set()

    def experimental(self):
        catalog = json.loads(self.catalog.read_text(encoding="utf-8"))
        catalog["characters"]["live"]["experimental"] = True
        self.catalog.write_text(json.dumps(catalog), encoding="utf-8")

    async def test_native_first_pcm_precedes_http_completion_without_silk(self):
        self.mode = "hold"
        task = asyncio.create_task(BroadcastVoiceService(self.config).synthesize("こんにちは。", "neutral", audio_sink=self.sink))
        try:
            await asyncio.wait_for(self.first.wait(), 3)
            self.assertFalse(task.done())
        finally:
            self.finish.set()
        result = await asyncio.wait_for(task, 3)
        self.assertEqual(result["pipeline"], "broadcast")
        self.assertEqual(result["state"], "ready")
        self.assertNotIn("silk_path", result)
        self.assertFalse(list(self.root.rglob("*.silk")))
        body = next(row for row in self.requests if isinstance(row, dict))
        self.assertIs(body["streaming_mode"], True)
        self.assertEqual(b"".join(base64.b64decode(e["pcm_base64"]) for e in self.events), wav_bytes(2)[44:])

    async def test_native_truncation_fails_instead_of_returning_ready(self):
        self.mode = "truncated"
        with self.assertRaises(VoiceError):
            await BroadcastVoiceService(self.config).synthesize("こんにちは。", "neutral", audio_sink=self.sink)
        saved = json.loads(next((self.root / "live-cache").glob("*/manifest.json")).read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(len([r for r in self.requests if isinstance(r, dict)]), 1)

    async def test_guarded_streaming_does_not_retry_after_audible_prefix(self):
        self.experimental()
        self.mode = "guarded"
        with self.assertRaisesRegex(VoiceError, "不会重播"):
            await BroadcastVoiceService(self.config).synthesize("さいごまできいてくれてありがとう。", "neutral", audio_sink=self.sink)
        self.assertTrue(self.events)
        self.assertEqual(len([r for r in self.requests if isinstance(r, dict)]), 1)

    async def test_validated_live_retries_before_emitting_and_still_skips_qq_encoding(self):
        self.experimental()
        self.mode = "guarded"
        self.config.delivery_mode = "validated"
        async def convert(config, source, target):
            self.assertFalse(self.events)
            shutil.copyfile(source, target)
        with patch("broadcast_tts.run_ffmpeg", side_effect=convert):
            result = await BroadcastVoiceService(self.config).synthesize("さいごまできいてくれてありがとう。", "neutral", audio_sink=self.sink)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(len([r for r in self.requests if isinstance(r, dict)]), 2)
        self.assertEqual(b"".join(base64.b64decode(e["pcm_base64"]) for e in self.events), wav_bytes(2)[44:])
        self.assertFalse(list(self.root.rglob("*.silk")))

    async def test_voice_studio_disconnect_after_audio_never_falls_back_to_native(self):
        self.mode = "truncated"
        self.config.provider = "voicestudio"
        with self.assertRaisesRegex(VoiceError, "断开"):
            await BroadcastVoiceService(self.config).synthesize("こんにちは。", "neutral", audio_sink=self.sink)
        self.assertTrue(self.events)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("streaming_mode", self.requests[0])

    async def test_startup_availability_failure_falls_back_before_audio(self):
        self.config.provider = "voicestudio"
        self.config.auto_start = True
        for error in (VoiceError("fixture bridge exited"), aiohttp.ClientConnectionError("fixture"), TimeoutError()):
            seen = []
            async def ready(config):
                seen.append(config.provider)
                if config.provider == "voicestudio":
                    raise error
            with self.subTest(error=type(error).__name__), patch("broadcast_tts.ready", side_effect=ready):
                result = await BroadcastVoiceService(self.config).synthesize("こんにちは。", "neutral", audio_sink=self.sink)
                self.assertEqual(seen, ["voicestudio", "gpt_sovits_v2"])
                self.assertEqual(result["effective_provider"], "gpt_sovits_v2")
                self.assertTrue(self.events)

    async def test_disabled_or_remote_fallback_propagates_startup_failure(self):
        self.config.provider = "voicestudio"
        self.config.auto_start = True
        for local, enabled in ((True, False), (False, True)):
            self.config.voicestudio.local_bridge = local
            self.config.voicestudio.fallback_enabled = enabled
            with patch("broadcast_tts.ready", side_effect=VoiceError("fixture startup")) as ready:
                with self.assertRaisesRegex(VoiceError, "fixture startup"):
                    await BroadcastVoiceService(self.config).synthesize("こんにちは。", "neutral", audio_sink=self.sink)
                self.assertEqual(ready.call_count, 1)
                self.assertFalse(self.events)

    async def test_authentication_failure_never_falls_back(self):
        self.config.provider = "voicestudio"
        self.mode = "unauthorized"
        with self.assertRaises(aiohttp.WSServerHandshakeError):
            await BroadcastVoiceService(self.config).synthesize("こんにちは。", "neutral", audio_sink=self.sink)
        self.assertFalse(self.events)
        self.assertFalse(self.requests)

    async def test_multi_emotion_live_requests_use_selected_reference_and_keep_tags_silent(self):
        await BroadcastVoiceService(self.config).synthesize("こんにちは[emotion:sad]さびしい。", "neutral", audio_sink=self.sink)
        bodies = [r for r in self.requests if isinstance(r, dict)]
        self.assertEqual(len(bodies), 2)
        self.assertEqual([Path(b["ref_audio_path"]).name for b in bodies], ["ref.wav", "sad.wav"])
        self.assertNotIn("emotion:", "".join(b["text"] for b in bodies))
        self.assertEqual({e["emotion"] for e in self.events}, {"neutral", "sad"})


if __name__ == "__main__":
    unittest.main()
