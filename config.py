# -*- coding: utf-8 -*-
"""
Configuration loader for QQ Bot + Antigravity Gemini Relay.
"""

from __future__ import annotations
import os
from pathlib import Path
import shutil
from typing import Optional, Dict, Literal
import uuid
from pydantic import BaseModel, Field, model_validator
import yaml
from agent_core.configuration import AgentConfig



class QQConfig(BaseModel):
    appid: str = Field(default="", description="QQ 开放平台机器人 AppID")
    secret: str = Field(default="", description="QQ 开放平台机器人 AppSecret")
    is_sandbox: bool = Field(default=False, description="是否使用沙箱环境进行测试")


class AntigravityConfig(BaseModel):
    default_model: str = Field(default="flash", description="默认模型类型: flash, pro, flash_lite")
    ls_address: Optional[str] = Field(default=None, description="Antigravity Language Server 地址，如 127.0.0.1:7873（留空则自动检测）")
    csrf_token: Optional[str] = Field(default=None, description="Antigravity CSRF Token（留空则自动检测）")
    project_id: Optional[str] = Field(default=None, description="Antigravity Project ID（留空则自动检测）")
    language_server_path: Optional[str] = Field(default=None, description="language_server.exe 路径（留空则自动检测）")
    timeout_seconds: int = Field(default=0, ge=0, description="旧配置兼容；机器人主对话和后台任务均不限时，0 表示不限制")
    session_timeout_minutes: int = Field(default=30, ge=0, description="临时会话空闲清理时间（分钟），0 为不清理；群聊与私聊主会话始终保留")


WorkAccess = Literal["all", "admin", "master", "allowlist", "disabled"]


class WorkPermissions(BaseModel):
    access: WorkAccess = Field(default="all", description="后台工作权限：所有成员、管理员、主人、白名单或关闭")
    allowed_user_ids: list[str] = Field(default_factory=list, description="额外允许工作的稳定成员 ID；disabled 时不生效")
    task_access: Dict[Literal["search", "image", "generate_image", "generate_video", "video", "file",
                              "task", "command", "code", "process"], WorkAccess] = Field(
        default_factory=dict, description="按任务类型覆盖 access，如 code/command/search/process")


