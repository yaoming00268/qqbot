"""Validated, secret-free Live2D/OBS settings for AppConfig and standalone runtime."""
from __future__ import annotations

import ipaddress
import hashlib
import json
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from urllib.parse import urlsplit


RESTART_FIELDS = {"host", "port", "credentials_file", "api_token_env", "allow_remote"}


def config_revision(config):
    return hashlib.sha256(json.dumps(config.model_dump(mode="json"), sort_keys=True,
        ensure_ascii=False).encode("utf-8")).hexdigest()


class VTuberConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=18765, ge=1024, le=65535)
    model_path: str = ""
    credentials_file: str = "data/vtuber/credentials.json"
    api_token_env: str = "QQBOT_VTUBER_API_TOKEN"
    obs_password_env: str = "QQBOT_OBS_PASSWORD"
    vts_url: str = "ws://127.0.0.1:8001"
    vts_token_file: str = "data/vtuber/vts-token.json"
    obs_url: str = "ws://127.0.0.1:4455"
    obs_scene: str = Field(default="QQBot Live", min_length=1, max_length=128)
    obs_source: str = Field(default="QQBot Live2D", min_length=1, max_length=128)
    width: int = Field(default=1280, ge=320, le=3840)
    height: int = Field(default=720, ge=240, le=2160)
    allow_remote: bool = False

    @field_validator("api_token_env", "obs_password_env")
    @classmethod
    def env_name(cls, value):
        if not value or not value.replace("_", "a").isalnum() or len(value) > 128:
            raise ValueError("Expected an environment variable name")
        return value

    @field_validator("vts_url", "obs_url")
    @classmethod
    def websocket_url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError("Expected ws:// or wss:// URL without embedded credentials")
        _ = parsed.port
        return value

    @model_validator(mode="after")
    def local_defaults(self):
        def local(host):
            try:
                return ipaddress.ip_address(host).is_loopback
            except ValueError:
                return host.lower() == "localhost"
        if not self.allow_remote:
            if not local(self.host):
                raise ValueError("Remote binding requires allow_remote=true and a TLS reverse proxy")
            for endpoint in (self.vts_url, self.obs_url):
                if not local(urlsplit(endpoint).hostname):
                    raise ValueError("Remote WebSockets require allow_remote=true")
        elif any(urlsplit(endpoint).scheme == "ws" and not local(urlsplit(endpoint).hostname)
                 for endpoint in (self.vts_url, self.obs_url)):
            raise ValueError("Remote control endpoints must use wss://")
        return self
