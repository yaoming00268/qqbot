"""Offline voice protocol, immutable reply and QQ record delivery regressions."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

from config import AppConfig, MediaConfig
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.files import ConsoleFileError
from console_tools.server import handle, tools_schema
from media_models import MediaAsset, MessageEnvelope
from media_sender import MediaSender
from messaging import SendUnknown
from voice_config import VoiceConfig
from voice_service import SILK_HEADER, VoiceService, encode_silk
from voice_text import VoiceError, prepare_chunks


def fixture_wav():
    data = io.BytesIO()
    with wave.open(data, "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        output.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i * 2 * math.pi * 440 / 24000))) for i in range(12000)))
    return data.getvalue()


class FrontendTests(unittest.TestCase):
    def test_japanese_is_kana_including_numbers_letters_and_names(self):
        config = VoiceConfig(dictionary={"ATRI": "あとり", "夏生": "なつき"}, chunk_chars=20)
        chunks = prepare_chunks("夏生さんとATRI、2026年11月14日に本を読みます。", config)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertNotRegex(chunk.text, r"[A-Za-z0-9\u3400-\u9fff]")
            self.assertLessEqual(len(chunk.text), 20)
        self.assertIn("なつき", "".join(chunk.text for chunk in chunks))
        with self.assertRaises(VoiceError):
            prepare_chunks("読めない𠮷", config)

    def test_mixed_explicit_spans_preserve_chinese_and_english(self):
        chunks = prepare_chunks("[ja]一緒に本を読みます。[/ja][zh]一起看书吧。[/zh][en]Let's read.[/en]", VoiceConfig(default_language="mixed"))
        self.assertEqual([c.language for c in chunks], ["ja", "zh", "en"])
        self.assertNotRegex(chunks[0].text, r"[\u3400-\u9fff]")
        self.assertEqual(chunks[1].text, "一起看书吧。")
        self.assertEqual(chunks[2].text, "Let's read.")
        for text in ("[ja]未完成", "正文[ja]ほん[/ja]", "[ja][en]nested[/en][/ja]"):
            with self.subTest(text=text), self.assertRaises(VoiceError):
                prepare_chunks(text, VoiceConfig(default_language="mixed"))

    def test_auto_mixed_keeps_japanese_kanji_with_kana_sentence(self):
        chunks = prepare_chunks("今日は本を読みます。你好。Hello!", VoiceConfig(default_language="mixed"))
        self.assertEqual([c.language for c in chunks], ["ja", "zh", "en"])

    def test_real_silk_encode_decode_round_trip(self):
        from pysilk import decode
        with tempfile.TemporaryDirectory() as directory:
            wav, silk = Path(directory) / "test.wav", Path(directory) / "test.silk"
            wav.write_bytes(fixture_wav())
            digest = encode_silk(wav, silk)
            self.assertTrue(silk.read_bytes().startswith(SILK_HEADER))
            self.assertEqual(digest, hashlib.sha256(silk.read_bytes()).hexdigest())
            pcm = io.BytesIO()
            decode(io.BytesIO(silk.read_bytes()), pcm, sample_rate=24000)
            self.assertGreater(len(pcm.getvalue()), 20000)


class ReplyVoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.payload = {"namespace": "fixture", "scope": "private", "sender_id": "1", "is_group": False,
            "transport": "official", "reply_slots": 1, "voice": {"enabled": True, "language": "ja", "emotions": ["neutral", "sad"]}}
        self.context = self.broker.issue_context(self.payload, self.root / "contexts")
        _, self.token = ConsoleBroker.from_context(self.context)

    def test_voice_metadata_is_committed_and_idempotency_includes_emotion(self):
        args = dict(text="一起读书吧。", voice=True, emotion="neutral", spoken_text="いっしょにほんをよみましょう。", request_key="one")
        first = execute("reply", self.context, **args)
        self.assertEqual(execute("reply", self.context, **args), first)
        rows = self.broker.committed_messages(self.token)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["emotion"], "neutral")
        self.assertTrue(rows[0]["voice"])
        with self.assertRaises(ConsoleFileError):
            execute("reply", self.context, **{**args, "emotion": "sad"})
        self.assertEqual(len(self.broker.committed_messages(self.token)), 1)

    def test_invalid_emotion_disabled_or_ambiguous_batch_is_atomic(self):
        for args in ({"voice": True}, {"voice": True, "emotion": "missing"},
                     {"voice": False, "emotion": "neutral"},
                     {"voice": True, "emotion": "neutral", "text": ["one", "two"]}):
            with self.subTest(args=args), self.assertRaises(ConsoleFileError):
                execute("reply", self.context, **{"text": "こんにちは。", **args})
            self.assertFalse(self.broker.committed_messages(self.token))
        context = self.broker.issue_context({**self.payload, "voice": {"enabled": False}}, self.root / "disabled")
        with self.assertRaises(ConsoleFileError):
            execute("reply", context, text="こんにちは。", voice=True, emotion="neutral")

    def test_voice_cannot_merge_into_text_to_evade_official_quota(self):
        execute("reply", self.context, text="hello", voice=True, emotion="neutral")
        with self.assertRaises(ConsoleFileError):
            execute("reply", self.context, text="more")
        self.assertEqual(len(self.broker.committed_messages(self.token)), 1)

    def test_fast_mcp_reply_accepts_voice_and_scope_rejects_hidden_tools(self):
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "qqbot_reply",
            "arguments": {"context_file": self.context, "text": "hello", "voice": True, "emotion": "neutral"}}})
        self.assertFalse(response["result"].get("isError"))
        self.assertTrue(self.broker.committed_messages(self.token)[0]["voice"])
        with patch.dict("os.environ", {"QQBOT_MCP_ROLE": "main"}):
            self.assertEqual({row["name"] for row in tools_schema()}, {"qqbot_reply", "qqbot_cli", "qqbot_read_memory", "qqbot_console_help"})
            result = handle({"id": 2, "method": "tools/call", "params": {"name": "qqbot_desktop_screenshot", "arguments": {}}})
            self.assertTrue(result["result"]["isError"])


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reference = self.root / "reference.wav"
        self.reference.write_bytes(fixture_wav())
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(json.dumps({"version": 1, "characters": {"fixture": {"gpt_weights": "fixture.ckpt",
            "sovits_weights": "fixture.pth", "emotions": {"neutral": {"reference_audio": str(self.reference),
                "prompt_text": "こんにちは。", "prompt_language": "ja"}}}}}), encoding="utf-8")
        self.config = VoiceConfig(enabled=True, character="fixture", catalog_path=str(self.catalog), cache_dir=str(self.root / "cache"), streaming=False, reuse_audio=False, denoise={"enabled": False})
        self.calls = []

    async def request(self, client, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))
        if endpoint == "/character_list":
            return b'{"fixture":["neutral"]}'
        if endpoint.startswith("/set_"):
            return b'{"message":"success"}'
        return fixture_wav()

    async def pcm(self, config, source, target):
        shutil.copyfile(source, target)

    async def test_api_v2_full_fixture_flow_joins_chunks_and_emits_silk_manifest(self):
        service = VoiceService(self.config)
        with patch.object(service, "_request", side_effect=self.request), patch("voice_service.run_ffmpeg", side_effect=self.pcm):
            result = await service.synthesize("一緒に読む。楽しいですね。", "neutral")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(len(result["chunks"]), 2)
        self.assertAlmostEqual(result["duration"], 1.12)
        self.assertTrue(Path(result["silk_path"]).read_bytes().startswith(SILK_HEADER))
        bodies = [values["body"] for method, path, values in self.calls if path == "/tts"]
        self.assertTrue(all(body["text_lang"] == "ja" and body["streaming_mode"] is False for body in bodies))
        self.assertTrue(all(Path(body["ref_audio_path"]).resolve() == self.reference.resolve() for body in bodies))
        self.assertTrue(all(not any("\u3400" <= c <= "\u9fff" for c in body["text"]) for body in bodies))

    async def test_inference_character_contract_and_no_emotion_fallback(self):
        config = self.config.model_copy(update={"provider": "gpt_sovits_inference"})
        service = VoiceService(config)
        with patch.object(service, "_request", side_effect=self.request), patch("voice_service.run_ffmpeg", side_effect=self.pcm):
            await service.synthesize("ほんをよみます。", "neutral")
        body = next(values["body"] for method, path, values in self.calls if path == "/tts")
        self.assertEqual({key: body[key] for key in ("character", "emotion", "format", "stream", "text_language")},
            {"character": "fixture", "emotion": "neutral", "format": "wav", "stream": False, "text_language": "ja"})
        with patch.object(service, "_request", return_value=b'{"fixture":["default"]}') as request:
            with self.assertRaises(VoiceError):
                await service.synthesize("ほんをよみます。", "neutral")
            self.assertEqual(request.await_count, 1)

    async def test_cancel_before_synthesis_finishes_retains_cancelled_manifest(self):
        service = VoiceService(self.config)
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(service, "_request", side_effect=blocked):
            task = asyncio.create_task(service.synthesize("ほんをよみます。", "neutral"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        manifest = json.loads(next((self.root / "cache").rglob("manifest.json")).read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "cancelled")
        self.assertNotIn("silk_path", manifest)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "voice.silk"
        self.path.write_bytes(SILK_HEADER + b"fixture")
        self.media = MediaConfig(cache_dir=str(self.root), local_library_roots=[str(self.root)])
        self.asset = MediaAsset(kind="voice", local_path=str(self.path), sha256=hashlib.sha256(self.path.read_bytes()).hexdigest(), mime="audio/silk")

    def ctx(self, transport="onebot", group=True):
        return MessageEnvelope(transport=transport, account_id="fixture", conversation_id="123", sender_id="456", message_id="q",
            text="question", is_group=group, send_text=AsyncMock(), send_action=AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": "v1"}}), api=SimpleNamespace(
                post_group_file=AsyncMock(return_value={"file_info": "voice-ticket"}), post_group_message=AsyncMock(return_value={"id": "v1"})))

    async def test_onebot_record_with_validated_reply_and_mention(self):
        ctx = self.ctx()
        receipt = await MediaSender(self.media).send(ctx, self.asset, reply_to="q", mention="456")
        action, params = ctx.send_action.call_args.args
        self.assertEqual(action, "send_group_msg")
        self.assertEqual([segment["type"] for segment in params["message"]], ["reply", "at", "record"])
        self.assertEqual(receipt.message_id, "v1")
        ctx.send_text.assert_not_called()

    async def test_remote_gateway_uses_uploaded_record_path(self):
        ctx = self.ctx(group=False)
        class Gateway:
            account_config = SimpleNamespace(ws_url="ws://192.0.2.10:3001")
            async def send(self, *args, **kwargs):
                return {"status": "ok", "retcode": 0, "data": {"message_id": "v1"}}
        gateway = Gateway()
        ctx.send_action = gateway.send
        sender = MediaSender(self.media)
        with patch.object(sender, "stream_to_gateway", return_value="/remote/voice.silk") as upload:
            await sender.send(ctx, self.asset)
        upload.assert_awaited_once()

    async def test_official_uses_voice_upload_type_three(self):
        ctx = self.ctx(transport="official")
        sender = MediaSender(self.media)
        with patch.object(sender, "public_asset", return_value="https://fixture.invalid/voice.silk"):
            await sender.send(ctx, self.asset)
        self.assertEqual(ctx.api.post_group_file.call_args.kwargs["file_type"], 3)
        self.assertEqual(ctx.api.post_group_message.call_args.kwargs["msg_type"], 7)
        ctx.send_text.assert_not_called()

    async def test_lost_gateway_receipt_remains_unknown_without_replay(self):
        ctx = self.ctx()
        ctx.send_action = AsyncMock(side_effect=SendUnknown("fixture timeout"))
        with self.assertRaises(SendUnknown):
            await MediaSender(self.media).send(ctx, self.asset)
        self.assertEqual(ctx.send_action.await_count, 1)


class DeliveryQueueTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the actual committed CLI → synthesis → record → ledger path."""
    def setUp(self):
        from media_integration import MediaClientMixin
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        voice = VoiceConfig(enabled=True, cache_dir=str(self.root / "voice"))
        self.context = self.broker.issue_context({"namespace": "fixture", "scope": "private", "sender_id": "456",
            "is_group": False, "transport": "onebot", "voice_config": voice.model_dump(),
            "voice": {"enabled": True, "language": "ja", "emotions": ["neutral"]}}, self.root / "contexts")
        _, token = ConsoleBroker.from_context(self.context)
        self.command = execute("reply", self.context, text="一起读书吧。", voice=True,
                               emotion="neutral", spoken_text="いっしょにほんをよみましょう。", finish=True)["message_ids"][0]
        self.ctx = MessageEnvelope(transport="onebot", account_id="fixture", conversation_id="123", sender_id="456",
            message_id="q", text="question", send_text=AsyncMock(), send_action=AsyncMock(return_value={
                "status": "ok", "retcode": 0, "data": {"message_id": "voice-accepted"}}),
            console_token=token, cli_messages=self.broker.committed_messages(token))
        turns = Mock()
        turns.source.return_value = {}
        self.host = SimpleNamespace(app_config=AppConfig(), send_action=self.ctx.send_action, _chat_turns=turns,
            _get_main_coordinator=lambda: SimpleNamespace(broker=self.broker),
            _rebatch_reply_if_needed=lambda ctx: False, _record_dialogue_reply=Mock())
        self.run_batch = lambda: MediaClientMixin._send_decision_batch(self.host, {}, self.ctx, "fixture-persona")
        folder = self.root / "voice" / "ready"
        folder.mkdir(parents=True)
        silk = folder / "voice.silk"
        silk.write_bytes(SILK_HEADER + b"fixture")
        self.manifest = {"state": "ready", "silk_path": str(silk), "sha256": hashlib.sha256(silk.read_bytes()).hexdigest(),
                         "duration": .5, "manifest_path": str(folder / "manifest.json")}
        Path(self.manifest["manifest_path"]).write_text(json.dumps(self.manifest), encoding="utf-8")

    def state(self):
        return self.broker.message_status(self.ctx.console_token, self.command)["state"]

    async def test_accepted_voice_is_recorded_once_and_survives_audit_write_failure(self):
        with patch("voice_delivery.VoiceService.synthesize", new=AsyncMock(return_value=self.manifest)) as synthesize, \
             patch("voice_delivery.Path.write_text", side_effect=OSError("fixture full disk")):
            result = await self.run_batch()
            await self.run_batch()  # Same committed command cannot be claimed again.
        self.assertEqual(result, "一起读书吧。")
        self.assertEqual(self.state(), "accepted")
        self.assertEqual(synthesize.await_count, 1)
        self.assertEqual(self.ctx.send_action.await_count, 1)
        params = self.ctx.send_action.call_args.args[1]
        self.assertEqual(params["message"][0]["type"], "record")
        self.ctx.send_text.assert_not_awaited()
        self.host._record_dialogue_reply.assert_called_once()
        self.assertEqual(self.host._record_dialogue_reply.call_args.args[2], "一起读书吧。")

    async def test_cancel_during_synthesis_marks_cancelled_without_transport(self):
        with patch("voice_delivery.VoiceService.synthesize", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.run_batch()
        self.assertEqual(self.state(), "cancelled")
        self.ctx.send_action.assert_not_awaited()
        self.host._record_dialogue_reply.assert_not_called()

    async def test_queued_reply_uses_current_saved_character(self):
        config = AppConfig()
        config.voice = VoiceConfig(enabled=True, character="new-character", cache_dir=str(self.root / "voice"))
        path = self.root / "live.yaml"
        config.save(str(path))
        self.host.config_path = str(path)
        selected = []
        async def synthesize(service, *args, **kwargs):
            selected.append(service.config.character)
            return self.manifest
        with patch("voice_delivery.VoiceService.synthesize", new=synthesize):
            await self.run_batch()
        self.assertEqual(selected, ["new-character"])
        self.assertEqual(self.state(), "accepted")

    async def test_switch_during_synthesis_never_submits_old_voice(self):
        config = AppConfig()
        config.voice = VoiceConfig(enabled=True, character="first", cache_dir=str(self.root / "voice"))
        path = self.root / "live.yaml"
        config.save(str(path))
        self.host.config_path = str(path)
        async def synthesize(*args, **kwargs):
            config.voice.character = "second"
            config.save(str(path))
            return self.manifest
        with patch("voice_delivery.VoiceService.synthesize", new=synthesize):
            with self.assertRaisesRegex(VoiceError, "切换"):
                await self.run_batch()
        self.ctx.send_action.assert_not_awaited()


    async def test_backlog_after_synthesis_cancels_before_submission(self):
        self.host._rebatch_reply_if_needed = Mock(side_effect=[False, True])
        with patch("voice_delivery.VoiceService.synthesize", new=AsyncMock(return_value=self.manifest)):
            with self.assertRaises(asyncio.CancelledError):
                await self.run_batch()
        self.assertEqual(self.state(), "cancelled")
        self.ctx.send_action.assert_not_awaited()

    async def test_cancel_or_lost_receipt_after_submission_stays_unknown_without_replay(self):
        self.ctx.send_action.side_effect = SendUnknown("fixture lost receipt")
        with patch("voice_delivery.VoiceService.synthesize", new=AsyncMock(return_value=self.manifest)):
            with self.assertRaises(SendUnknown):
                await self.run_batch()
            await self.run_batch()
        self.assertEqual(self.state(), "unknown")
        self.assertEqual(self.ctx.send_action.await_count, 1)
        self.host._record_dialogue_reply.assert_not_called()


class VoiceSettingsReloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_language_switch_reloads_without_replacing_persistent_identity(self):
        from test_startup_participation import StartupParticipationTests
        fixture = StartupParticipationTests()
        fixture.setUp()
        try:
            for official in (False, True):
                bot, key = fixture.bot(official, stored_cid="persistent-voice-identity")
                path = Path(f"voice-config-{official}.yaml").resolve()
                config = bot.app_config.model_copy(deep=True)
                config.save(str(path))
                bot.config_path = str(path)
                bot._config_mtime = path.stat().st_mtime
                config.voice.enabled = True
                config.voice.default_language = "mixed"
                config.save(str(path))
                os.utime(path, (bot._config_mtime + 2, bot._config_mtime + 2))
                with patch.object(bot.ai, "reset_session", wraps=bot.ai.reset_session) as reset:
                    bot.sync_system_prompt()
                self.assertTrue(bot.app_config.voice.enabled)
                self.assertEqual(bot.app_config.voice.default_language, "mixed")
                self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "persistent-voice-identity")
                reset.assert_not_called()
        finally:
            await fixture.asyncTearDown()
