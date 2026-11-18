"""Optional Bilibili operations. Account secrets never belong in AppConfig."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, model_validator


class BilibiliAutonomyConfig(BaseModel):
    enabled: bool = Field(default=False, description="启用 AI 自主逛 B 站和账号运营；需启动 Bot 核心")
    actor_id: str = Field(default="", max_length=120, description="账号运营角色；与直播弹幕权限独立")
    interval_minutes: int = Field(default=15, ge=1, le=1440, description="自主活动间隔（分钟）")
    interests: list[str] = Field(default_factory=list, max_length=20, description="兴趣话题列表，例如 [\"游戏\", \"音乐\"]")
    goal: str = Field(default="阅读感兴趣的内容，形成自己的观点，认真回复相关讨论，记录选题并运营自己的账号。", max_length=3000, description="账号运营目标")
    promotion_text: str = Field(default="", max_length=2000, description="账号介绍或宣传资料；只在相关内容中自然介绍，不重复刷广告")
    allow_comments: bool = Field(default=False, description="允许 AI 自主发表评论和楼中楼回复")
    allow_likes: bool = Field(default=False, description="允许 AI 自主点赞")
    allow_dynamics: bool = Field(default=False, description="允许 AI 自主发布动态")
    allow_video_uploads: bool = Field(default=False, description="允许 AI 发布授权素材目录内的视频草稿")
    allow_profile_update: bool = Field(default=False, description="允许 AI 更新账号个性签名，保留昵称和身份资料")
    max_actions_per_hour: int = Field(default=6, ge=1, le=60, description="每小时最多外发动作")
    max_comments_per_day: int = Field(default=20, ge=1, le=200, description="每天最多评论与回复")
    min_write_interval_seconds: int = Field(default=120, ge=30, le=3600, description="外发动作最短间隔（秒）")
    max_thread_replies: int = Field(default=2, ge=1, le=10, description="同一讨论串最多参与次数，避免反复纠缠")


class LiveHimeConfig(BaseModel):
    executable: str = Field(default="", max_length=4096, description="哔哩哔哩直播姬 EXE 路径；留空自动检测")
    transport: Literal["window", "obs_virtual_camera"] = Field(default="window", description="直播姬画面接入方式：window 窗口捕获，obs_virtual_camera OBS 虚拟摄像头")
    audio_device: str = Field(default="", max_length=256, description="直播姬选择的音频设备；虚拟摄像头不传音频")

    @model_validator(mode="after")
    def known_transport(self):
        if self.transport not in {"window", "obs_virtual_camera"}:
            raise ValueError("直播姬接入方式必须为 window 或 obs_virtual_camera。")
        return self


class BilibiliConfig(BaseModel):
    enabled: bool = Field(default=False, description="启用 B 站直播与运营服务")
    host: str = Field(default="127.0.0.1", description="运营接口仅监听本机")
    port: int = Field(default=18766, ge=1024, le=65535)
    database_path: str = "data/broadcast/operations.sqlite3"
    credentials_file: str = Field(default="data/broadcast/bilibili-credentials.json", description="本机私有凭据 JSON；不在控制台回显")
    api_token_file: str = "data/broadcast/api-token.json"
    room_id: int = Field(default=0, ge=0, description="B 站直播间号")
    area_id: int = Field(default=0, ge=0, description="开播分区 ID")
    actor_id: str = Field(default="", max_length=120, description="接收弹幕的持续角色 ID；对应 agent.actors 中的条目")
    auto_listen: bool = Field(default=False, description="启动核心时连接配置的直播间弹幕")
    auto_reply: bool = Field(default=False, description="将去重、合并后的弹幕交给绑定角色，自主选择是否语音回复")
    batch_seconds: float = Field(default=3, ge=1, le=30, description="弹幕合并窗口，减少逐条触发模型")
    batch_limit: int = Field(default=8, ge=1, le=30)
    max_pending_events: int = Field(default=200, ge=10, le=3000)
    allow_publish: bool = Field(default=False, description="允许显式发布草稿、发送弹幕和切换 B 站开播状态")
    media_roots: list[str] = Field(default_factory=lambda: ["data/broadcast/media", "data/voice", "data/singing", "artifacts"], description="投稿与表演可读取的媒体目录")
    request_timeout_seconds: int = Field(default=120, ge=10, le=3600)
    autonomy: BilibiliAutonomyConfig = Field(default_factory=BilibiliAutonomyConfig)
    livehime: LiveHimeConfig = Field(default_factory=LiveHimeConfig)

    @model_validator(mode="after")
    def local_only(self):
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("B 站运营接口必须监听本机回环地址。")
        return self
