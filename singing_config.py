"""Settings for isolated singing engines (never import torch in the bot)."""
from typing import Literal
from pydantic import BaseModel, Field
from audio_processing import DenoiseConfig


class SingingConfig(BaseModel):
    denoise: DenoiseConfig = Field(default_factory=DenoiseConfig)
    reuse_separation: bool = Field(default=True, description="按原曲内容与固定模型版本复用已成功分离的分轨")
    enabled: bool = Field(default=False, description="允许主代理提交唱歌任务")
    engine_python: str = Field(default="tools/singing-engine/.venv/Scripts/python.exe", description="Seed-VC 与人声分离独立环境")
    engine_directory: str = Field(default="tools/singing-engine/seed-vc", description="Seed-VC 引擎目录")
    generation_python: str = Field(default="tools/singing-engine/.ace-venv/Scripts/python.exe", description="ACE-Step 歌声生成独立环境")
    generation_directory: str = Field(default="tools/singing-engine/ace-step", description="ACE-Step 1.5 引擎目录，使用支持 lego 的 base 模型")
    cache_dir: str = Field(default="data/cache/singing", description="歌曲、分轨、歌词及生成清单")
    device: Literal["cuda", "cpu"] = "cuda"
    max_duration_seconds: int = Field(default=360, ge=5, le=600, description="歌曲最长秒数；超过即拒绝，不静默截断")
    max_input_mb: int = Field(default=100, ge=1, le=500)
    max_lyrics_chars: int = Field(default=4000, ge=100, le=16000)
    timeout_seconds: int = Field(default=3600, ge=30, le=14400)
    diffusion_steps: int = Field(default=30, ge=5, le=100, description="角色歌声转换步数")
    generation_steps: int = Field(default=50, ge=8, le=200, description="伴奏补全人声的生成步数")
    vocal_gain_db: float = Field(default=0, ge=-24, le=12)
    accompaniment_gain_db: float = Field(default=-3, ge=-24, le=12)
    max_voice_seconds: int = Field(default=360, ge=5, le=600, description="QQ 语音最长秒数；长歌曲建议文件输出")
    suspend_owned_tts: bool = Field(default=True, description="唱歌期间释放由本项目启动的 TTS 显存，完成后恢复；不关闭外部服务")


class SingingRequest(BaseModel):
    """Audio identifiers are resolved by the console before reaching the service."""
    model_config = {"extra": "forbid"}
    mode: Literal["cover", "compose"] = "cover"
    song: str = Field(min_length=1, max_length=8192)
    lyrics: str = Field(min_length=1, max_length=16000)
    accompaniment: str = Field(default="", max_length=8192)
    vocals_only: bool = False
    emotion: str = Field(default="neutral", min_length=1, max_length=100)
    language: Literal["ja", "zh", "en", "unknown"] = "ja"
    style: str = Field(default="Expressive clear solo female singing, melodic and natural.", max_length=500)
    pitch_shift: int = Field(default=0, ge=-12, le=12)
    song_output: Literal["file", "voice", "both"] = "file"
    seed: int = Field(default=42, ge=0, le=2147483647)
