"""Validated, optional configuration; legacy QQ identities remain separate."""
from __future__ import annotations

from urllib.parse import urlsplit
from pydantic import BaseModel, Field, model_validator


def origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("账号网址必须为不含凭据的 http/https 地址。")
    port = parts.port
    host = parts.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    return parts.scheme + "://" + host + (":" + str(port) if port and port != (443 if parts.scheme == "https" else 80) else "")


class LocatorSpec(BaseModel):
    model_config = {"extra": "forbid"}
    role: str = ""
    name: str = ""
    label: str = ""
    placeholder: str = ""
    test_id: str = ""
    css: str = ""

    @model_validator(mode="after")
    def one_locator(self):
        if sum(bool(value) for value in (self.role, self.label, self.placeholder, self.test_id, self.css)) != 1:
            raise ValueError("定位器必须且只能指定 role、label、placeholder、test_id、css 之一。")
        if self.name and not self.role:
            raise ValueError("name 只与 role 一起使用。")
        return self


class PublishRecipe(BaseModel):
    editor: LocatorSpec
    submit: LocatorSpec
    confirmation: LocatorSpec
    composer_url: str = ""
    result_link: LocatorSpec | None = None


class ActorConfig(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = ""
    persona_id: str = ""
    prompt: str = ""
    enabled: bool = True


class WebAccountConfig(BaseModel):
    actor_id: str
    platform: str = "web"
    home_url: str
    allowed_origins: list[str] = Field(default_factory=list)
    enabled: bool = True
    profile: str = ""
    executable: str = ""
    headless: bool = True
    actions: list[str] = Field(default_factory=lambda: ["read_content", "read_feed", "create_draft"])
    feeds: list[str] = Field(default_factory=list)
    interval_seconds: int = Field(default=1800, ge=60, le=604800)
    identity: LocatorSpec | None = None
    expected_identity: str = ""
    publish: PublishRecipe | None = None
    comment: PublishRecipe | None = None

    @model_validator(mode="after")
    def validate_account(self):
        allowed = {origin(url) for url in (self.allowed_origins or [self.home_url])}
        if origin(self.home_url) not in allowed:
            raise ValueError("home_url 必须属于 allowed_origins。")
        self.allowed_origins = sorted(allowed)
        for url in self.feeds:
            if origin(url) not in allowed:
                raise ValueError("订阅网址不属于该账号的 allowed_origins。")
        known = {"read_content", "read_feed", "create_draft", "publish_post", "reply_comment"}
        if set(self.actions) - known:
            raise ValueError("不支持的账号动作：" + ", ".join(sorted(set(self.actions) - known)))
        for action, recipe in (("publish_post", self.publish), ("reply_comment", self.comment)):
            if action in self.actions and (not recipe or not self.identity or not self.expected_identity):
                raise ValueError(action + " 需要编辑器、提交、确认和账号身份定位配置。")
            if recipe and recipe.composer_url and origin(recipe.composer_url) not in allowed:
                raise ValueError("发布页面不属于该账号的 allowed_origins。")
        return self


class AgentConfig(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: bool = True
    database_path: str = ""  # Empty: beside bot.runtime_database_path.
    actors: dict[str, ActorConfig] = Field(default_factory=dict)
    bindings: dict[str, str] = Field(default_factory=dict)
    accounts: dict[str, WebAccountConfig] = Field(default_factory=dict)
    max_concurrent_activities: int = Field(default=2, ge=1, le=16)
    action_timeout_ms: int = Field(default=10000, ge=1000, le=60000)
    max_content_chars: int = Field(default=16000, ge=1000, le=64000)
    autonomous_model_enabled: bool = True
    reflection_timeout_seconds: int = Field(default=180, ge=10, le=1800)

    @model_validator(mode="after")
    def references(self):
        profiles = [account.profile.casefold().replace("\\", "/").rstrip("/") for account in self.accounts.values() if account.profile]
        if len(profiles) != len(set(profiles)):
            raise ValueError("不同网站账号需要独立的浏览器 profile。")
        for actor_id in [*self.bindings.values(), *(account.actor_id for account in self.accounts.values())]:
            if actor_id not in self.actors:
                raise ValueError("账号或绑定引用了不存在的角色：" + actor_id)
        for actor_id in self.actors:
            if not actor_id or len(actor_id) > 120 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in actor_id):
                raise ValueError("角色 ID 使用 1–120 位字母、数字、下划线或连字符。")
        return self
