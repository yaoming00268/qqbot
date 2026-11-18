"""Typed controls for the optional local voice output pipeline."""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator
from singing_config import SingingConfig
from audio_processing import DenoiseConfig
from voice_training_config import VoiceTrainingConfig


class VoiceStudioConfig(BaseModel):
    source_directory: str = "tools/voicestudio-upstream"
    backend_endpoint: str = "http://127.0.0.1:9880"
    engine: str = Field(default="gpt-sovits", max_length=100)
    local_bridge: bool = Field(default=True, description="使用固定版本 VoiceStudio 的轻量本机流式服务")
    fallback_enabled: bool = Field(default=True, description="流开始前服务连接失败时使用原 GPT-SoVITS；内容错误不重试")
    api_key_env: str = Field(default="VOICESTUDIO_API_KEY", max_length=100)

    @field_validator("backend_endpoint")
    @classmethod
    def backend_valid(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("VoiceStudio 后端必须为不含凭据、查询和片段的 HTTP(S) 地址")
        return value.rstrip("/")


class VoiceConfig(BaseModel):
    process_namespace: Literal["qq", "broadcast"] = Field(default="qq", description="隔离 QQ 与直播引擎的进程记录、日志和启停操作")
    training: VoiceTrainingConfig = Field(default_factory=VoiceTrainingConfig)
    singing: SingingConfig = Field(default_factory=SingingConfig)
    enabled: bool = Field(default=False, description="允许主代理通过 reply --voice --emotion 选择语音回复")
    provider: Literal["gpt_sovits_v2", "gpt_sovits_inference", "voicestudio"] = Field(
        default="gpt_sovits_v2", description="VoiceStudio 流式服务、api_v2 或角色/情绪接口")
    voicestudio: VoiceStudioConfig = Field(default_factory=VoiceStudioConfig)
    denoise: DenoiseConfig = Field(default_factory=DenoiseConfig)
    streaming: bool = Field(default=True, description="接收分片 PCM；控制台可边合成边听，QQ 在完整 SILK 编码后发送")
    stream_chunk_ms: int = Field(default=80, ge=20, le=500)
    crossfade_ms: int = Field(default=8, ge=0, le=30, description="情绪边界的短交叉淡化，不额外插入句间停顿")
    reuse_models: bool = Field(default=True, description="本项目拥有的推理进程中复用已确认的角色权重选择")
    reuse_audio: bool = Field(default=True, description="按台词、情绪、模型和参考切片内容复用已验证的完整语音")
    sample_steps: int = Field(default=16, ge=4, le=64, description="GPT-SoVITS v3/v4 采样步数，越高越慢")
    endpoint: str = Field(default="http://127.0.0.1:9880", description="所选语音服务的 HTTP 根地址")
    auto_start: bool = Field(default=False, description="需要合成时启动已安装的 VoiceStudio 与本地推理引擎")
    engine_python: str = Field(default="tools/voice-engine/.venv/Scripts/python.exe", description="独立 GPU 推理环境的 Python")
    engine_directory: str = Field(default="tools/voice-engine/GPT-SoVITS-Inference", description="推理引擎工作目录")
    engine_config: str = Field(default="tools/voice-engine/atri-infer.yaml", description="api_v2 推理 YAML 配置")
    default_language: Literal["ja", "zh", "en", "mixed"] = Field(default="ja", description="语音语言；日语严格假名化，mixed 支持 [ja]/[zh]/[en] 标记")
    character: str = Field(default="atri", max_length=100, description="本地样本目录中的角色 ID，或 Inference 服务的角色名")
    catalog_path: str = Field(default="data/voice/catalog.json", description="角色模型与情绪参考切片的本地 JSON 目录")
    cache_dir: str = Field(default="data/cache/voice", description="语音任务、WAV、SILK 和分词清单目录")
    dictionary: dict[str, str] = Field(default_factory=lambda: {"ATRI": "あとり", "アトリ": "あとり"}, description="日语专名读音词典；值必须为假名")
    chunk_chars: int = Field(default=100, ge=20, le=500, description="每次合成的最大字符数，按标点和分词边界切分")
    max_text_chars: int = Field(default=4000, ge=100, le=16000)
    max_duration_seconds: int = Field(default=120, ge=1, le=600)
    max_audio_mb: int = Field(default=32, ge=1, le=128)
    timeout_seconds: int = Field(default=180, ge=5, le=1800)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    pause_ms: int = Field(default=120, ge=0, le=1000)
    ffmpeg_path: str = Field(default="ffmpeg", description="ffmpeg 可执行文件名或完整路径")
    bit_rate: int = Field(default=24000, ge=8000, le=40000)

    @field_validator("endpoint")
    @classmethod
    def endpoint_valid(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("语音接口必须为不含凭据、查询和片段的 HTTP(S) 地址")
        return value.rstrip("/")

    @field_validator("dictionary")
    @classmethod
    def kana_dictionary(cls, value):
        import re
        if len(value) > 1000:
            raise ValueError("日语读音词典最多 1000 项")
        for key, reading in value.items():
            if not key or not re.fullmatch(r"[\u3041-\u3096\u30a1-\u30faー ・]+", reading):
                raise ValueError("读音词典的键不能为空，读音必须为假名")
        return value


class BroadcastVoiceConfig(VoiceConfig):
    """Independent live profile. Reuses TTS settings, never QQ delivery or SILK."""
    process_namespace: Literal["broadcast"] = "broadcast"
    enabled: bool = Field(default=False, description="启用独立的直播语音管线，与 QQ 语音开关分开")
    provider: Literal["gpt_sovits_v2", "voicestudio"] = Field(default="gpt_sovits_v2", description="直播专用流式推理接口")
    endpoint: str = Field(default="http://127.0.0.1:9882", description="直播专用接口地址；默认与 QQ 推理服务分开")
    voicestudio: VoiceStudioConfig = Field(default_factory=lambda: VoiceStudioConfig(backend_endpoint="http://127.0.0.1:9882"))
    delivery_mode: Literal["streaming", "validated"] = Field(default="streaming", description="streaming 即时输出 PCM；validated 先校验片段再播放，适合易截断的模型")
    stream_chunk_ms: int = Field(default=40, ge=20, le=500, description="直播播放块大小，不控制模型本身的首包大小")
    cache_dir: str = Field(default="data/cache/broadcast-voice", description="独立的直播音频与诊断目录，不读取 QQ 完整语音缓存")
    denoise: DenoiseConfig = Field(default_factory=DenoiseConfig, description="清理并缓存参考音频；即时模式的输出降噪由流式后端负责，校验模式可处理整段输出")
    max_ahead_seconds: float = Field(default=0.5, ge=0.1, le=3.5, description="最多提前向直播播放器提交的音频秒数")
    queue_blocks: int = Field(default=8, ge=1, le=32, description="直播 PCM 有界队列长度，慢播放器会向推理端施加背压")

    @model_validator(mode="after")
    def live_endpoints_and_buffer(self):
        if self.provider == "voicestudio" and self.voicestudio.local_bridge:
            def address(value):
                parsed = urlsplit(value)
                host = "127.0.0.1" if parsed.hostname in ("localhost", "127.0.0.1") else parsed.hostname
                return parsed.scheme, host, parsed.port or (443 if parsed.scheme == "https" else 80), parsed.path.rstrip("/")
            if address(self.endpoint) == address(self.voicestudio.backend_endpoint):
                raise ValueError("直播 VoiceStudio 与原生后端不能占用同一地址；例如前端 9883、后端 9882。")
        if self.max_ahead_seconds < self.stream_chunk_ms / 1000 + .025:
            raise ValueError("直播提前缓冲需至少容纳一个音频块及 25 毫秒播放调度余量。")
        return self
