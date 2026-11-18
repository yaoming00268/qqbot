"""Scoped song tasks and real local mix/codec work with deterministic DSP fixtures."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import contextlib
import io
import json
import math
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
from types import SimpleNamespace
import wave

from console_tools.broker import ConsoleBroker
from console_tools.cli import execute, main
from console_tools.contracts import capabilities, schema
from singing_config import SingingConfig
from singing_service import SingingError, SingingService, generation_lyrics
from voice_config import VoiceConfig
from voice_service import SILK_HEADER
from singing_commands import execute_singing


def wav(path, seconds=2):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        output.writeframes(b"".join(struct.pack("<h", int(4000 * math.sin(2 * math.pi * 220 * i / 24000)))
                                  for i in range(int(seconds * 24000))))
    return path


class SingingConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.song = wav(self.root / "歌曲 空格.wav")
        self.voice = VoiceConfig(singing=SingingConfig(enabled=True))
        self.metadata = {"namespace": "fixture", "scope": "chat", "sender_id": "member", "is_master": False,
            "source_message_id": "message", "output_dir": str(self.root), "artifact_roots": [str(self.root)],
            "voice_config": self.voice.model_dump()}
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.context = self.broker.issue_context(self.metadata, self.root / "context")

    def test_discovery_and_utf16_lyrics_cli_create_one_bound_task(self):
        self.assertIn("sing", schema()["actions"])
        self.assertEqual(schema("sing", "compose")["file_inputs"]["lyrics"], "--lyrics-file")
        lyrics = self.root / "歌词.txt"
        lyrics.write_text("[Verse]\nこんにちは\n明日も歌おう", encoding="utf-16")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["sing", "compose", "--context-file", self.context, "--song", str(self.song),
                  "--lyrics-file", str(lyrics), "--language", "ja", "--request-key", "one"])
        first = json.loads(output.getvalue())
        same = execute("sing", self.context, operation="compose", params={"song": str(self.song),
            "lyrics": lyrics.read_text(encoding="utf-16"), "language": "ja", "request_key": "one"})
        self.assertEqual(first["id"], same["id"])
        row = self.broker.record(first["id"])
        self.assertEqual(row["kind"], "singing")
        data = json.loads(row["result"])
        self.assertEqual(data["singing"]["mode"], "compose")
        self.assertIn("\n", data["singing"]["lyrics"])
        self.assertEqual(data["singing_inputs"]["song"]["path"], str(self.song.resolve()))
        self.assertNotIn("delivery", data)

    def test_work_policy_disabled_cannot_be_bypassed_by_sing_group(self):
        context = self.broker.issue_context({**self.metadata, "work_permissions": {
            "access": "all", "task_access": {"process": "disabled"}}}, self.root / "denied")
        with self.assertRaises(PermissionError):
            execute("sing", context, operation="cover", params={"song": str(self.song), "lyrics": "hello"})

    def test_file_ids_are_scoped_and_local_paths_must_be_authorized(self):
        key = self.broker.record_files("fixture", "other", "remote", "member", [{"native_id": "up", "name": "song.wav", "size": 10}])[0]
        with self.assertRaises(ValueError):
            execute("sing", self.context, operation="cover", params={"song": key, "lyrics": "hello"})
        outside = self.root.parent / (self.root.name + "-outside.wav")
        outside.write_bytes(b"audio")
        self.addCleanup(outside.unlink)
        with self.assertRaises(ValueError):
            execute("sing", self.context, operation="cover", params={"song": str(outside), "lyrics": "hello"})
        own = self.broker.record_files("fixture", "chat", "message", "member", [{"native_id": "up", "name": "song.wav", "size": 10}])[0]
        receipt = execute("sing", self.context, operation="cover", params={"song": "latest", "lyrics": "hello"})
        self.assertEqual(json.loads(self.broker.record(receipt["id"])["result"])["singing_inputs"]["song"]["file_id"], own)

    def test_restricted_contexts_never_advertise_or_admit_singing(self):
        for marker, value in [("emotion_analysis", True), ("agent_worker_id", "worker"), ("self_activity", True),
                              ("proactive_event", True), ("memory_maintenance", True), ("idle_event", True),
                              ("sandbox_session_id", "sandbox"), ("parent_request_id", "parent"), ("agent_event", True)]:
            with self.subTest(marker=marker):
                data = {**self.metadata, marker: value}
                self.assertNotIn("sing", capabilities(data))
                context = self.broker.issue_context(data, self.root / marker)
                with self.assertRaises((PermissionError, ValueError)):
                    execute("sing", context, operation="cover", params={"song": str(self.song), "lyrics": "hello"})


@unittest.skipUnless(shutil.which("ffmpeg"), "local ffmpeg required")
class SingingServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.song = wav(self.root / "input.wav")
        self.reference = wav(self.root / "reference.wav", 1.5)
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(json.dumps({"version": 1, "characters": {"test": {"emotions": {
            "neutral": {"reference_audio": str(self.reference)}}}}}), encoding="utf-8")
        self.config = VoiceConfig(character="test", catalog_path=str(self.catalog), singing=SingingConfig(
            enabled=True, cache_dir=str(self.root / "cache"), suspend_owned_tts=False))
        self.calls = []

    async def fixture_worker(self, stage, directory, values, **kwargs):
        self.calls.append((stage, values))
        if stage == "separate":
            shutil.copyfile(values["source"], values["vocals"])
            shutil.copyfile(values["source"], values["instrumental"])
            return {"vocals": values["vocals"], "instrumental": values["instrumental"]}
        output = directory / (stage + "-fixture.wav")
        shutil.copyfile(values["source"], output)
        return {"path": str(output)}

    async def test_cover_pipeline_preserves_lyrics_and_produces_real_mp3_and_silk(self):
        service = SingingService(self.config)
        with patch.object(service, "_worker", side_effect=self.fixture_worker):
            result = await service.render({"song": str(self.song), "lyrics": "[00:01.00]原词\n第二行", "song_output": "both"})
        self.assertEqual([s for s, v in self.calls], ["separate", "convert"])
        self.assertEqual([f["kind"] for f in result["files"]], ["file", "voice"])
        self.assertTrue(Path(result["silk_path"]).read_bytes().startswith(SILK_HEADER))
        self.assertGreater(Path(result["mp3_path"]).stat().st_size, 2000)
        self.assertEqual(Path(result["lyrics_path"]).read_text(encoding="utf-8"), "[00:01.00]原词\n第二行")
        with wave.open(result["wav_path"]) as audio:
            self.assertEqual((audio.getnchannels(), audio.getframerate()), (2, 44100))
            self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), 2, places=2)
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "ready")
        self.assertEqual(len(manifest["inputs"]["song"]["sha256"]), 64)

    async def test_compose_generates_before_conversion_and_japanese_lyrics_are_kana(self):
        service = SingingService(self.config)
        with patch.object(service, "_worker", side_effect=self.fixture_worker):
            result = await service.render({"mode": "compose", "song": str(self.song),
                "lyrics": "[ti:歌]\n[Verse]\n[00:01.20]明日も歌おう", "language": "ja"})
        self.assertEqual([s for s, v in self.calls], ["generate", "convert"])
        lyrics = self.calls[0][1]["lyrics"]
        self.assertIn("[Verse]", lyrics)
        self.assertIn("あした", lyrics)
        self.assertNotRegex(lyrics, r"[\u3400-\u9fff]")
        self.assertNotIn("00:01", lyrics)
        self.assertEqual([f["kind"] for f in result["files"]], ["file"])

    async def test_background_result_is_registered_under_media_roots_and_waits_for_deliver(self):
        from config import MediaConfig
        from console_tools.files import outgoing_files
        from media_jobs import MediaService
        from media_models import MessageEnvelope
        from skills.base import SkillContext
        service = SingingService(self.config)
        with patch.object(service, "_worker", side_effect=self.fixture_worker):
            rendered = await service.render({"song": str(self.song), "lyrics": "hello", "song_output": "both"})
        broker = ConsoleBroker(self.root / "jobs.sqlite3")
        metadata = {"namespace": "test", "scope": "test", "sender_id": "member", "output_dir": str(self.root),
                    "artifact_roots": [str(self.root)], "voice_config": self.config.model_dump()}
        context = broker.issue_context(metadata, self.root / "contexts")
        receipt = execute("sing", context, operation="cover", params={"song": str(self.song), "lyrics": "hello", "song_output": "both"})
        request = broker.record(receipt["id"])
        broker.update(request["id"], "running")
        media = MediaService(MediaConfig(enabled=True, file_enabled=True, cache_dir=str(self.root / "media"),
                                          database_path=str(self.root / "media.sqlite3")))
        sender = AsyncMock()
        ctx = MessageEnvelope("onebot", "test", "chat", "member", "message", "唱歌", sender)
        worker = MessageEnvelope("onebot", "test", "chat", "member", "message:singing", "唱歌", sender)
        skill = SkillContext("work:test", "chat", "member", media_context=worker, media_service=media)
        main = SimpleNamespace(broker=broker, client=SimpleNamespace(app_config=SimpleNamespace(voice=self.config)))
        with patch("singing_service.SingingService.render", new=AsyncMock(return_value=rendered)):
            result = await execute_singing(main, request, ctx, metadata, json.loads(request["result"]), skill)
        self.assertTrue(result.success)
        self.assertEqual(result.data["state"], "ready")
        self.assertTrue(result.data["defer_to_console"])
        self.assertEqual([f["kind"] for f in result.data["files"]], ["file", "voice"])
        self.assertEqual(result.data["files"][1]["mime"], "audio/silk")
        self.assertTrue(all(Path(f["local_path"]).resolve().is_relative_to(Path(media.config.cache_dir).resolve()) for f in result.data["files"]),
                        (media.config.cache_dir, [f["local_path"] for f in result.data["files"]]))
        sender.assert_not_called()

    async def test_too_long_audio_rejected_before_dsp_and_never_silently_truncated(self):
        wav(self.song, 6)
        self.config.singing.max_duration_seconds = 5
        service = SingingService(self.config)
        with patch.object(service, "_worker", side_effect=self.fixture_worker), self.assertRaises(SingingError):
            await service.render({"song": str(self.song), "lyrics": "hello"})
        self.assertFalse(self.calls)
        manifest = next((self.root / "cache").glob("*/manifest.json"))
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["state"], "failed")

    async def test_conversion_failure_does_not_claim_ready_or_leave_tts_stopped(self):
        self.config.singing.suspend_owned_tts = True
        service = SingingService(self.config)
        async def fail(*args, **kwargs):
            raise SingingError("fixture conversion failure")
        with (patch.object(service, "_worker", side_effect=fail), patch("voice_engine.engine_status", return_value={
                "owned": True, "running": True, "endpoint": self.config.endpoint}), patch("voice_engine.stop_engine") as stop,
                patch("voice_engine.start_engine") as start, self.assertRaises(SingingError)):
            await service.render({"song": str(self.song), "lyrics": "hello", "vocals_only": True})
        stop.assert_called_once()
        start.assert_called_once()

    async def test_cancellation_is_durable_and_does_not_run_following_stages(self):
        service = SingingService(self.config)
        entered = asyncio.Event()
        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(service, "_worker", side_effect=wait):
            task = asyncio.create_task(service.render({"song": str(self.song), "lyrics": "hello"}))
            await asyncio.wait_for(entered.wait(), 15)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        manifest = next((self.root / "cache").glob("*/manifest.json"))
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(data["state"], "cancelled")
        self.assertNotIn("result", data)

    def test_empty_metadata_only_lyrics_and_unknown_emotion_are_not_fallbacks(self):
        with self.assertRaises(SingingError):
            generation_lyrics("[ti:Test]\n[Verse]", "ja", {})
