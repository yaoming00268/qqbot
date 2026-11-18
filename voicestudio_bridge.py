"""Local streaming extension of VoiceStudio's GPT-SoVITS backend.

This headless integration imports the pinned upstream adapter and sentence
chunker. The upstream source and AGPL-3.0 notices remain in its installation;
this integration's source is shipped beside the executable, under AGPL-3.0.
It implements VoiceStudio's start / PCM / done WebSocket protocol and adds
native api_v2 streaming, reference selection, denoising and model reuse.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

# Standalone helpers ship in a sealed resource tree beside the executable.
sys.dont_write_bytecode = True

from aiohttp import web, ClientSession, ClientTimeout, WSMsgType

from audio_processing import clean_reference, denoise_filter
from voice_config import VoiceConfig
from voice_model_cache import select_models
from voice_stream import WavStreamDecoder
from voice_text import completion_limit, japanese_tokens, prepare_chunks

UPSTREAM_REVISION = "4da51ddb4841e04e1e9878f9934fde46c21389f9"


def profile_id(character, emotion):
    return "qqbot:" + character + ":" + emotion


def create_app(config):
    source = Path(config.voicestudio.source_directory).resolve()
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(source / "backend"))
    from services.tts_backend import GPTSoVITSBackend
    from services.sentence_chunker import SentenceChunker
    from services.expression import expression_caps

    class StreamingGPTSoVITS(GPTSoVITSBackend):
        """Keep upstream language validation and backend identity, extend transport."""
        def __init__(self):
            self._url = config.voicestudio.backend_endpoint

        async def stream(self, client, body):
            response = await client.post(self._url + "/tts", json=body, allow_redirects=False)
            if response.status != 200:
                detail = await response.content.read(800)
                response.close()
                raise ValueError(f"GPT-SoVITS HTTP {response.status}: " + detail.decode("utf-8", errors="replace"))
            return response

    backend = StreamingGPTSoVITS()
    lock = asyncio.Lock()

    def profiles():
        catalog = json.loads(Path(config.catalog_path).read_text(encoding="utf-8-sig"))
        return {profile_id(char, tag): (char, tag, character, ref)
                for char, character in catalog["characters"].items()
                for tag, ref in character.get("emotions", {}).items()}

    @web.middleware
    async def local_only(request, handler):
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise web.HTTPForbidden(text="This is a local voice service.")
        return await handler(request)

    app = web.Application(client_max_size=128 * 1024, middlewares=[local_only])
    native_client = None

    async def upstream_session(app):
        nonlocal native_client
        async with ClientSession(timeout=ClientTimeout(total=config.timeout_seconds)) as client:
            native_client = client
            try:
                yield
            finally:
                native_client = None

    app.cleanup_ctx.append(upstream_session)

    async def health(request):
        return web.json_response({"ok": True, "integration": "qqbot-voicestudio", "revision": UPSTREAM_REVISION,
                                  "engine": backend.id, "native_stream": True, "denoise": config.denoise.enabled})

    async def engines(request):
        return web.json_response([{"id": backend.id, "available": True, "expression_caps": {
            **expression_caps(type(backend)), "reference_emotions": True}, "native_stream": True}])

    async def list_profiles(request):
        return web.json_response([{"id": key, "name": char + " / " + tag, "kind": "clone"}
                                  for key, (char, tag, _, _) in profiles().items()])

    async def render(ws, data):
        text = data.get("text", "")
        if not isinstance(text, str) or not text.strip() or len(text) > config.max_text_chars:
            raise ValueError("朗读台词为空或超过限制。")
        if data.get("engine", backend.id) != backend.id:
            raise ValueError("此轻量服务使用 gpt-sovits；其他引擎请连接完整 VoiceStudio 服务。")
        selected = profiles().get(data.get("voice"))
        if selected is None:
            raise ValueError("声音或情绪参考不存在；请重新查询 /profiles。")
        _, tag, character, reference = selected
        language = backend._check_language(data.get("language", "ja"))
        if language == "ja":
            text = "".join(japanese_tokens(text, config.dictionary))
        prompt = reference.get("prompt_text", "")
        prompt_language = reference.get("prompt_language", "ja")
        if prompt_language == "ja":
            prompt = "".join(japanese_tokens(prompt, config.dictionary))
        ref, denoise_info = await clean_reference(reference["reference_audio"], config.denoise,
            ffmpeg=config.ffmpeg_path, cache_root=config.cache_dir)
        speed = data.get("speed", 1.0)
        if type(speed) not in (float, int) or not .5 <= speed <= 2:
            raise ValueError("速度必须为 0.5–2。")
        seed = data.get("seed", 42)
        parallel = data.get("parallel_infer", True)
        if type(seed) is not int or not -1 <= seed <= 2147483647 or type(parallel) is not bool:
            raise ValueError("无效的合成随机种子或并行选项。")
        limit = completion_limit(character)
        if limit:
            # Apply the same protection to direct VoiceStudio clients. Keep every
            # character, including the last incomplete sentence, in the request.
            options = config.model_copy(update={"default_language": language})
            sentences = [chunk.text for chunk in prepare_chunks(text, options, tag, clause_limit=limit)]
        else:
            splitter = SentenceChunker(language=language)
            sentences = splitter.push(text) + splitter.flush()
        started, byte_count, first, t0 = False, 0, None, time.perf_counter()
        model_info = None
        async with lock:
            client = native_client
            if client is None:
                raise ValueError("推理连接尚未就绪。")
            async def select(route, params):
                async with client.get(backend._url + route, params=params, allow_redirects=False) as response:
                    content = await response.content.read(4096)
                    if response.status != 200:
                        raise ValueError(f"模型切换 HTTP {response.status}")
                    return content
            model_info = await select_models(character, backend._url, select, reuse=config.reuse_models)
            for sentence in sentences:
                body = {"text": sentence, "text_lang": language, "ref_audio_path": ref,
                    "prompt_text": prompt, "prompt_lang": prompt_language, "text_split_method": "cut0",
                    "batch_size": 1, "speed_factor": speed, "media_type": "wav", "streaming_mode": True,
                    "parallel_infer": parallel, "sample_steps": config.sample_steps, "seed": seed}
                decoder, process, sending = WavStreamDecoder(), None, None
                response = await backend.stream(client, body)
                async def send(pcm, rate=24000, channels=1):
                    nonlocal started, byte_count, first
                    if not started:
                        await ws.send_json({"type": "start", "sample_rate": rate, "channels": channels,
                                            "format": "pcm16", "engine": backend.id})
                        started = True
                    byte_count += len(pcm)
                    if (byte_count > config.max_audio_mb * 1024 * 1024
                            or byte_count / 48000 > config.max_duration_seconds):
                        raise ValueError("音频超过大小或时长限制。")
                    for offset in range(0, len(pcm), 24000 * config.stream_chunk_ms // 1000 * 2):
                        await ws.send_bytes(pcm[offset:offset + 24000 * config.stream_chunk_ms // 1000 * 2])
                        if first is None:
                            first = time.perf_counter()
                async def drain():
                    size = 24000 * config.stream_chunk_ms // 1000 * 2
                    while True:
                        try:
                            block = await process.stdout.readexactly(size)
                        except asyncio.IncompleteReadError as error:
                            if error.partial:
                                await send(error.partial)
                            break
                        await send(block)
                try:
                    async for block in response.content.iter_chunked(8192):
                        pcm = decoder.feed(block)
                        if decoder.format and process is None:
                            rate, channels = decoder.format
                            process = await asyncio.create_subprocess_exec(config.ffmpeg_path, "-nostdin", "-v", "error",
                                "-f", "s16le", "-ar", str(rate), "-ac", str(channels), "-probesize", "32",
                                "-analyzeduration", "0", "-i", "pipe:0", "-af", denoise_filter(config.denoise),
                                "-ar", "24000", "-ac", "1", "-f", "s16le", "pipe:1",
                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE, **({"creationflags": 0x08000000} if os.name == "nt" else {}))
                            sending = asyncio.create_task(drain())
                        if pcm:
                            if sending.done():
                                await sending
                            process.stdin.write(pcm)
                            await process.stdin.drain()
                    decoder.finish()
                    if process is None:
                        raise ValueError("推理未返回音频。")
                    process.stdin.close()
                    await sending
                    error = await process.stderr.read()
                    await process.wait()
                    if process.returncode:
                        raise ValueError("流式降噪／重采样失败：" + error.decode("utf-8", errors="replace")[-500:])
                finally:
                    response.close()
                    if process and process.returncode is None:
                        process.kill()
                        await process.wait()
                    if sending and not sending.done():
                        sending.cancel()
                        await asyncio.gather(sending, return_exceptions=True)
        if not byte_count or byte_count % 2:
            raise ValueError("推理没有生成完整 PCM 音频。")
        await ws.send_json({"type": "done", "audio_bytes": byte_count, "duration_s": byte_count / 48000,
            "gen_time_s": time.perf_counter() - t0, "ttfa_ms": (first - t0) * 1000,
            "emotion": tag, "model_selection": model_info, "denoise": denoise_info,
            "output_denoise": config.denoise.enabled})

    async def websocket(request):
        ws = web.WebSocketResponse(max_msg_size=128 * 1024, heartbeat=20)
        await ws.prepare(request)
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                break
            try:
                data = json.loads(message.data)
                work = asyncio.create_task(render(ws, data))
                disconnected = asyncio.create_task(ws.receive())
                try:
                    done, _ = await asyncio.wait({work, disconnected}, return_when=asyncio.FIRST_COMPLETED)
                    if disconnected in done:
                        work.cancel()
                        await asyncio.gather(work, return_exceptions=True)
                        break
                    await work
                finally:
                    disconnected.cancel()
                    await asyncio.gather(disconnected, return_exceptions=True)
                    if not work.done():
                        work.cancel()
                        await asyncio.gather(work, return_exceptions=True)
            except Exception as error:
                if ws.closed:
                    break
                await ws.send_json({"type": "error", "detail": str(error)[:1000]})
        return ws

    app.router.add_get("/health", health)
    app.router.add_get("/engines/tts", engines)
    app.router.add_get("/profiles", list_profiles)
    app.router.add_get("/ws/tts", websocket)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    os.chdir(args.home)
    config = VoiceConfig.model_validate_json(args.config.read_text(encoding="utf-8"))
    web.run_app(create_app(config), host="127.0.0.1", port=args.port, print=None, access_log=None)


if __name__ == "__main__":
    main()