class BotBehaviorConfig(BaseModel):
    bot_name: str = Field(default="", max_length=40, description="命令设置的机器人名称；留空沿用人设名称")
    reply_char_limit: int = Field(default=0, ge=0, le=100000, description="普通聊天一轮回复的总字数；0 不限制，完整命令结果和文件报告不截断")
    admin_ids: list[str] = Field(default_factory=list, description="管理员 QQ/OpenID；官方通道使用此列表确认管理员身份")
    work_permissions: WorkPermissions = Field(default_factory=WorkPermissions)
    runtime_database_path: str = "data/runtime.sqlite3"
    passive_backlog: int = Field(default=2, ge=1, le=20)
    backlog_interrupt_messages: int = Field(default=3, ge=0, le=100, description="同会话积压多少条自然消息后中断当前回复并合并交给 AI；0 关闭，已发送内容保留，后台任务继续")
    chat_buffer_seconds: float = Field(default=5, ge=0, le=300, description="群消息固定缓存窗口秒数；回复完成后重新计时，0 使用旧的合并方式")
    chat_settle_seconds: float = Field(default=1.5, ge=0, le=10, description="旧版群聊安静等待秒数，仅固定缓存为0时使用")
    chat_max_wait_seconds: float = Field(default=6, ge=0, le=30, description="连续收消息时单批最多等待秒数，避免一直无法处理")
    delayed_quote_seconds: int = Field(default=8, ge=0, description="旧配置兼容；由控制台按是否已有新消息决定引用")
    idle_recall_minutes: int = Field(default=30, ge=1, le=1440, description="空闲时将新增事件概括到固定记忆文件的间隔；无新增或同批失败时不重复调用模型，保留主会话 ID")
    qq_window_capture: bool = True
    memory_archive_interval_minutes: int = Field(default=30, ge=1)
    system_prompt: str = Field(
        default="你有自己的性格、兴趣和判断，与熟悉的人自然交流，认真回应值得在意的事。",
        description="默认系统提示词"
    )
    max_reply_length: int = Field(default=1200, description="单条 QQ 消息分段阈值，非整轮回答总长度；按自然边界分段，平台回复额度不足时合并相邻段")
    strip_mentions: bool = Field(default=True, description="是否自动剔除消息开头的 @机器人 文本")

    # 动作与神态描写控制（开关为 False 时只输出纯台词对话，节约 Token 且杜绝冗余动作）
    enable_action_descriptions: bool = Field(
        default=True,
        description="是否允许输出动作神态描写（True=生动角色扮演，False=纯台词对话，节约输出Token与长度）"
    )

    # 群聊自主互动/插话功能
    autonomous_enabled: bool = Field(
        default=False,
        description="群聊新消息自动唤醒分析，无需 @；由模型判断是否适合接话"
    )
    never_sleep: bool = Field(default=True, description="空闲时保持参聊待命；关闭后按空闲休眠时间停止主动话题，新消息仍自动唤醒")
    idle_sleep_minutes: int = Field(default=30, ge=1, le=1440, description="关闭永不休眠时，群聊连续无新消息多久暂停主动话题（分钟）")
    proactive_chat_enabled: bool = Field(default=False, description="有想分享的见闻或愿望时主动聊天；仅在本次运行已收到消息的群生效")
    proactive_interval_minutes: int = Field(default=10, ge=1, le=1440, description="群聊空闲多久尝试主动话题；每次尝试与实际发言后重新计时（分钟）")
    self_activity_enabled: bool = Field(default=False, description="空闲时独立思考、记录兴趣并持续探索授权资料与公开网络；与主动聊天分别控制")
    self_activity_interval_minutes: int = Field(default=20, ge=1, le=1440, description="无人聊天时自主复盘与阅读的最小间隔（分钟），同一时间只保留一个探索任务")
    self_activity_roots: list[str] = Field(default_factory=list, description="额外允许自主阅读的资料目录；默认仅探索工作区、本地资料库和已有授权目录")
    reply_probability: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="已停用，仅兼容旧配置；群聊接话由 AI 根据上下文决定，此值不限制发言"
    )
    mention_probability: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="已停用，仅兼容旧配置；引用和 @ 由主代理通过 CLI 显式指定"
    )
    cooldown_seconds: int = Field(
        default=30,
        ge=0,
        description="已停用，仅兼容旧配置；连续接话不使用固定冷却，空闲话题单独计时"
    )
    context_message_count: int = Field(
        default=10,
        ge=0,
        le=50,
        description="回复时附带的近期群聊上下文最大条数"
    )
    master_id: str = Field(
        default="",
        description="主人 QQ OpenID（主人在群内发言 100% 必定回复）"
    )
    master_title: str = Field(
        default="主人",
        description="全局主人专属称谓（如：主人 / 哥哥 / 司令）"
    )

    # 群聊记忆库设置
    memory_enabled: bool = Field(
        default=True,
        description="是否启用群聊专属记忆库（JSON 存储与概括压缩）"
    )
    memory_max_entries: int = Field(
        default=15,
        ge=5,
        le=100,
        description="记忆库触发压缩的条目数阈值"
    )
    memory_history_count: int = Field(
        default=3,
        ge=1,
        le=20,
        description="每次打开bot读取的历史群记忆次数（由大模型在关闭前总结提炼）"
    )

    # 各群独立设置与群聊隔离 (group_id -> GroupCustomConfig)
    group_settings: Dict[str, GroupCustomConfig] = Field(
        default_factory=dict,
        description="各群独立设置与群聊隔离配置（主人名称、独立人设等）"
    )

    def get_group_config(self, group_id: str) -> Optional[GroupCustomConfig]:
        """获取指定群聊的独立配置"""
        if not group_id or not self.group_settings:
            return None
        return self.group_settings.get(str(group_id).strip())

    def get_master_name(self, group_id: str) -> str:
        """获取指定群聊中配置的主人专属称谓（未单独配置则返回空字符串）"""
        grp = self.get_group_config(group_id)
        if grp:
            title = getattr(grp, "master_title", "") or grp.master_name
            if title and title.strip():
                return title.strip()
        return ""

    def get_persona_name(self, group_id: str) -> str:
        """获取指定群聊中配置的独立人设名称"""
        grp = self.get_group_config(group_id)
        if grp and grp.persona:
            return grp.persona.strip()
        return ""

    def get_work_permissions(self, group_id: str) -> WorkPermissions:
        group = self.get_group_config(group_id)
        return group.work_permissions if group and group.work_permissions is not None else self.work_permissions

    def is_master(self, group_id: str, user_id: str, sender_name: str = "") -> bool:
        """稳定用户 ID 判断说话人是否为群主人（包括群专属主人及全局主人；昵称仅用于展示，不可用于鉴权）"""
        clean_uid = str(user_id or "").strip()
        if not clean_uid:
            return False
        from messaging import is_master_user
        grp = self.get_group_config(group_id)
        if grp and grp.master_id and str(grp.master_id).strip():
            if is_master_user(str(grp.master_id), clean_uid):
                return True
        # 群覆盖缺少 ID 或群主人非当前用户时，可回退明确配置的全局主人 ID
        if self.master_id:
            return is_master_user(self.master_id, clean_uid)
        return False

    def is_admin(self, group_id: str, user_id: str, sender_role: str = "") -> bool:
        if self.is_master(group_id, user_id):
            return True
        if str(user_id) in self.admin_ids:
            return True
        group = self.get_group_config(group_id)
        return bool(group_id and (sender_role in ("admin", "owner") or
                    group and str(user_id) in group.admin_ids))


    # 预设提醒频率与Token优化（默认每10次交互提醒一次预设）
    preset_reminder_interval: int = Field(
        default=10,
        ge=1,
        le=100,
        description="旧版兼容项；总预设现改为首次加载与空闲整理时回忆"
    )

    # 网络搜梗与生僻不明言语解析
    search_enabled: bool = Field(
        default=True,
        description="是否启用网络搜索解析网络梗与生僻不明言语，辅助模型更合理用梗回复"
    )

    # 表情包与图片语义过滤
    image_filter_enabled: bool = Field(
        default=True,
        description="是否启用表情包与图片语义过滤（意义不明则不随意回复，理解含义则回复对应语句）"
    )

    mode: str = Field(
        default="official",
        description="运行模式: official / onebot / dual / agent（独立角色与网站账号）"
    )


