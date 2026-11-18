"""Official DeepSeek Harness backend selection; secrets stay in environment/home."""
from typing import Literal

from pydantic import BaseModel, Field


class ModelBackendConfig(BaseModel):
    backend: Literal["antigravity", "deepseek_harness"] = Field(default="antigravity", description="主代理和执行代理使用的模型后端")


class DSHConfig(BaseModel):
    home: str = Field(default="data/dsh", description="专用 DSH Home，保存凭据和真实持久会话；建议与日常 DSH 分开")
    workspace: str = Field(default=".", description="DSH 代理工作区")
    provider: str = Field(default="deepseek-official", min_length=1, max_length=100)
    model: str = Field(default="deepseek-v4-flash", min_length=1, max_length=200)
    reasoning_effort: str = Field(default="", max_length=80, description="留空沿用模型默认推理强度")
    max_tokens: int = Field(default=0, ge=0, le=262144, description="0 沿用提供方输出上限")
    base_url: str = Field(default="", description="可选 DeepSeek API 地址；留空沿用官方 SDK/环境设置")
    api_key_env: str = Field(default="DEEPSEEK_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", description="API Key 的环境变量名，不在机器人配置中保存密钥")
    initialize_timeout_seconds: int = Field(default=40, ge=5, le=180)
    model_aliases: dict[str, str] = Field(default_factory=lambda: {
        "flash": "deepseek-v4-flash", "pro": "deepseek-v4-pro", "flash_lite": "deepseek-v4-flash"},
        description="兼容模型切换命令的 tier 到 DSH 模型 ID 映射")
