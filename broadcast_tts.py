"""Live-only PCM synthesis. Shares model adapters, never QQ packaging or SILK."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
import uuid

import aiohttp

from audio_processing import clean_reference
from voice_completion import inspect_chunk
from voice_engine import backend_config, backend_endpoint, ready
from voice_model_cache import select_models
from voice_service import VoiceService, engine_lock, run_ffmpeg
from voice_stream import completed_pcm_events, native_sovits_stream, voicestudio_stream
from voice_text import VoiceError, completion_limit, inline_emotions, japanese_tokens, prepare_chunks, PROSODY_INSTRUCTION, PROSODY_SPEEDS


def broadcast_voice_context(config):
    from voice_service import load_catalog
    result = {"enabled": config.enabled, "pipeline": "broadcast", "provider": config.provider,
              "language": config.default_language, "character": config.character,
              "delivery_mode": config.delivery_mode, "emotions": [], "inline_emotion": True,
              "prosody": PROSODY_SPEEDS,
              "instruction": "直播语音独立配置，使用 broadcast speak；显式指定默认 emotion，句内可用 [emotion:标签] 切换。即时 PCM 不生成 QQ SILK。" + PROSODY_INSTRUCTION}
    if config.enabled:
        try:
            references = load_catalog(config)["characters"][config.character]["emotions"]
            result["emotions"] = list(references)
            result["emotion_descriptions"] = {key: ref.get("description", key) for key, ref in references.items()}
        except (KeyError, ValueError, OSError) as error:
            result["error"] = str(error)
    return result


class BroadcastVoiceService:
    def __init__(self, config):
        self.config = config
        # Only transport/catalog selection is shared. VoiceService.synthesize is
        # deliberately absent from this pipeline, including validated delivery.
        self.adapter = VoiceService(config)

    async def synthesize(self, text, emotion, *, spoken_text="", audio_sink):
        config = self.config
        if not config.enabled:
            raise VoiceError("请先在虚拟主播页面启用直播专用语音。")
        started = time.perf_counter()
        spoken = str(spoken_text or text).strip()
        labels = inline_emotions(spoken, emotion)
        selections = {tag: self.adapter._selection(tag) for tag in labels}
        character = selections[emotion][0]
        chunks = prepare_chunks(spoken, config, emotion, clause_limit=completion_limit(character))
        root = Path(config.cache_dir).resolve()
        folder = root / uuid.uuid4().hex
        folder.mkdir(parents=True)
        manifest_path = folder / "manifest.json"
        manifest = {"id": folder.name, "pipeline": "broadcast", "state": "synthesizing",
                    "provider": config.provider, "character": config.character,
                    "delivery_mode": config.delivery_mode, "text": text, "spoken_text": spoken,
                    "chunks": [chunk.to_dict() for chunk in chunks], "created_at": time.time()}
        first, seconds, byte_count = None, 0.0, 0

        def save():
            temporary = manifest_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(manifest_path)

        async def emit(event, chunk):
            nonlocal first, seconds, byte_count
            import base64
            data = base64.b64decode(event["pcm_base64"], validate=True)
            byte_count += len(data)
            seconds += len(data) / (event["sample_rate"] * event["channels"] * 2)
            if byte_count > config.max_audio_mb * 1024 * 1024 or seconds > config.max_duration_seconds:
                raise VoiceError("直播音频超过大小或时长上限。")
            if first is None:
                first = (time.perf_counter() - started) * 1000
            await audio_sink({**event, "emotion": chunk.emotion, "text": chunk.text,
                              "pause_before_ms": chunk.pause_before_ms})

        async def render():
            native = backend_config(config)
            effective, ws = config.provider, None
            async with engine_lock(backend_endpoint(config)):
                if config.auto_start:
                    try:
                        await ready(config)
                    except (VoiceError, aiohttp.ClientConnectionError, TimeoutError) as error:
                        if (config.provider != "voicestudio" or not config.voicestudio.local_bridge
                                or not config.voicestudio.fallback_enabled):
                            raise
                        # The bridge failed before synthesis, so no audio can be repeated.
                        manifest["fallback_reason"] = str(error)[:600]
                        await ready(native)
                        effective = "gpt_sovits_v2"
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=config.timeout_seconds)) as client:
                    if effective == "voicestudio":
                        try:
                            url = config.endpoint.replace("http:", "ws:", 1).replace("https:", "wss:", 1)
                            ws = await client.ws_connect(url + "/ws/tts", headers=self.adapter._studio_headers(), max_msg_size=256 * 1024)
                        except (aiohttp.ClientConnectionError, aiohttp.WSServerHandshakeError) as error:
                            if (not config.voicestudio.local_bridge or not config.voicestudio.fallback_enabled
                                    or isinstance(error, aiohttp.WSServerHandshakeError) and error.status not in (502, 503, 504)):
                                raise
                            # Fallback is allowed only before any audio is emitted.
                            effective = "gpt_sovits_v2"
                            manifest["fallback_reason"] = str(error)[:600]
                            if config.auto_start:
                                await ready(native)
                    manifest["effective_provider"] = effective
                    try:
                        if effective == "gpt_sovits_v2":
                            adapter = VoiceService(native)
                            async def select(route, params):
                                return await adapter._request(client, "GET", route, params=params)
                            manifest["model_selection"] = await select_models(character, native.endpoint, select, reuse=config.reuse_models)
                        references = {}
                        for index, chunk in enumerate(chunks):
                            info = manifest["chunks"][index]
                            reference = selections[chunk.emotion][1]
                            source = folder / f"chunk-{index:03}.wav"
                            guarded = bool(character.get("experimental"))
                            validated = config.delivery_mode == "validated"
                            async def sink(event):
                                await emit(event, chunk)
                            # Once audible, neither retry nor final-WAV replay may
                            # repeat a prefix. Validation failures stop the stream.
                            for attempt in range(2 if validated and guarded else 1):
                                if effective == "voicestudio":
                                    voice = reference.get("voicestudio_profile_id") or character.get("voicestudio_profile_id") or "qqbot:" + config.character + ":" + chunk.emotion
                                    body = {"text": chunk.text, "voice": voice, "language": chunk.language,
                                            "speed": chunk.speed, "engine": config.voicestudio.engine,
                                            "seed": 42 if not attempt else 17, "parallel_infer": not attempt}
                                    for field in ("instruct", "emo_text", "emo_alpha", "emo_vector"):
                                        if field in reference.get("voicestudio", {}):
                                            body[field] = reference["voicestudio"][field]
                                    info["stream"] = await voicestudio_stream(ws, body, source, config,
                                        index=index, audio_sink=None if validated else sink)
                                else:
                                    if chunk.emotion not in references:
                                        ref, cleanup = await clean_reference(reference["reference_audio"], config.denoise,
                                            ffmpeg=config.ffmpeg_path, cache_root=root)
                                        prompt = reference.get("prompt_text", "")
                                        if reference.get("prompt_language", "ja") == "ja" and prompt:
                                            prompt = "".join(japanese_tokens(prompt, config.dictionary))
                                        references[chunk.emotion] = ref, cleanup, prompt
                                    ref, cleanup, prompt = references[chunk.emotion]
                                    info["reference_denoise"] = cleanup
                                    body = {"text": chunk.text, "text_lang": chunk.language, "ref_audio_path": ref,
                                            "prompt_text": prompt, "prompt_lang": reference.get("prompt_language", "ja"),
                                            "text_split_method": "cut0", "batch_size": 1, "speed_factor": chunk.speed,
                                            "sample_steps": config.sample_steps, "parallel_infer": not attempt,
                                            "seed": 42 if not attempt else 17}
                                    info["stream"] = await native_sovits_stream(client, native.endpoint, body, source,
                                        config, index=index, audio_sink=None if validated else sink)
                                if guarded:
                                    check = inspect_chunk(source, chunk.text, chunk.language, chunk.speed)
                                    info.setdefault("duration_checks", []).append(check)
                                    if check["suspected_early_end"]:
                                        if validated and not attempt:
                                            continue
                                        raise VoiceError("直播片段疑似提前结束；已停止且不会重播已出声内容。可更换参考切片或使用先校验模式。")
                                if validated:
                                    target = folder / f"chunk-{index:03}.processed.wav"
                                    processed = config.model_copy(deep=True)
                                    if info.get("stream", {}).get("server", {}).get("output_denoise"):
                                        processed.denoise.enabled = False
                                    await run_ffmpeg(processed, source, target)
                                    for event in completed_pcm_events(target, config, index=index):
                                        await emit(event, chunk)
                                break
                            save()
                    finally:
                        if ws is not None:
                            await ws.close()

        save()
        try:
            async with asyncio.timeout(config.timeout_seconds):
                await render()
            if first is None:
                raise VoiceError("直播推理没有生成可播放音频。")
            manifest.update(state="ready", source_seconds=seconds, manifest_path=str(manifest_path),
                            metrics={"first_audio_ms": round(first, 2),
                                     "total_ms": round((time.perf_counter() - started) * 1000, 2)})
            save()
            return manifest
        except BaseException as error:
            manifest.update(state="cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                            error=str(error)[:1000], completed_at=time.time())
            save()
            raise
