"""Validate and reuse completed PCM WAVs without a redundant FFmpeg process."""
from __future__ import annotations

from pathlib import Path
import shutil
import wave

from voice_text import VoiceError


def copy_ready_pcm(source, target, config):
    """Copy only complete 24 kHz mono PCM16; return False when conversion is needed.

    Callers decide whether denoising has already been performed. Header agreement
    alone is insufficient: reject truncated files and validate the complete payload.
    """
    source, target = Path(source), Path(target)
    try:
        with wave.open(str(source), "rb") as stream:
            if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype()) != (1, 2, 24000, "NONE"):
                return False
            frames = stream.getnframes()
            if not frames or frames / 24000 > config.max_duration_seconds or frames * 2 > config.max_audio_mb * 1024 * 1024:
                raise VoiceError("PCM 音频为空或超过大小／时长限制。")
            remaining = frames * 2
            while remaining:
                data = stream.readframes(min(65536, (remaining + 1) // 2))
                if not data or len(data) % 2 or len(data) > remaining:
                    raise VoiceError("PCM WAV 在完成前中断或包含不完整采样。")
                remaining -= len(data)
    except (wave.Error, EOFError):
        return False
    shutil.copyfile(source, target)
    return True
