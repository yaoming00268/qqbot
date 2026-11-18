"""Offline live-only playback, buffering, format, emotion and cancellation tests."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import base64
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import broadcast_speech as speech
from voice_config import VoiceConfig, BroadcastVoiceConfig
from voice_synthesis import join_chunks


def wav_bytes(seconds=.16, value=1000, rate=24000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(value.to_bytes(2, "little", signed=True) * round(seconds * rate) * channels)
    return output.getvalue()


def pcm_event(index=0, *, seconds=.16, value=1000, emotion="neutral", pause=0, rate=24000, channels=1):
    return {"type": "audio", "index": index, "format": "pcm16", "sample_rate": rate,
            "channels": channels, "emotion": emotion, "pause_before_ms": pause,
            "pcm_base64": base64.b64encode(wav_bytes(seconds, value, rate, channels)[44:]).decode()}


class AssemblyTests(unittest.TestCase):
    def test_incremental_pcm_preserves_crossfades_and_explicit_sentence_pauses(self):
        with tempfile.TemporaryDirectory() as folder:
            root, config = Path(folder), BroadcastVoiceConfig()
            chunks = [SimpleNamespace(pause_before_ms=pause) for pause in (0, 0, 120, 0)]
            assembler, actual = speech._PCMAssembler(config), []
            for index, chunk in enumerate(chunks):
                raw = wav_bytes(.13 + .013 * index, 1000 + 600 * index)
                (root / f"chunk-{index:03}.wav").write_bytes(raw)
                for offset in range(44, len(raw), 100):
                    event = {**pcm_event(index, pause=chunk.pause_before_ms),
                             "pcm_base64": base64.b64encode(raw[offset:offset + 100]).decode()}
                    actual.extend(data for data, _ in assembler.feed(event))
            actual.extend(data for data, _ in assembler.end())
            final, _ = join_chunks(chunks, root, config)
            with wave.open(str(final), "rb") as audio:
                self.assertEqual(b"".join(actual), audio.readframes(audio.getnframes()))
            self.assertTrue(all(len(block) <= 1920 for block in actual))

    def test_inline_emotions_map_to_supported_avatar_values(self):
        for label, expected in (("喜び", "happy"), ("sad-soft", "sad"), ("羞涩", "shy"), ("不明", "neutral")):
            self.assertEqual(speech.avatar_emotion(label), expected)


class SpeechTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.voice = BroadcastVoiceConfig(enabled=True)
        self.avatar = SimpleNamespace(host="127.0.0.1", port=18765)
        self.config = SimpleNamespace(voice=VoiceConfig(enabled=False), broadcast_voice=self.voice, vtuber=self.avatar)
        self.calls, self.played = [], asyncio.Event()
        self.loop = asyncio.get_running_loop()
        for item in (patch.object(speech, "ensure_server", return_value={}),
                     patch.object(speech, "execute", side_effect=self.execute),
                     patch("voice_service.VoiceService.synthesize", side_effect=AssertionError("QQ pipeline invoked")),
                     patch("voice_service.encode_silk", side_effect=AssertionError("SILK invoked"))):
            item.start()
            self.addCleanup(item.stop)

    def execute(self, action, payload, config, **kwargs):
        self.calls.append((action, payload))
        if action == "pcm":
            self.loop.call_soon_threadsafe(self.played.set)
        return {"clients": 1, "event_id": len(self.calls)}

    def output(self):
        return {"state": "ready", "id": "fixture", "metrics": {"first_audio_ms": 20}}

    def pcm(self):
        return b"".join(base64.b64decode(p["pcm_base64"]) for a, p in self.calls if a == "pcm")

    async def run_fixture(self, synthesize, params=None):
        with patch.object(speech.BroadcastVoiceService, "synthesize", new=synthesize):
            return await speech.speak(self.config, params or {"text": "こんにちは。", "emotion": "neutral"})

    async def test_first_pcm_plays_before_synthesis_finishes_even_with_qq_disabled(self):
        owner = self
        async def synthesize(service, text, emotion, **kwargs):
            owner.assertIs(service.config, owner.voice)
            await kwargs["audio_sink"](pcm_event())
            await asyncio.wait_for(owner.played.wait(), 2)
            await kwargs["audio_sink"](pcm_event(1, emotion="sad", value=2000))
            return owner.output()
        result = await self.run_fixture(synthesize)
        self.assertEqual(result["pipeline"], "broadcast")
        self.assertEqual(result["metrics"]["broadcast_source"], "live-streaming")
        self.assertEqual(self.calls[-1], ("event", {"type": "pcm_end"}))
        self.assertIn("sad", [p["emotion"] for a, p in self.calls if a == "pcm"])
        self.assertFalse(any(a == "audio" for a, _ in self.calls))

    async def test_preserves_native_sample_rate_and_stereo_without_full_wav_conversion(self):
        owner = self
        async def synthesize(service, text, emotion, **kwargs):
            await kwargs["audio_sink"](pcm_event(rate=32000, channels=2))
            return owner.output()
        await self.run_fixture(synthesize)
        self.assertEqual(self.pcm(), wav_bytes(rate=32000, channels=2)[44:])
        self.assertTrue(all((p["sample_rate"], p["channels"]) == (32000, 2) for a, p in self.calls if a == "pcm"))

    async def test_failure_after_audible_pcm_stops_without_replay_or_completion(self):
        owner = self
        async def synthesize(service, text, emotion, **kwargs):
            await kwargs["audio_sink"](pcm_event())
            await owner.played.wait()
            raise ValueError("fixture failure")
        with self.assertRaisesRegex(ValueError, "fixture failure"):
            await self.run_fixture(synthesize)
        self.assertEqual(self.calls[-1], ("event", {"type": "speech_stop"}))
        self.assertFalse(any(a == "audio" or p.get("type") == "pcm_end" for a, p in self.calls))

    async def test_invalid_and_missing_pcm_are_not_reported_as_complete(self):
        owner = self
        for event in (pcm_event(1), {**pcm_event(), "pcm_base64": "AA=="}):
            async def synthesize(service, text, emotion, **kwargs):
                await kwargs["audio_sink"](event)
                return owner.output()
            with self.assertRaises(ValueError):
                await self.run_fixture(synthesize)
            self.assertEqual(self.calls[-1], ("event", {"type": "speech_stop"}))
        self.assertFalse(any(a == "pcm" for a, _ in self.calls))

    async def test_slow_browser_applies_backpressure_and_bounds_queue(self):
        gate, entered, done = threading.Event(), asyncio.Event(), asyncio.Event()
        original = self.execute
        def slow(action, payload, config, **kwargs):
            if action == "pcm":
                self.loop.call_soon_threadsafe(entered.set)
                gate.wait(2)
            return original(action, payload, config, **kwargs)
        owner = self
        self.voice.queue_blocks = 2
        async def synthesize(service, text, emotion, **kwargs):
            for _ in range(12):
                await kwargs["audio_sink"](pcm_event(seconds=.04))
            done.set()
            return owner.output()
        with patch.object(speech, "execute", side_effect=slow):
            task = asyncio.create_task(self.run_fixture(synthesize))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertFalse(done.is_set())
                self.assertFalse(task.done())
            finally:
                gate.set()
            result = await asyncio.wait_for(task, 3)
        self.assertLessEqual(result["metrics"]["broadcast_max_queue_blocks"], 2)

    async def test_cancellation_drains_inflight_pcm_before_closing_mouth(self):
        gate, entered = threading.Event(), asyncio.Event()
        original = self.execute
        def slow(action, payload, config, **kwargs):
            if action == "pcm":
                self.loop.call_soon_threadsafe(entered.set)
                gate.wait(2)
            return original(action, payload, config, **kwargs)
        async def synthesize(service, text, emotion, **kwargs):
            await kwargs["audio_sink"](pcm_event())
            await asyncio.Event().wait()
        with patch.object(speech, "execute", side_effect=slow):
            task = asyncio.create_task(self.run_fixture(synthesize))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.calls[-1], ("event", {"type": "speech_stop"}))

    async def test_live_sample_clock_bounds_player_ahead_time(self):
        now, sleeps = [100.0], []
        async def advance(delay):
            sleeps.append(delay)
            now[0] += delay
        clock = speech._Clock(now=lambda: now[0], sleep=advance)
        for _ in range(100):
            await clock.pace(.04, .5)
            self.assertLessEqual(clock.submitted(.04), .501)
        self.assertGreater(sum(sleeps), 3)

    async def test_explicit_avatar_default_and_inline_emotion_mapping(self):
        owner = self
        async def synthesize(service, text, emotion, **kwargs):
            await kwargs["audio_sink"](pcm_event())
            await kwargs["audio_sink"](pcm_event(1, emotion="悲伤"))
            return owner.output()
        await self.run_fixture(synthesize, {"text": "hello[emotion:悲伤]bye", "emotion": "neutral", "avatar_emotion": "happy"})
        emotions = [p["emotion"] for a, p in self.calls if a == "pcm"]
        self.assertEqual(emotions[0], "happy")
        self.assertIn("sad", emotions)
        self.assertNotIn("[emotion:", self.calls[0][1]["text"])


if __name__ == "__main__":
    unittest.main()
