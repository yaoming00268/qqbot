"""Expressive streaming orchestration; no model libraries in the bot process."""
from __future__ import annotations

import asyncio
from array import array
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid
import wave

import aiohttp

from audio_processing import clean_reference
from voice_model_cache import select_models
from voice_pcm import copy_ready_pcm
from voice_stream import completed_pcm_events, native_sovits_stream, voicestudio_stream
from voice_text import VoiceError, completion_limit, inline_emotions, japanese_tokens, prepare_chunks


def fingerprint(path):
    source = Path(path)
    if source.is_file():
        return [str(source.resolve()), source.stat().st_size, source.stat().st_mtime_ns]
    return str(path)


def join_chunks(chunks, folder, config):
    """Sentence pauses stay explicit; a mid-sentence emotion switch adds no silence."""
    parts = []
    for index, chunk in enumerate(chunks):
        with wave.open(str(folder / f"chunk-{index:03}.wav"), "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 24000):
                raise VoiceError("合成片段必须为 24kHz 单声道 PCM16。")
            samples = array("h", source.readframes(source.getnframes()))
        if not samples:
            raise VoiceError("合成片段为空。")
        if __import__("sys").byteorder != "little":
            samples.byteswap()
        if index and chunk.pause_before_ms:
            parts.append(array("h", [0]) * (24000 * chunk.pause_before_ms // 1000))
        elif parts and config.crossfade_ms:
            previous = parts[-1]
            count = min(24000 * config.crossfade_ms // 1000, len(previous), len(samples))
            for offset in range(count):
                alpha = (offset + 1) / (count + 1)
                previous[-count + offset] = round(previous[-count + offset] * (1 - alpha) + samples[offset] * alpha)
            samples = samples[count:]
        parts.append(samples)
        if sum(len(part) for part in parts) / 24000 > config.max_duration_seconds:
            raise VoiceError("合成语音超过最长时长，请缩短台词。")
    frames = sum(len(part) for part in parts)
    if frames < 2400:
        raise VoiceError("合成音频为空或过短。")
    output = folder / "voice.wav"
    with wave.open(str(output), "wb") as stream:
        stream.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        for part in parts:
            if __import__("sys").byteorder != "little":
                part.byteswap()
            stream.writeframes(part.tobytes())
    return output, frames / 24000


async def synthesize(service, text, emotion, *, spoken_text="", check_current=None, progress=None):
    from voice_service import engine_lock, encode_silk, run_ffmpeg
    from voice_engine import backend_config, backend_endpoint, ready
    config = service.config
    started = time.perf_counter()
    spoken = str(spoken_text or text).strip()
    labels = inline_emotions(spoken, emotion)
    selections = {tag: service._selection(tag) for tag in labels}
    character = selections[emotion][0]
    clause_limit = completion_limit(character)
    chunks = prepare_chunks(spoken, config, emotion, clause_limit=clause_limit)
    references = {}
    for tag, (_, reference) in selections.items():
        info = dict(reference)
        if reference.get("reference_audio") and Path(reference["reference_audio"]).is_file():
            info["sha256"] = await asyncio.to_thread(lambda p=reference["reference_audio"]: hashlib.sha256(Path(p).read_bytes()).hexdigest())
        references[tag] = info
    identity = {"config": config.model_dump(), "text": text, "spoken": spoken, "emotion": emotion,
                "character": character, "references": references,
                "weights": {key: fingerprint(character.get(key, "")) for key in ("gpt_weights", "sovits_weights")},
                "frontend_version": 6}
    identifier = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    root = Path(config.cache_dir).resolve()
    folder = root / (identifier[:24] + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    cache = root / "artifacts" / identifier
    manifest = {"version": 2, "id": identifier, "state": "synthesizing", "created_at": time.time(),
                "text": text, "spoken_text": spoken, "emotion": emotion, "emotions": [c.emotion for c in chunks],
                "language": config.default_language, "character": config.character, "provider": config.provider,
                "reference": references[emotion], "references": references, "chunks": [c.to_dict() for c in chunks],
                "source": character.get("source", ""), "license": character.get("license", ""),
                "attribution": character.get("attribution", ""), "streaming": config.streaming,
                "completion_policy": {"clause_limit": clause_limit, "duration_guard": bool(character.get("experimental")),
                                      "frontend_version": 6}}
    manifest_path = folder / "manifest.json"
    first_audio = None
    def report(event):
        nonlocal first_audio
        if event.get("type") == "audio" and first_audio is None:
            first_audio = time.perf_counter()
        if progress:
            progress(event)
    def current():
        if check_current:
            check_current()
    def save():
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(manifest_path)
    save()

    async def render():
        current()
        if config.reuse_audio:
            try:
                saved = json.loads((cache / "cache.json").read_text(encoding="utf-8"))
                if saved["id"] == identifier and all(hashlib.sha256((cache / name).read_bytes()).hexdigest() == saved[name] for name in ("voice.wav", "voice.silk")):
                    for name in ("voice.wav", "voice.silk"):
                        await asyncio.to_thread(shutil.copyfile, cache / name, folder / name)
                    with wave.open(str(folder / "voice.wav"), "rb") as audio:
                        duration = audio.getnframes() / audio.getframerate()
                    if not .1 <= duration <= config.max_duration_seconds:
                        raise VoiceError("缓存语音时长无效。")
                    manifest["cache_hit"] = True
                    report({"type": "stage", "stage": "cache-hit"})
                    return folder / "voice.wav", folder / "voice.silk", duration, saved["voice.silk"]
            except (OSError, ValueError, KeyError, wave.Error):
                pass
        native_config = backend_config(config)
        active, ws = service, None
        effective = config.provider
        # The same underlying GPU lock is shared with singing and legacy previews.
        async with engine_lock(backend_endpoint(config)):
            current()
            if config.auto_start:
                report({"type": "stage", "stage": "starting"})
                try:
                    await ready(config, check_current=current)
                except (VoiceError, aiohttp.ClientConnectionError, TimeoutError) as error:
                    if config.provider != "voicestudio" or not config.voicestudio.fallback_enabled or not config.voicestudio.local_bridge:
                        raise
                    manifest["fallback_reason"] = str(error)[:600]
                    await ready(native_config, check_current=current)
                    effective = "gpt_sovits_v2"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=config.timeout_seconds)) as client:
                if effective == "voicestudio":
                    try:
                        url = config.endpoint.replace("http:", "ws:", 1).replace("https:", "wss:", 1)
                        ws = await client.ws_connect(url + "/ws/tts", headers=service._studio_headers(), max_msg_size=256 * 1024)
                    except (aiohttp.ClientConnectionError, aiohttp.WSServerHandshakeError) as error:
                        if (not config.voicestudio.fallback_enabled or not config.voicestudio.local_bridge
                                or isinstance(error, aiohttp.WSServerHandshakeError) and error.status not in (502, 503, 504)):
                            raise
                        effective = "gpt_sovits_v2"
                        manifest["fallback_reason"] = str(error)[:600]
                        if config.auto_start:
                            await ready(native_config, check_current=current)
                if effective == "gpt_sovits_v2":
                    from voice_service import VoiceService
                    active = service if config.provider == effective else VoiceService(native_config)
                    async def select(route, params):
                        return await active._request(client, "GET", route, params=params)
                    manifest["model_selection"] = await select_models(character, native_config.endpoint, select, reuse=config.reuse_models)
                elif effective == "gpt_sovits_inference":
                    available = json.loads(await service._request(client, "GET", "/character_list"))
                    if any(tag not in available.get(config.character, []) for tag in labels):
                        raise VoiceError("Inference 服务没有全部请求的情绪标签，已拒绝默认回退。")
                manifest["effective_provider"] = effective
                prepared_references = {}
                try:
                    for index, chunk in enumerate(chunks):
                        current()
                        report({"type": "stage", "stage": "synthesizing", "index": index, "count": len(chunks),
                                "emotion": chunk.emotion, "pause_before_ms": chunk.pause_before_ms})
                        reference = selections[chunk.emotion][1]
                        source, target = folder / f"chunk-{index:03}.source.wav", folder / f"chunk-{index:03}.wav"
                        info = manifest["chunks"][index]
                        guarded = bool(character.get("experimental"))
                        for attempt in range(2 if guarded else 1):
                            current()
                            attempt_started = time.perf_counter()
                            seed = 42 if attempt == 0 else 17
                            # Guarded phrases are published from the final validated
                            # WAV. Do not base64-encode and retain discarded attempts.
                            emit = None if guarded else report
                            if effective == "voicestudio":
                                voice = reference.get("voicestudio_profile_id") or character.get("voicestudio_profile_id") or "qqbot:" + config.character + ":" + chunk.emotion
                                body = {"text": chunk.text, "voice": voice, "language": chunk.language,
                                        "speed": chunk.speed, "engine": config.voicestudio.engine,
                                        "seed": seed, "parallel_infer": attempt == 0}
                                controls = reference.get("voicestudio", {})
                                for field in ("instruct", "emo_text", "emo_alpha", "emo_vector"):
                                    if field in controls:
                                        body[field] = controls[field]
                                info["stream"] = await voicestudio_stream(ws, body, source, config,
                                    progress=emit if config.streaming else None, index=index)
                            elif effective == "gpt_sovits_v2":
                                if chunk.emotion not in prepared_references:
                                    ref, cleanup = await clean_reference(reference["reference_audio"], config.denoise,
                                        ffmpeg=config.ffmpeg_path, cache_root=root, check_current=current)
                                    prompt = reference.get("prompt_text", "")
                                    if reference.get("prompt_language", "ja") == "ja" and prompt:
                                        prompt = "".join(japanese_tokens(prompt, config.dictionary))
                                    prepared_references[chunk.emotion] = ref, cleanup, prompt
                                ref, cleanup, prompt = prepared_references[chunk.emotion]
                                info["reference_denoise"] = cleanup
                                body = {"text": chunk.text, "text_lang": chunk.language, "ref_audio_path": ref,
                                        "prompt_text": prompt, "prompt_lang": reference.get("prompt_language", "ja"),
                                        "text_split_method": "cut0", "batch_size": 1, "speed_factor": chunk.speed,
                                        "sample_steps": config.sample_steps, "parallel_infer": attempt == 0,
                                        "media_type": "wav", "streaming_mode": False, "seed": seed}
                                if config.streaming:
                                    info["stream"] = await native_sovits_stream(client, native_config.endpoint, body, source, config, progress=emit, index=index)
                                else:
                                    source.write_bytes(await active._request(client, "POST", "/tts", body=body))
                            else:
                                body = {"character": config.character, "emotion": chunk.emotion, "text": chunk.text,
                                        "text_language": chunk.language, "format": "wav", "stream": False,
                                        "batch_size": 1, "speed": chunk.speed, "save_temp": False}
                                source.write_bytes(await service._request(client, "POST", "/tts", body=body))
                            # The owned VoiceStudio bridge has already denoised its PCM.
                            processed = config.model_copy(deep=True)
                            if info.get("stream", {}).get("server", {}).get("output_denoise"):
                                processed.denoise.enabled = False
                            reused_pcm = (not processed.denoise.enabled and
                                await asyncio.to_thread(copy_ready_pcm, source, target, processed))
                            if not reused_pcm:
                                await run_ffmpeg(processed, source, target)
                            info["pcm_processing"] = "validated-copy" if reused_pcm else "ffmpeg"
                            current()
                            if guarded:
                                from voice_completion import inspect_chunk
                                check = inspect_chunk(target, chunk.text, chunk.language, chunk.speed)
                                info.setdefault("duration_checks", []).append({"attempt": attempt+1, "seed": seed, **check})
                                if check["suspected_early_end"]:
                                    save()
                                    if attempt == 0:
                                        report({"type": "stage", "stage": "retrying", "index": index,
                                                "message": "片段异常短，丢弃本次音频并重新合成完整台词。"})
                                        continue
                                    raise VoiceError("模型连续生成异常短的片段，疑似提前结束；未提交半段语音，请更换参考切片或核对台词。")
                                if config.streaming:
                                    first_ms = (time.perf_counter() - attempt_started) * 1000
                                    for event in completed_pcm_events(target, config, index=index, first_audio_ms=first_ms):
                                        current()
                                        report(event)
                            break
                        info["wav"] = str(target)
                        save()
                        report({"type": "stage", "stage": "chunk-ready", "index": index,
                                "wav_path": str(target), "emotion": chunk.emotion,
                                "pause_before_ms": chunk.pause_before_ms, "text": chunk.text})
                finally:
                    if ws:
                        await ws.close()
        current()
        report({"type": "stage", "stage": "encoding"})
        wav, duration = await asyncio.to_thread(join_chunks, chunks, folder, config)
        silk = folder / "voice.silk"
        digest = await asyncio.to_thread(encode_silk, wav, silk, config.bit_rate)
        current()
        if silk.stat().st_size > config.max_audio_mb * 1024 * 1024:
            raise VoiceError("SILK 超过大小限制。")
        if config.reuse_audio:
            cache.mkdir(parents=True, exist_ok=True)
            # The digest manifest is published last; partial cache writes cannot be reused.
            for file in (wav, silk):
                temporary = cache / (file.name + "." + uuid.uuid4().hex + ".tmp")
                await asyncio.to_thread(shutil.copyfile, file, temporary)
                temporary.replace(cache / file.name)
            saved = {"id": identifier, "voice.wav": hashlib.sha256(wav.read_bytes()).hexdigest(), "voice.silk": digest}
            temporary = cache / (uuid.uuid4().hex + ".tmp")
            temporary.write_text(json.dumps(saved), encoding="utf-8")
            temporary.replace(cache / "cache.json")
        return wav, silk, duration, digest

    task = asyncio.create_task(render())
    try:
        async with asyncio.timeout(config.timeout_seconds):
            while not task.done():
                current()
                await asyncio.wait({task}, timeout=.1)
            wav, silk, duration, digest = task.result()
        current()
        manifest.update(state="ready", duration=duration, sample_rate=24000, wav_path=str(wav), silk_path=str(silk),
            sha256=digest, manifest_path=str(manifest_path), completed_at=time.time(),
            metrics={"total_ms": round((time.perf_counter() - started) * 1000, 2),
                     "first_audio_ms": None if first_audio is None else round((first_audio - started) * 1000, 2)})
        save()
        report({"type": "stage", "stage": "ready", "metrics": manifest["metrics"]})
        return manifest
    except BaseException as error:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        cancelled = isinstance(error, asyncio.CancelledError) or type(error).__name__ in ("TrainingCancelled", "StaleContext")
        manifest.update(state="cancelled" if cancelled else "failed", error=str(error)[:1000], completed_at=time.time())
        save()
        raise
