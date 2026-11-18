"""GPT-SoVITS adapters, serialized model selection, WAV joining and QQ SILK."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import time
import uuid
import wave

import aiohttp

from voice_config import VoiceConfig
from voice_text import VoiceError, inline_emotions, japanese_tokens, prepare_chunks, PROSODY_INSTRUCTION, PROSODY_SPEEDS
from audio_processing import clean_reference, denoise_filter


SILK_HEADER = b"\x02#!SILK_V3"


def load_catalog(config):
    path = Path(config.catalog_path).resolve()
    if not path.is_file():
        raise VoiceError("语音样本目录不存在，请先在语音工作室配置角色与情绪切片。")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise VoiceError("语音样本目录过大。")
    catalog = json.loads(path.read_text(encoding="utf-8-sig"))
    if catalog.get("version") != 1 or not isinstance(catalog.get("characters"), dict):
        raise VoiceError("语音样本目录格式无效，需要 version: 1 和 characters。")
    return catalog


def voice_context(config):
    """Small, path-free capability for the main agent; no network on each turn."""
    result = {"enabled": config.enabled, "language": config.default_language,
              "character": config.character, "emotions": [],
              "streaming": config.streaming, "inline_emotion": True,
              "prosody": PROSODY_SPEEDS,
              "instruction": "选择语音必须显式 emotion 作为默认情绪。spoken_text 可在句中多次插入 [emotion:可用标签]，持续到下个标记；[emotion:] 恢复默认。标签不朗读，未知标签拒绝。日语台词严格转假名，不翻译中文。mixed 可用 [ja]…[/ja] [zh]…[/zh] [en]…[/en]。QQ 需要完整 SILK 后发送，控制台可边生成边听。" + PROSODY_INSTRUCTION}
    from singing_service import singing_context
    result["singing"] = singing_context(config)
    if not config.enabled:
        return result
    try:
        character = load_catalog(config)["characters"][config.character]
        emotions = character.get("emotions", {})
        result["emotions"] = list(emotions)
        result["emotion_descriptions"] = {key: value.get("description", key) for key, value in emotions.items()}
    except (KeyError, ValueError, OSError) as error:
        result["error"] = str(error)
    return result


@asynccontextmanager
async def engine_lock(endpoint):
    """Interprocess lock also covers previews in Studio and all synthesis chunks."""
    root = Path("data/voice/locks").resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / (hashlib.sha256(endpoint.encode()).hexdigest()[:24] + ".lock")
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                await asyncio.sleep(0.15)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


async def run_ffmpeg(config, source, target):
    try:
        process = await asyncio.create_subprocess_exec(config.ffmpeg_path, "-nostdin", "-v", "error", "-y",
            "-i", str(source), "-vn", "-t", str(config.max_duration_seconds + 1),
            "-ar", "24000", "-ac", "1", "-af", denoise_filter(config.denoise), "-c:a", "pcm_s16le", str(target),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            **({"creationflags": 0x08000000} if os.name == "nt" else {}))
    except FileNotFoundError:
        raise VoiceError("找不到 ffmpeg，请在语音设置中填写可执行文件路径。") from None
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), 60)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        raise VoiceError("语音 PCM 转换失败：" + stderr.decode("utf-8", errors="replace")[-400:])


def encode_silk(wav_path, silk_path, bit_rate=24000):
    from pysilk import encode
    with wave.open(str(wav_path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 24000):
            raise VoiceError("SILK 输入必须为 24kHz 单声道 16bit PCM。")
        pcm = audio.readframes(audio.getnframes())
    output = io.BytesIO()
    encode(io.BytesIO(pcm), output, sample_rate=24000, bit_rate=bit_rate, tencent=True)
    data = output.getvalue()
    if not data.startswith(SILK_HEADER) or len(data) <= len(SILK_HEADER) + 2:
        raise VoiceError("编码器未生成有效腾讯 SILK 语音。")
    Path(silk_path).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


class VoiceService:
    def __init__(self, config: VoiceConfig):
        self.config = config

    async def _request(self, client, method, endpoint, *, params=None, body=None):
        async with client.request(method, self.config.endpoint + endpoint, params=params, json=body,
                                  headers=self._studio_headers() if self.config.provider == "voicestudio" else None,
                                  allow_redirects=False) as response:
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > self.config.max_audio_mb * 1024 * 1024:
                    raise VoiceError("推理响应超过设置的语音大小上限。")
            if response.status != 200:
                raise VoiceError(f"GPT-SoVITS 接口 {endpoint} 返回 HTTP {response.status}：" + bytes(data).decode("utf-8", errors="replace")[:400])
            return bytes(data)

    def _selection(self, emotion):
        if not self.config.enabled:
            raise VoiceError("语音输出尚未启用。")
        if not emotion:
            raise VoiceError("选择语音时必须显式指定 emotion 情绪标签。")
        try:
            character = load_catalog(self.config)["characters"][self.config.character]
            reference = character["emotions"][emotion]
        except KeyError:
            raise VoiceError("角色或情绪不在已配置样本目录中；请先查询 voice 可用标签。") from None
        if self.config.provider == "gpt_sovits_v2" or self.config.provider == "voicestudio" and self.config.voicestudio.local_bridge:
            for key in ("gpt_weights", "sovits_weights"):
                if not character.get(key):
                    raise VoiceError("当前角色没有配置 " + key)
            if not reference.get("reference_audio") or not reference.get("prompt_text"):
                raise VoiceError("所选情绪缺少真实参考音频或参考文字。")
        return character, reference

    async def check(self):
        result = voice_context(self.config)
        if self.config.auto_start and self.config.provider == "voicestudio":
            from voice_engine import ready
            await ready(self.config)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:
            if self.config.provider == "voicestudio":
                result["engines"] = json.loads(await self._request(client, "GET", "/engines/tts"))
                result["profiles"] = json.loads(await self._request(client, "GET", "/profiles"))
                profiles = result["profiles"]
                if not isinstance(profiles, list):
                    raise VoiceError("VoiceStudio /profiles 未返回声音列表。")
                ids = {row.get("id") for row in profiles if isinstance(row, dict)}
                character = load_catalog(self.config)["characters"].get(self.config.character, {})
                missing = [tag for tag, reference in character.get("emotions", {}).items()
                           if (reference.get("voicestudio_profile_id") or character.get("voicestudio_profile_id")
                               or "qqbot:" + self.config.character + ":" + tag) not in ids]
                if missing:
                    raise VoiceError("VoiceStudio 缺少所选角色的声音配置：" + ", ".join(missing))
                url = self.config.endpoint.replace("http:", "ws:", 1).replace("https:", "wss:", 1)
                headers = self._studio_headers()
                async with client.ws_connect(url + "/ws/tts", headers=headers, max_msg_size=131072):
                    result["websocket"] = True
            elif self.config.provider == "gpt_sovits_inference":
                data = json.loads(await self._request(client, "GET", "/character_list"))
                if not isinstance(data, dict):
                    raise VoiceError("Inference /character_list 未返回角色情绪映射。")
                result["server_characters"] = data
                result["server_emotions"] = data.get(self.config.character, [])
            else:
                data = json.loads(await self._request(client, "GET", "/openapi.json"))
                if "/tts" not in data.get("paths", {}):
                    raise VoiceError("该服务没有 api_v2 /tts 接口。")
            result["reachable"] = True
        return result

    def _studio_headers(self):
        token = os.environ.get(self.config.voicestudio.api_key_env, "")
        return {"Authorization": "Bearer " + token} if token else {}

    async def synthesize(self, text, emotion, *, spoken_text="", check_current=None, progress=None):
        from voice_synthesis import synthesize
        return await synthesize(self, text, emotion, spoken_text=spoken_text,
                                check_current=check_current, progress=progress)