class GroupCustomConfig(BaseModel):
    """单群独立个性化配置（实现各群主人名称与人设预设隔离）"""
    group_id: str = Field(description="群号或群 OpenID")
    group_name: str = Field(default="", description="群聊备注名称")
    master_name: str = Field(default="", description="开发者（主人）在此群的名称/昵称/名片/专属称谓")
    master_id: str = Field(default="", description="开发者（主人）在此群的 QQ号或 OpenID (可选)")
    master_nickname: str = Field(default="", description="主人的群名片/昵称 (可选)")
    master_title: str = Field(default="", description="主人在此群的专属称谓（如：主人 / 哥哥 / 司令）")
    persona: str = Field(default="", description="此群独立绑定的人设预设名称 (留空使用全局默认人设)")
    admin_ids: list[str] = Field(default_factory=list, description="此群管理员稳定 ID")
    work_permissions: Optional[WorkPermissions] = Field(default=None, description="覆盖此群的后台工作权限；留空继承全局")


class OneBotAccountConfig(BaseModel):
    name: str = Field(default="小号 A", description="账号备注标签")
    account_id: str = Field(default="", description="本地 QQ 号")
    ws_url: str = Field(default="ws://127.0.0.1:3001", description="OneBot WebSocket 地址")
    access_token: str = Field(default="", description="访问令牌 Token (若无则留空)")
    enabled: bool = Field(default=True, description="是否启用")


