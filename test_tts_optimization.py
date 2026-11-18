"""TTS latency changes retain validated PCM, cancellation and reading accuracy."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import base64
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

from aiohttp import web
import aiohttp

from voice_config import VoiceConfig
from voice_pcm import copy_ready_pcm
from voice_service import VoiceService
from voice_stream import completed_pcm_events
from voice_text import VoiceError, _japanese_tokens_cached, japanese_tokens


def wav_data(*, rate=24000, channels=1, seconds=2, value=1000):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(struct.pack("<h", value) * round(rate * seconds) * channels)
    return output.getvalue()


class CompletedPCMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.target = self.root / "source.wav", self.root / "target.wav"
        self.config = VoiceConfig()

    def test_completed_pcm_is_copied_without_altering_any_sample(self):
        original = wav_data()
        self.source.write_bytes(original)
        self.assertTrue(copy_ready_pcm(self.source, self.target, self.config))
        self.assertEqual(self.target.read_bytes(), original)

    def test_resampling_or_downmixing_still_requires_conversion(self):
        for rate, channels in ((32000, 1), (24000, 2)):
            self.source.write_bytes(wav_data(rate=rate, channels=channels))
            self.assertFalse(copy_ready_pcm(self.source, self.target, self.config))
            self.assertFalse(self.target.exists())

    def test_truncated_payload_never_uses_fast_copy(self):
        self.source.write_bytes(wav_data()[:-3])
        with self.assertRaisesRegex(VoiceError, "中断|不完整"):
            copy_ready_pcm(self.source, self.target, self.config)
        self.assertFalse(self.target.exists())

    def test_empty_and_over_duration_outputs_are_rejected(self):
        self.config.max_duration_seconds = 1
        for seconds in (0, 2):
            self.source.write_bytes(wav_data(seconds=seconds))
            with self.assertRaises(VoiceError):
                copy_ready_pcm(self.source, self.target, self.config)

    def test_replay_preserves_every_processed_sample_and_block_timing(self):
        original = wav_data(seconds=.273)
        self.source.write_bytes(original)
        events = list(completed_pcm_events(self.source, self.config, index=3, first_audio_ms=731))
        self.assertEqual(b"".join(base64.b64decode(e["pcm_base64"]) for e in events), original[44:])
        self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(e["sample_rate"] == 24000 and e["index"] == 3 and e["first_audio_ms"] == 731 for e in events))
        self.assertTrue(all(len(base64.b64decode(e["pcm_base64"])) <= 3840 for e in events))


class ReadingCacheTests(unittest.TestCase):
    def setUp(self):
        _japanese_tokens_cached.cache_clear()

    def test_dictionary_order_is_canonical_and_results_are_not_shared_lists(self):
        first = japanese_tokens("ATRIと図書館へ。", {"ATRI": "あとり", "図書館": "としょかん"})
        expected = list(first)
        first.clear()
        second = japanese_tokens("ATRIと図書館へ。", {"図書館": "としょかん", "ATRI": "あとり"})
        self.assertEqual(second, expected)
        self.assertEqual(_japanese_tokens_cached.cache_info().hits, 1)

    def test_changed_dictionary_is_not_a_stale_cache_hit(self):
        self.assertEqual("".join(japanese_tokens("名。", {"名": "な"})), "な。")
        self.assertEqual("".join(japanese_tokens("名。", {"名": "めい"})), "めい。")
        self.assertEqual(_japanese_tokens_cached.cache_info().misses, 2)

    def test_equal_length_dictionary_entries_preserve_replacement_order(self):
        self.assertEqual("".join(japanese_tokens("あ", {"い": "う", "あ": "い"})), "い")

    def test_unsupported_characters_still_fail_source_coverage(self):
        with self.assertRaises(VoiceError):
            japanese_tokens("こんにちは😀")
        self.assertEqual(_japanese_tokens_cached.cache_info().currsize, 0)


class GuardedReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reference = self.root / "reference.wav"
        self.reference.write_bytes(wav_data())
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(json.dumps({"version": 1, "characters": {"test": {
            "experimental": True, "gpt_weights": "g.ckpt", "sovits_weights": "s.pth",
            "emotions": {"neutral": {"reference_audio": str(self.reference), "prompt_text": "こんにちは。"}}}}}), encoding="utf-8")
        app = web.Application()
        self.native_connections = []
        async def synthesize(request):
            self.native_connections.append(request.transport)
            return web.Response(body=wav_data(rate=32000, value=1000))
        app.router.add_post("/tts", synthesize)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.config = VoiceConfig(enabled=True, character="test", endpoint=url,
            catalog_path=str(self.catalog), cache_dir=str(self.root / "cache"), reuse_audio=False,
            denoise={"enabled": False})

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_guard_publishes_validated_processed_pcm_instead_of_transport_pcm(self):
        events = []
        async def convert(config, source, target):
            self.assertFalse(any(e["type"] == "audio" for e in events))
            target.write_bytes(wav_data(value=2500))
        with patch("voice_synthesis.select_models", new=AsyncMock(return_value={})), patch("voice_service.run_ffmpeg", side_effect=convert):
            result = await VoiceService(self.config).synthesize("さいごまできいてくれてありがとう。", "neutral", progress=events.append)
        audio = [e for e in events if e["type"] == "audio"]
        self.assertEqual(b"".join(base64.b64decode(e["pcm_base64"]) for e in audio), wav_data(value=2500)[44:])
        self.assertTrue(all(e["sample_rate"] == 24000 for e in audio))
        self.assertEqual(result["chunks"][0]["pcm_processing"], "ffmpeg")

    async def test_cancellation_after_conversion_never_publishes_guarded_audio(self):
        events, cancelled = [], False
        async def convert(config, source, target):
            nonlocal cancelled
            target.write_bytes(wav_data())
            cancelled = True
        def current():
            if cancelled:
                raise asyncio.CancelledError()
        with patch("voice_synthesis.select_models", new=AsyncMock(return_value={})), patch("voice_service.run_ffmpeg", side_effect=convert):
            with self.assertRaises(asyncio.CancelledError):
                await VoiceService(self.config).synthesize("さいごまできいてくれてありがとう。", "neutral",
                    check_current=current, progress=events.append)
        self.assertFalse(any(e["type"] == "audio" for e in events))
        self.assertFalse(list(self.root.rglob("*.silk")))

    async def test_bridge_keeps_one_upstream_connection_across_phrase_requests(self):
        from voicestudio_bridge import create_app
        class Backend:
            id = "gpt-sovits"
            def _check_language(self, language):
                return language
        package = ModuleType("services")
        package.__path__ = []
        modules = {"services": package,
            "services.tts_backend": SimpleNamespace(GPTSoVITSBackend=Backend),
            "services.sentence_chunker": SimpleNamespace(SentenceChunker=object),
            "services.expression": SimpleNamespace(expression_caps=lambda cls: {})}
        config = self.config.model_copy(deep=True)
        config.voicestudio.backend_endpoint = self.config.endpoint
        with patch.dict(sys.modules, modules), patch("voicestudio_bridge.select_models", new=AsyncMock(return_value={})):
            runner = web.AppRunner(create_app(config))
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
            try:
                async with aiohttp.ClientSession() as client:
                    async with client.ws_connect(url + "/ws/tts") as ws:
                        for _ in range(2):
                            await ws.send_json({"text": "こんにちは。", "voice": "qqbot:test:neutral", "language": "ja"})
                            while True:
                                message = await ws.receive(timeout=5)
                                if message.type == aiohttp.WSMsgType.TEXT:
                                    event = json.loads(message.data)
                                    self.assertNotEqual(event["type"], "error", event)
                                    if event["type"] == "done":
                                        break
            finally:
                await runner.cleanup()
        self.assertEqual(len(self.native_connections), 2)
        self.assertIs(self.native_connections[0], self.native_connections[1])


if __name__ == "__main__":
    unittest.main()
