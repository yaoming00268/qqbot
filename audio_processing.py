"""Conservative, local FFmpeg noise reduction shared by speech and singing."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import uuid
import wave

from pydantic import BaseModel, Field


class DenoiseConfig(BaseModel):
    enabled: bool = True
    reference: bool = Field(default=True, description="缓存清理后的参考切片，不覆盖原文件")
    strength_db: float = Field(default=6, ge=0.01, le=24, description="频谱降噪量；过高可能损伤气声与歌声")
    noise_floor_db: float = Field(default=-50, ge=-80, le=-20)


def denoise_filter(config):
    if not config.enabled:
        return "anull"
    return f"afftdn=nr={config.strength_db:g}:nf={config.noise_floor_db:g}:tn=1:gs=10"


async def clean_reference(path, config, *, ffmpeg="ffmpeg", cache_root, check_current=None):
    """Content-addressed reference cache. Originals and transcripts remain intact."""
    source = Path(path).resolve()
    if not config.enabled or not config.reference:
        return str(source), {"enabled": False}
    if not source.is_file():
        # Remote api_v2 paths belong to that server; never substitute a local guess.
        return str(path), {"enabled": False, "reason": "reference is server-managed"}
    if source.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("参考切片超过 64MB。")
    data = await asyncio.to_thread(source.read_bytes)
    key = hashlib.sha256(data + json.dumps(config.model_dump(), sort_keys=True).encode() + b"afftdn-v1").hexdigest()
    root = Path(cache_root).resolve() / "references"
    root.mkdir(parents=True, exist_ok=True)
    target = root / (key + ".wav")
    if target.is_file():
        try:
            with wave.open(str(target), "rb") as audio:
                if (not 0 < audio.getnframes() / audio.getframerate() <= 30.01 or audio.getsampwidth() != 2
                        or len(audio.readframes(audio.getnframes())) != audio.getnframes() * audio.getnchannels() * 2):
                    raise ValueError("参考降噪缓存无效。")
            return str(target), {"enabled": True, "cache_hit": True, "sha256": key, "filter": denoise_filter(config)}
        except (OSError, ValueError, EOFError, wave.Error):
            target.unlink(missing_ok=True)
    temporary = root / (key + "." + uuid.uuid4().hex + ".wav")
    process = await asyncio.create_subprocess_exec(ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y",
        "-protocol_whitelist", "file,pipe", "-i", str(source), "-vn", "-t", "31", "-af", denoise_filter(config),
        "-c:a", "pcm_s16le", str(temporary), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        **({"creationflags": 0x08000000} if os.name == "nt" else {}))
    waiting = asyncio.create_task(process.communicate())
    try:
        async with asyncio.timeout(60):
            while not waiting.done():
                if check_current:
                    check_current()
                await asyncio.wait({waiting}, timeout=.1)
            _, error = waiting.result()
        if process.returncode:
            raise ValueError("参考降噪失败：" + error.decode("utf-8", errors="replace")[-500:])
        with wave.open(str(temporary), "rb") as audio:
            if not 0 < audio.getnframes() / audio.getframerate() <= 30.01:
                raise ValueError("参考切片必须小于 30 秒。")
        temporary.replace(target)
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.gather(waiting, return_exceptions=True)
        temporary.unlink(missing_ok=True)
    return str(target), {"enabled": True, "cache_hit": False, "sha256": key, "filter": denoise_filter(config)}
