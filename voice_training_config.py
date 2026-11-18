"""Small, serializable training controls; GPU libraries stay outside the Bot."""
from pydantic import BaseModel, Field
from typing import Literal


class VoiceTrainingConfig(BaseModel):
    python: str = "tools/voice-engine/.train-venv/Scripts/python.exe"
    directory: str = "data/voice/training"
    version: Literal["v2ProPlus", "v4"] = "v2ProPlus"
    batch_size: int = Field(default=2, ge=1, le=16)
    sovits_epochs: int = Field(default=8, ge=1, le=100)
    gpt_epochs: int = Field(default=10, ge=1, le=100)
    save_every: int = Field(default=1, ge=1, le=20)
    lora_rank: Literal[8, 16, 32, 64] = 16
    validation_ratio: float = Field(default=.15, ge=.05, le=.4)
    seed: int = Field(default=42, ge=0, le=2147483647)
    asr_model: str = "Systran/faster-whisper-small"
    asr_device: Literal["cpu", "cuda"] = "cpu"
    asr_compute_type: Literal["int8", "float16", "int8_float16"] = "int8"
    timeout_seconds: int = Field(default=21600, ge=60, le=172800)
