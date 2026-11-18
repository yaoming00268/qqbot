"""Offline regressions for natural phrasing, prosody and one-click B站 setup."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
from unittest.mock import patch
import wave

from aiohttp import web
import pytest

from config import AppConfig
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.files import ConsoleFileError
from voice_config import BroadcastVoiceConfig, VoiceConfig
from voice_text import VoiceError, inline_prosody, prepare_chunks, strip_controls


INVALID_PROSODY = ["[fast]hello", "hello[/slow]", "[fast]hello[/slow]", "[slow", "[stretch:2]a",
    "[slow][fast]hello[/fast][/slow]", "[stretch][/stretch]", "[fast]…[/fast]",
    "[slow][emotion:neutral][/slow]", "[fast]a[/fast]" * 65]


@pytest.mark.parametrize("text", INVALID_PROSODY)
def test_malformed_prosody_rejected_before_a_reply_is_committed(tmp_path, text):
    broker = ConsoleBroker(tmp_path / "console.sqlite3")
    context = broker.issue_context({"namespace": "fixture", "session_key": "fixture", "scope": "private", "transport": "onebot", "main_agent": True,
        "voice": {"enabled": True, "emotions": ["neutral"]}}, tmp_path / "contexts")
    _, token = ConsoleBroker.from_context(context)
    with pytest.raises(ConsoleFileError) as error:
        execute("reply", context, text="完整台词", spoken_text=text, voice=True, emotion="neutral")
    assert error.value.code == "VOICE_PROSODY_INVALID"
    assert broker.committed_messages(token) == []


def test_language_emotion_and_prosody_compose_without_extra_pause():
    text = ("[slow][ja]今日は[emotion:sad]ゆっくり。[/ja][zh]慢一点。[/zh][/slow]"
            "[fast][en]Let's go![/en][/fast][stretch][ja]ねえ[/ja][/stretch][ja]またね。[/ja]")
    chunks = prepare_chunks(text, VoiceConfig(default_language="mixed"), "neutral")
    assert [c.language for c in chunks] == ["ja", "ja", "zh", "en", "ja", "ja"]
    assert [c.prosody for c in chunks] == ["slow", "slow", "slow", "fast", "stretch", "normal"]
    assert [c.speed for c in chunks] == [.8, .8, .8, 1.2, .65, 1.0]
    assert [c.emotion for c in chunks] == ["neutral", "sad", "sad", "sad", "sad", "sad"]
    assert chunks[1].pause_before_ms == 0 and chunks[-1].pause_before_ms == 0
    assert "[" not in "".join(c.text for c in chunks)
    assert "ねえ" in strip_controls(text) and "[" not in strip_controls(text)


@pytest.mark.parametrize("base,marker,expected", [(2, "fast", 2), (.5, "slow", .5), (.5, "stretch", .5), (1.1, "fast", 1.32)])
def test_relative_speed_respects_backend_limits(base, marker, expected):
    chunks = prepare_chunks(f"[{marker}]完整的短句。[/{marker}]", VoiceConfig(default_language="zh", speed=base))
    assert chunks[0].speed == expected


def test_visual_line_wraps_and_short_acknowledgements_stay_in_a_phrase():
    chunks = prepare_chunks("嗯。今\n天\n我\n们\n一\n起出去散步吧。", VoiceConfig(default_language="zh"))
    assert len(chunks) == 1
    assert chunks[0].text == "嗯。今天我们一起出去散步吧。"
    assert chunks[0].speed == 1 and chunks[0].prosody == "normal"
    mixed = prepare_chunks("今\n天\n我们一起出去散步吧。", VoiceConfig(default_language="mixed"))
    assert len(mixed) == 1 and mixed[0].text == "今天我们一起出去散步吧。"
    english = prepare_chunks("Let's\nread together.", VoiceConfig(default_language="mixed"))
    assert len(english) == 1 and english[0].text == "Let's read together."


@pytest.mark.parametrize("text", ["好" * 21 + "。", "好" * 20 + "。", "Hello there " * 30])
def test_hard_limit_does_not_leave_a_one_character_or_punctuation_request(text):
    language = "en" if text.isascii() else "zh"
    chunks = prepare_chunks(text, VoiceConfig(default_language=language, chunk_chars=20))
    assert "".join(c.text for c in chunks) == text.strip()
    assert all(4 <= len(c.text) <= 20 for c in chunks)


def test_default_emotion_switches_and_explicit_stretch_keep_their_boundaries():
    chunks = prepare_chunks("好[emotion:sad]难过[emotion:]呀[stretch]啊[/stretch]。", VoiceConfig(default_language="zh"), "neutral")
    assert [(c.text, c.emotion, c.prosody) for c in chunks[:4]] == [
        ("好", "neutral", "normal"), ("难过", "sad", "normal"), ("呀", "neutral", "normal"), ("啊。", "neutral", "stretch")]
    japanese = prepare_chunks("[stretch]ねえ[/stretch]。", VoiceConfig(), "neutral")
    assert len(japanese) == 1 and japanese[0].text == "ねえ。" and japanese[0].speed == .65


def wav_bytes(seconds=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\xe8\x03" * round(24000 * seconds))
    return output.getvalue()


@pytest.mark.parametrize("pipeline,provider,mode", [
    ("qq", "gpt_sovits_v2", "streaming"), ("qq", "gpt_sovits_v2", "validated"),
    ("qq", "gpt_sovits_inference", "validated"), ("qq", "voicestudio", "streaming"),
    ("broadcast", "gpt_sovits_v2", "streaming"), ("broadcast", "gpt_sovits_v2", "validated"),
    ("broadcast", "voicestudio", "streaming"), ("broadcast", "voicestudio", "validated")])
def test_actual_http_and_ws_requests_receive_phrase_speeds_and_clean_text(tmp_path, pipeline, provider, mode):
    from broadcast_tts import BroadcastVoiceService
    from voice_service import VoiceService
    requests, events = [], []
    async def scenario():
        async def select(request):
            return web.json_response({"message": "success"})
        async def characters(request):
            return web.json_response({"fixture": ["neutral"]})
        async def native(request):
            body = await request.json()
            requests.append(body)
            return web.Response(body=wav_bytes(1 / body.get("speed_factor", body.get("speed", 1))), content_type="audio/wav")
        async def websocket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                body = json.loads(message.data)
                requests.append(body)
                pcm = wav_bytes(1 / body["speed"])[44:]
                await ws.send_json({"type": "start", "sample_rate": 24000, "channels": 1, "format": "pcm16"})
                await ws.send_bytes(pcm)
                await ws.send_json({"type": "done", "audio_bytes": len(pcm)})
            return ws
        app = web.Application()
        app.router.add_get("/set_gpt_weights", select)
        app.router.add_get("/set_sovits_weights", select)
        app.router.add_get("/character_list", characters)
        app.router.add_post("/tts", native)
        app.router.add_get("/ws/tts", websocket)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        reference, catalog = tmp_path / "reference.wav", tmp_path / "catalog.json"
        reference.write_bytes(wav_bytes())
        catalog.write_text(json.dumps({"version": 1, "characters": {"fixture": {
            "gpt_weights": "fixture.ckpt", "sovits_weights": "fixture.pth", "emotions": {"neutral": {
                "reference_audio": str(reference), "prompt_text": "こんにちは。", "prompt_language": "ja"}}}}}), encoding="utf-8")
        kind = VoiceConfig if pipeline == "qq" else BroadcastVoiceConfig
        config = kind(enabled=True, provider=provider, character="fixture", endpoint=url, speed=1.1,
            catalog_path=str(catalog), cache_dir=str(tmp_path / "cache"), streaming=mode == "streaming",
            reuse_audio=False, denoise={"enabled": False})
        config.voicestudio.backend_endpoint = url
        text = "通常の台詞。[fast]すぐにいこう。[/fast][slow]ゆっくりはなす。[/slow][stretch]ねえ[/stretch]またあした。"
        try:
            if pipeline == "qq":
                result = await VoiceService(config).synthesize("自然な台詞", "neutral", spoken_text=text)
                assert Path(result["silk_path"]).is_file()
                assert result["completion_policy"]["frontend_version"] == 6
            else:
                config.delivery_mode = mode
                async def sink(event):
                    events.append(event)
                with patch("voice_service.encode_silk", side_effect=AssertionError("Live must not encode QQ audio")):
                    result = await BroadcastVoiceService(config).synthesize("自然な台詞", "neutral", spoken_text=text, audio_sink=sink)
                assert events and not list(tmp_path.rglob("*.silk"))
                assert all(base64.b64decode(e["pcm_base64"]) for e in events)
            assert result["state"] == "ready"
            assert len(requests) == 5
            assert [body.get("speed_factor", body.get("speed")) for body in requests] == [1.1, 1.32, .88, .715, 1.1]
            assert all("[" not in body["text"] and len(body["text"]) > 1 for body in requests)
            # Test transport plus resulting duration, not just duplicated metadata.
            durations = []
            folder = Path(result["manifest_path"]).parent
            paths = sorted(folder.glob("chunk-*.source.wav")) if pipeline == "qq" else sorted(folder.glob("chunk-???.wav"))
            for path in paths:
                with wave.open(str(path), "rb") as stream:
                    durations.append(stream.getnframes() / stream.getframerate())
            assert len(durations) == 5 and durations[1] < durations[0] < durations[2] < durations[3]
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_presets_preserve_actor_prompt_credentials_and_separate_live_voice(tmp_path):
    from agent_core.configuration import ActorConfig
    from ui.studio_api import StudioAPI
    api = StudioAPI(tmp_path)
    initial = api.bootstrap()["data"]
    persona = next(p for p in initial["personas"]["items"] if p["name"] == initial["personas"]["active"])
    path = tmp_path / "config.yaml"
    config = AppConfig.load(str(path))
    config.agent.actors["existing"] = ActorConfig(name="原角色名", persona_id=persona["id"], prompt="保留我的角色提示。", enabled=False)
    config.agent.bindings["persona:" + persona["id"]] = "existing"
    config.bilibili.actor_id = "live-role"
    config.bilibili.autonomy.goal = "保留既有选题"
    config.bilibili.autonomy.max_actions_per_hour = 3
    config.agent.autonomous_model_enabled = False
    config.save(str(path))
    private = tmp_path / "credentials.json"
    private.write_text('fixture secrets stay untouched', encoding="utf-8")
    untouched = (private.read_bytes(), config.voice.model_dump(), config.broadcast_voice.model_dump(), config.bilibili.livehime.model_dump())
    revision = api.bootstrap()["data"]["revision"]
    result = api.broadcast("account_preset", {"mode": "operate", "revision": revision})
    assert result["ok"], result
    config = AppConfig.load(str(path))
    assert config.agent.actors["existing"].prompt == "保留我的角色提示。"
    assert config.agent.actors["existing"].name == "原角色名" and config.agent.actors["existing"].enabled
    assert config.agent.autonomous_model_enabled and config.bilibili.enabled
    assert config.bilibili.autonomy.actor_id == "existing" and config.bilibili.actor_id == "live-role"
    assert config.bilibili.autonomy.goal == "保留既有选题" and config.bilibili.autonomy.max_actions_per_hour == 3
    assert config.bilibili.allow_publish and config.bilibili.autonomy.allow_comments and config.bilibili.autonomy.allow_dynamics
    assert not config.bilibili.autonomy.allow_video_uploads and not config.bilibili.autonomy.allow_profile_update
    result = api.broadcast("account_preset", {"mode": "browse", "revision": result["data"]["revision"]})
    assert result["ok"], result
    config = AppConfig.load(str(path))
    assert config.bilibili.autonomy.enabled and not config.bilibili.autonomy.allow_comments and not config.bilibili.autonomy.allow_dynamics
    # Browsing preset does not revoke an existing independent live publish switch.
    assert config.bilibili.allow_publish
    result = api.broadcast("account_preset", {"mode": "pause", "revision": result["data"]["revision"]})
    assert result["ok"], result
    config = AppConfig.load(str(path))
    assert not config.bilibili.autonomy.enabled and config.agent.actors["existing"].enabled
    assert (private.read_bytes(), config.voice.model_dump(), config.broadcast_voice.model_dump(), config.bilibili.livehime.model_dump()) == untouched


def test_preset_rejects_stale_missing_revision_and_invalid_mode_without_writes(tmp_path):
    from ui.studio_api import StudioAPI
    api = StudioAPI(tmp_path)
    revision = api.bootstrap()["data"]["revision"]
    path = tmp_path / "config.yaml"
    before = path.read_bytes()
    for params in ({"mode": "operate"}, {"mode": "operate", "revision": "stale"}, {"mode": "all", "revision": revision}):
        assert not api.broadcast("account_preset", params)["ok"]
        assert path.read_bytes() == before


def test_two_studios_cannot_overwrite_the_same_revision(tmp_path):
    from ui.studio_api import StudioAPI
    api = StudioAPI(tmp_path)
    revision = api.bootstrap()["data"]["revision"]
    other = StudioAPI(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda instance: instance.broadcast("account_preset", {"mode": "browse", "revision": revision}), [api, other]))
    assert sum(result["ok"] for result in results) == 1
    assert AppConfig.load(str(tmp_path / "config.yaml")).bilibili.autonomy.enabled


def test_native_bilibili_account_lists_operations_and_legacy_draft_uses_native_checks(tmp_path):
    from agent_core.runtime import ActorRuntime
    from agent_core.tools import operate
    from bilibili_operations import schedule
    from broadcast_commands import context_binding
    from broadcast_service import BroadcastService
    from test_bilibili_account import AccountFixture
    config = AppConfig.model_validate({"agent": {"database_path": str(tmp_path / "actors.sqlite3"),
        "actors": {"operator": {"name": "运营"}}}, "bilibili": {"enabled": True,
        "autonomy": {"enabled": True, "actor_id": "operator"}}})
    path = tmp_path / "config.yaml"
    config.save(str(path))
    service = BroadcastService(path, adapter=AccountFixture())
    runtime = ActorRuntime(config.agent)
    try:
        assert schedule(service)["scheduled"]
        owner, _ = runtime.store.claim_events("operator")
        broker = ConsoleBroker(tmp_path / "console.sqlite3")
        payload = {**context_binding(path, actor_id="operator", account_ops=True), "namespace": "fixture", "session_key": "fixture", "scope": "private",
            "main_agent": True, "actor_id": "operator", "actor_event": True, "actor_database": str(runtime.store.path), "actor_lease_owner": owner}
        context = broker.issue_context(payload, tmp_path / "contexts")
        _, token = ConsoleBroker.from_context(context)
        account = operate(broker, token, "accounts")["accounts"][0]
        assert account["tool"] == {"action": "broadcast"} and "draft" in account["actions"]
        assert "publish" not in account["actions"] and "create_draft" not in account["actions"]
        with patch("broadcast_host.execute", side_effect=lambda path, op, params: asyncio.run(service.execute(op, params))):
            draft = operate(broker, token, "draft", account_id=account["id"], text="这是本机草稿。")
            assert draft["kind"] == "dynamic" and draft["body"]["text"] == "这是本机草稿。"
            assert service.store.drafts()[0]["id"] == draft["id"]
            config.bilibili.autonomy.enabled = False
            config.save(str(path))
            with pytest.raises(PermissionError, match="授权已撤回"):
                operate(broker, token, "draft", account_id=account["id"], text="撤权后不得保存。")
        service.adapter.publish_dynamic.assert_not_awaited()
    finally:
        asyncio.run(runtime.close())
