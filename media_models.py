"""Validated media intents, scoped resources and transport reply context."""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Callable, Literal
import uuid

from pydantic import BaseModel, Field

from media_downloader import MediaError


class ReplyWindowExpired(MediaError):
    """A prepared asset can be collected with a new inbound message."""


class MediaIntent(BaseModel):
    operation: Literal["search", "search_send", "send_url", "pick", "next", "export", "quote", "library", "group_files", "status", "cancel", "help"]
    kind: Literal["image", "video", "file"] = "image"
    query: str = Field(default="", max_length=1000)
    request_text: str = ""
    url: str = Field(default="", max_length=8192)
    count: int = Field(default=1, ge=1)
    count_requested: bool = False
    index: int = Field(default=1, ge=1, le=100)
    result_id: str = Field(default="", max_length=100)
    job_id: str = Field(default="", max_length=100)
    extension: Literal["txt", "csv", "md"] = "txt"
    orientation: str = ""
    aspect_ratio: str = ""
    max_duration: int = Field(default=0, ge=0)
    as_file: bool = False
    send_requested: bool = False
    latest: bool = False
    silent_summary: bool = False
    defer_errors: bool = False


class MediaAsset(BaseModel):
    asset_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    kind: Literal["image", "video", "file"]
    title: str = ""
    url: str = ""
    source_url: str = ""
    provider: str = ""
    thumbnail: str = ""
    local_path: str = ""
    mime: str = ""
    size: int = 0
    sha256: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0
    license: str = ""
    author: str = ""
    public_url: str = ""
    metadata: dict = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)


@dataclass
class MessageEnvelope:
    transport: str
    account_id: str
    conversation_id: str
    sender_id: str
    message_id: str
    text: str
    send_text: Callable
    is_group: bool = False
    api: Any = None
    send_action: Any = None
    received_at: float = field(default_factory=time.time)
    max_text_length: int = 1200
    quoted_images: list[dict] = field(default_factory=list)
    quoted_message_id: str = ""
    last_reply: str = ""
    guard: Callable = field(default=lambda: True)
    sequence: int = 0
    cache_sent: Callable | None = None
    ai_client: Any = None
    ai_session_key: str = ""
    wait_transport: Any = None
    reply_supersedable: bool = True
    reply_sources: list = field(default_factory=list)
    reply_label: str = ""
    reply_quote: bool | None = None
    reply_targets: dict = field(default_factory=dict)
    console_token: str = ""
    cli_messages: list = field(default_factory=list)
    cli_sent_texts: list = field(default_factory=list)
    cli_streamed: bool = False
    proactive_event: bool = False
    sender_role: str = ""
    mentioned_user_ids: list[str] = field(default_factory=list)
    media_persona: str | None = None

    def __post_init__(self):
        # A worker replaces ai_session_key with its own task conversation.
        # Storage ownership belongs to the originating persona for the entire
        # request, including copies, delivery receipts and error handling.
        if self.media_persona is None:
            from persona_isolation import context_persona
            self.media_persona = context_persona({"session_key": self.ai_session_key})

    @property
    def scope(self):
        return f"{self.transport}:{self.account_id}:{'group' if self.is_group else 'private'}:{self.conversation_id}"

    @property
    def owner(self):
        return f"{self.scope}:{self.sender_id}"

    @property
    def media_scope(self):
        return self.scope + "::persona:" + self.media_persona if self.media_persona else self.scope

    @property
    def media_owner(self):
        return f"{self.media_scope}:{self.sender_id}"

    def check_current(self):
        if not self.guard():
            raise MediaError("会话已重置，本次任务已经取消。")

    def check_reply_available(self):
        self.check_current()
        if self.transport == "official":
            window = 300 if self.is_group else 3600
            if time.time() - self.received_at >= window:
                raise ReplyWindowExpired("本次回复窗口已结束，请发送“任务进度”领取结果。")
            if self.sequence >= 5:
                raise ReplyWindowExpired("本轮回复次数已用完，请继续发送“任务进度”领取剩余结果。")

    def allocate_sequence(self):
        self.check_reply_available()
        self.sequence += 1
        return self.sequence