def default_onebot_accounts() -> Dict[str, OneBotAccountConfig]:
    return {
        "account_1": OneBotAccountConfig(name="小号 A (账号一)", account_id="", ws_url="ws://127.0.0.1:3001"),
        "account_2": OneBotAccountConfig(name="小号 B (账号二)", account_id="", ws_url="ws://127.0.0.1:3002")
    }


class OneBotConfig(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_account(cls, data):
        if isinstance(data, dict) and "accounts" not in data and ("ws_url" in data or "access_token" in data):
            data = dict(data)
            accounts = {k: v.model_dump() for k, v in default_onebot_accounts().items()}
            accounts["account_1"].update(ws_url=data.get("ws_url", "ws://127.0.0.1:3001"),
                                         access_token=data.get("access_token", ""))
            data.update(accounts=accounts, active_account="account_1")
        return data

    enabled: bool = Field(default=False, description="是否启用本地 QQNT 个人小号 (OneBot v11 / NapCat)")
    active_account: str = Field(default="account_1", description="当前激活使用的 QQ 小号 (account_1 或 account_2)")
    accounts: Dict[str, OneBotAccountConfig] = Field(
        default_factory=default_onebot_accounts,
        description="本地两个 QQ 小号的配置"
    )
    ws_url: str = Field(default="ws://127.0.0.1:3001", description="当前激活账号的 WebSocket 地址（自动同步）")
    access_token: str = Field(default="", description="当前激活账号的 Access Token（自动同步）")
    reconnect_interval: int = Field(default=5, ge=1, description="WebSocket 断线重试重连间隔（秒）")
    message_send_timeout_seconds: float = Field(default=30, ge=1, le=600,
        description="QQ 消息发送回调等待秒数；RPC 在此基础上额外保留 5 秒接收回执，超时不自动重发")
    napcat_path: Optional[str] = Field(default=None, description="NapCatQQ 本地安装目录或可执行文件路径（留空自动检测）")

    def model_post_init(self, __context) -> None:
        if self.accounts and self.active_account in self.accounts:
            acc = self.accounts[self.active_account]
            if not self.ws_url or self.ws_url == "ws://127.0.0.1:3001":
                object.__setattr__(self, "ws_url", acc.ws_url)
            if not self.access_token:
                object.__setattr__(self, "access_token", acc.access_token)

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == "active_account":
            accounts = getattr(self, "accounts", None)
            if isinstance(accounts, dict) and value in accounts:
                acc = accounts[value]
                object.__setattr__(self, "ws_url", acc.ws_url)
                object.__setattr__(self, "access_token", acc.access_token)
        elif name in ("ws_url", "access_token"):
            accounts = getattr(self, "accounts", None)
            active = getattr(self, "active_account", "account_1")
            if isinstance(accounts, dict) and active in accounts:
                setattr(accounts[active], name, value)

    def get_active_account(self) -> OneBotAccountConfig:
        """获取当前激活使用的账号配置"""
        if self.accounts and self.active_account in self.accounts:
            return self.accounts[self.active_account]
        if self.accounts:
            first_key = next(iter(self.accounts))
            return self.accounts[first_key]
        default_acc = OneBotAccountConfig(name="小号 A", ws_url=self.ws_url, access_token=self.access_token)
        self.accounts = {"account_1": default_acc}
        return default_acc

    def switch_account(self, account_key: str) -> bool:
        """切换当前激活账号，并同步更新兼容字段 ws_url 与 access_token"""
        if account_key in self.accounts:
            self.active_account = account_key
            acc = self.accounts[account_key]
            self.ws_url = acc.ws_url
            self.access_token = acc.access_token
            return True
        return False


class GeminiWebConfig(BaseModel):
    model: str = Field(default="pro", max_length=200, description="网页端模型别名或账号返回的模型名称；auto 使用账号默认模型")
    auto_browser: bool = Field(default=True, description="自动复用已连接或当前浏览器的 Gemini 登录态")
    browser: Literal["auto", "chrome", "edge", "brave", "firefox"] = "auto"
    browser_profile: str = Field(default="", description="留空使用浏览器最近使用的配置；也可指定配置目录")
    auto_proxy: bool = Field(default=True, description="代理环境变量未设置时自动使用系统代理")
    cookie_file: str = Field(default="data/gemini_web/cookies.json", description="本地 Cookie JSON 文件；也可通过环境变量提供登录态")
    secure_1psid_env: str = "QQBOT_GEMINI_1PSID"
    secure_1psidts_env: str = "QQBOT_GEMINI_1PSIDTS"
    proxy_env: str = "HTTPS_PROXY"
    timeout_seconds: int = Field(default=600, ge=30, le=7200)


class MediaConfig(BaseModel):
    delivery_batch_size: int = Field(default=20, ge=1, le=1000, description="文件直接发送数量阈值，超出自动打包 ZIP，保留全部成果")
    delivery_max_file_mb: int = Field(default=1024, ge=1, le=1024, description="单个交付文件的大小上限 MB（最高 1 GB）")
    enabled: bool = True
    image_enabled: bool = True
    image_generation_enabled: bool = True
    image_generation_provider: Literal["native", "api", "gemini_web"] = "native"
    image_generation_api_url: str = ""
    image_generation_api_key_env: str = "QQBOT_IMAGE_GENERATION_KEY"
    image_generation_model: str = ""
    gemini_web: GeminiWebConfig = Field(default_factory=GeminiWebConfig)
    video_enabled: bool = True
    video_generation_enabled: bool = True
    video_generation_provider: Literal["native", "gemini_web"] = "gemini_web"
    file_enabled: bool = True
    default_image_count: int = Field(default=3, ge=1, le=10)
    max_image_count: int = Field(default=5, ge=1, description="旧配置兼容项，已不再限制交付数量")
    max_image_mb: int = Field(default=10, ge=1, le=50)
    max_video_mb: int = Field(default=50, ge=1, le=500)
    max_file_mb: int = Field(default=1024, ge=1, le=1024)
    default_video_max_seconds: int = Field(default=120, ge=1, le=3600)
    download_timeout_seconds: int = Field(default=60, ge=1, le=600)
    upload_timeout_seconds: int = Field(default=120, ge=1, le=600)
    task_timeout_seconds: int = Field(default=0, ge=0, description="公共下载器总时限；0 表示不限制；反重力自主任务不使用此项")
    max_concurrent_downloads: int = Field(default=2, ge=1, le=8)
    per_conversation_queue_limit: int = Field(default=3, ge=1, description="旧配置兼容项，临时对话不设数量上限")
    max_pending_jobs: int = Field(default=32, ge=1, description="旧配置兼容项，临时对话不设数量上限")
    cache_ttl_hours: int = Field(default=24, ge=1, le=720)
    cache_max_mb: int = Field(default=2048, ge=100, le=102400)
    result_ttl_minutes: int = Field(default=15, ge=1, le=120)
    require_explicit_group_request: bool = True
    local_library_roots: list[str] = Field(default_factory=lambda: ["data/library"])
    # Optional HTTP search provider: GET ?q=...&kind=...&limit=...&offset=...
    search_api_url: str = ""
    search_api_key_env: str = "QQBOT_MEDIA_SEARCH_KEY"
    search_provider: Literal["native", "auto", "wikimedia"] = "native"
    commons_api_url: str = "https://commons.wikimedia.org/w/api.php"
    # Optional existing HTTPS object-store upload endpoint. No automatic public server.
    publish_upload_url: str = ""
    publish_token_env: str = "QQBOT_MEDIA_PUBLISH_TOKEN"
    official_file_enabled: bool = False
    napcat_stream_enabled: bool = True
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    cache_dir: str = "data/cache/media"
    database_path: str = "data/media.sqlite3"


class StickerConfig(BaseModel):
    enabled: bool = True
    directory: str = "data/stickers"
    cooldown_seconds: int = Field(default=60, ge=0, le=3600)
    expire_seconds: int = Field(default=120, ge=10, le=300)


class VirtualMachineConfig(BaseModel):
    qemu_path: str = "tools/qemu/qemu-system-x86_64.exe"
    disk_path: str = "data/sandbox/vm/windows.qcow2"
    install_iso: str = ""
    memory_mb: int = Field(default=4096, ge=1024, le=16384)
    cpus: int = Field(default=2, ge=1, le=8)
    accelerator: Literal["tcg", "whpx"] = "tcg"


class GalgameReadingConfig(BaseModel):
    enabled: bool = True
    batch_lines: int = Field(default=8, ge=1, le=20, description="每次本地阅读最多推进的对白数；遇选项或异常提前停止")
    dialogue_top: float = Field(default=0.62, ge=0.35, le=0.9, description="对白区域上边界，相对客户区高度")
    settle_ms: int = Field(default=600, ge=200, le=2000, description="对白文本稳定等待毫秒，不使用长按或快进")
    visual_interval: int = Field(default=24, ge=1, le=100, description="普通对白最多累计多少次推进后补充一次完整画面")
    scene_threshold: float = Field(default=0.16, ge=0.03, le=0.5, description="对白区域外明显画面变化的本地检测阈值")


class SandboxConfig(BaseModel):
    enabled: bool = True
    directory: str = "data/sandbox"
    mode: Literal["background", "qemu"] = "background"
    startup_timeout_seconds: int = Field(default=60, ge=1, le=300, description="等待实际内容窗口及首张有效截图的最长秒数；不重启或重复输入")
    max_actions: int = Field(default=0, ge=0, description="软件/游戏操作次数上限；0 表示不限次数")
    galgame_reading: GalgameReadingConfig = Field(default_factory=GalgameReadingConfig)
    autonomous_enabled: bool = False
    interval_minutes: int = Field(default=30, ge=1, le=1440)
    # Profiles contain user-supplied files/session data, never plaintext account
    # passwords in prompts. Additional host paths must be explicitly granted.
    profiles: Dict[str, str] = Field(default_factory=dict)
    # Dedicated Chromium user-data directories; cookies stay local across runs.
    browser_profiles: Dict[str, str] = Field(default_factory=dict)
    browser_executable: str = ""  # Empty selects an installed Chrome/Edge automatically.
    vm: VirtualMachineConfig = Field(default_factory=VirtualMachineConfig)


class EmotionConfig(BaseModel):
    provider: Literal["main", "lmstudio", "local_classifier", "bert"] = "main"
    bert_service_file: str = "emotion_studio/runtime/inference.json"
    lms_host: str = Field(default="127.0.0.1:1234", min_length=1)
    llm_model: str = ""
    embedding_model: str = ""
    classifier_path: str = "data/emotion/model.npz"
    dataset_path: str = "data/emotion/dataset.jsonl"
    labels: list[str] = Field(default_factory=lambda: ["中性", "喜悦", "悲伤", "愤怒", "恐惧", "惊讶", "厌恶"],
                              min_length=2, max_length=32)
    training_features: Literal["char_hash", "lmstudio_embedding"] = "char_hash"
    feature_dim: int = Field(default=2048, ge=64, le=65536)
    epochs: int = Field(default=40, ge=1, le=10000)
    batch_size: int = Field(default=16, ge=1, le=1024)
    learning_rate: float = Field(default=0.2, gt=0, le=10, allow_inf_nan=False)
    l2: float = Field(default=0.0001, ge=0, le=1, allow_inf_nan=False)
    validation_fraction: float = Field(default=0.2, ge=0, le=0.5, allow_inf_nan=False)
    seed: int = Field(default=42, ge=0, le=4294967295)

    @model_validator(mode="after")
    def validate_emotion(self):
        self.labels = [label.strip() for label in self.labels]
        if any(not label or len(label) > 40 or "\n" in label or "\r" in label for label in self.labels):
            raise ValueError("情感标签必须为 1–40 字的单行文字。")
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("情感标签不能重复。")
        self.lms_host = self.lms_host.strip()
        if "://" in self.lms_host or "/" in self.lms_host or any(c.isspace() for c in self.lms_host):
            raise ValueError("LM Studio 地址请填写 host:port，例如 127.0.0.1:1234。")
        if not self.lms_host:
            raise ValueError("LM Studio 地址不能为空。")
        return self


class AppConfig(BaseModel):
    agent: AgentConfig = Field(default_factory=AgentConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    antigravity: AntigravityConfig = Field(default_factory=AntigravityConfig)
    bot: BotBehaviorConfig = Field(default_factory=BotBehaviorConfig)
    onebot: OneBotConfig = Field(default_factory=OneBotConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    stickers: StickerConfig = Field(default_factory=StickerConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    emotion: EmotionConfig = Field(default_factory=EmotionConfig)

    @classmethod
    def load(cls, config_path: str = "config.yaml") -> AppConfig:
        """从 yaml 配置文件加载配置，若文件不存在则创建默认模板。"""
        if not os.path.exists(config_path):
            default_config = cls()
            default_config.save(config_path)
            return default_config

        target_path = Path(config_path).resolve()
        backup_path = target_path.with_name(f"{target_path.name}.backup")

        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()

        if not content.strip():
            hint = f"；检测到备份文件: {backup_path}，可尝试恢复" if backup_path.exists() else ""
            raise ValueError(f"配置文件为空或已损坏: {config_path}{hint}")

        try:
            raw_data = yaml.safe_load(content)
        except Exception as e:
            hint = f"；检测到备份文件: {backup_path}，可尝试恢复" if backup_path.exists() else ""
            raise ValueError(f"配置文件解析失败: {config_path} ({e}){hint}") from e

        if not isinstance(raw_data, dict):
            hint = f"；检测到备份文件: {backup_path}，可尝试恢复" if backup_path.exists() else ""
            raise ValueError(f"配置文件格式无效: {config_path}{hint}")

        config = cls(**raw_data)
        # 保持 active_account 与 ws_url / access_token 互相同步 (以当前激活账号配置为准同步到外层兼容字段)
        active = config.onebot.get_active_account()
        if active:
            config.onebot.ws_url = active.ws_url
            config.onebot.access_token = active.access_token
        return config

    def save(self, config_path: str = "config.yaml") -> None:
        """保存配置到 yaml 文件（原子写入，保证原有配置在序列化或磁盘错误时不被破坏）。"""
        # 保存前确保 active_account 与 ws_url 同步
        active = self.onebot.get_active_account()
        if active:
            self.onebot.ws_url = active.ws_url
            self.onebot.access_token = active.access_token

        target_path = Path(config_path).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = target_path.with_name(f"{target_path.name}.backup")
        tmp_path = target_path.with_name(f"{target_path.name}.tmp.{uuid.uuid4().hex[:8]}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    self.model_dump(),
                    f,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False
                )
                f.flush()
                os.fsync(f.fileno())
            if target_path.exists() and target_path.stat().st_size > 0:
                try:
                    shutil.copy2(target_path, backup_path)
                except OSError:
                    pass
            os.replace(tmp_path, target_path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

