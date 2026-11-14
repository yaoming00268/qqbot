"""Small, lazily loaded adapter for the authenticated Gemini website client."""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import getproxies

from media_downloader import MediaError
from gemini_browser_auth import BrowserLogin, browser_login


logger = logging.getLogger("GeminiWeb")
_STAGES = {"connect": "登录连接", "inputs": "附件读取", "generation": "生成请求",
           "download": "图片下载", "validation": "图片校验", "delivery": "结果归档", "request": "请求"}


class GeminiWebError(MediaError):
    """A public message with credential-free diagnostics for the task record."""

    def __init__(self, message, diagnostic):
        super().__init__(message)
        self.diagnostic = diagnostic


def error_diagnostics(error, stage="request"):
    """Keep typed error information, never third-party messages or request URLs."""
    result = {"stage": stage if stage in _STAGES else "request", "error_types": []}
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        name = type(error).__name__
        result["error_types"].append(name if name.isidentifier() and len(name) <= 80 else "Exception")
        code = getattr(error, "code", None)
        if type(code) is int and 0 < code < 100:
            result.setdefault("curl_code", code)
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        if type(status) is int and 100 <= status <= 599:
            result.setdefault("http_status", status)
        error = error.__cause__ or error.__context__
    return result


def resolve_login(config):
    psid = os.environ.get(config.secure_1psid_env, "").strip() if config.secure_1psid_env else ""
    psidts = os.environ.get(config.secure_1psidts_env, "").strip() if config.secure_1psidts_env else ""
    source = "环境变量"
    if not psid and config.cookie_file:
        path = Path(config.cookie_file).expanduser()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict) and "cookies" in data:
                    data = data["cookies"]
                if isinstance(data, list):
                    data = {row["name"]: row.get("value", "") for row in data
                            if isinstance(row, dict) and row.get("domain", "").lstrip(".") in
                            ("google.com", "gemini.google.com") and row.get("name") in
                            ("__Secure-1PSID", "__Secure-1PSIDTS")}
                if not isinstance(data, dict):
                    raise ValueError()
                psid = data.get("__Secure-1PSID", "")
                psidts = psidts or data.get("__Secure-1PSIDTS", "")
                source = "手动 Cookie 文件"
            except (OSError, ValueError, TypeError):
                raise MediaError("网页端 Gemini Cookie 文件无效；请使用 Cookie 名称到值的 JSON 对象或导出的 Cookie 数组。") from None
    if not isinstance(psid, str) or not isinstance(psidts, str):
        raise MediaError("网页端 Gemini Cookie 格式无效，请检查手动配置或使用“连接当前浏览器”。")
    if psid.strip():
        return BrowserLogin(psid.strip(), psidts.strip(), source)
    return browser_login(config)


def cookie_values(config):
    login = resolve_login(config)
    return login.psid, login.psidts


def proxy_settings(config):
    proxy = os.environ.get(config.proxy_env, "").strip() if config.proxy_env else ""
    if proxy:
        return proxy, "代理环境变量"
    if config.auto_proxy:
        proxies = getproxies()
        if proxy := proxies.get("https") or proxies.get("http") or proxies.get("all"):
            return proxy, "系统代理"
    return None, "直连"


def public_error(error, *, stage="request", media_kind="image"):
    """Never persist third-party error strings containing URLs or credentials."""
    if isinstance(error, MediaError):
        return str(error)
    diagnostic = error_diagnostics(error, stage)
    names = set(diagnostic["error_types"])
    code, status = diagnostic.get("curl_code"), diagnostic.get("http_status")
    media_label = "视频" if media_kind == "video" else "图片"
    label = _STAGES.get(stage, _STAGES["request"]).replace("图片", media_label)
    if "AuthError" in names:
        return "网页端 Gemini 登录失效；请在已连接的浏览器打开 gemini.google.com 登录，再检查连接。"
    if "UsageLimitExceededError" in names:
        return "网页端 Gemini 当前账号或模型配额已用尽，请稍后再试或选择其他可用模型。"
    if "ModelInvalidError" in names:
        return "网页端 Gemini 不支持所选模型，请在媒体页刷新账号的可用模型。"
    if "TemporarilyBlockedError" in names:
        return "网页端 Gemini 暂时限制了当前请求，请稍后手动重试。"
    if names & {"TimeoutError", "Timeout", "ReadTimeout"} or code == 28:
        if stage == "download":
            return f"网页端 Gemini {media_label}已生成，但下载超时；请检查代理能否访问 Google {media_label}服务。"
        suffix = "；本次结果尚未确认，请勿连续重复提交。" if stage in ("generation", "request") else "，请检查代理与网络。"
        return f"网页端 Gemini {label}超时" + suffix
    if status and status >= 400:
        return f"网页端 Gemini {label}失败（HTTP {status}），请稍后再试。"
    if code or names & {"ConnectionError", "ConnectError", "ProxyError", "DNSError", "SSLError"}:
        if stage == "download":
            return f"网页端 Gemini {media_label}已生成，但下载连接失败；请检查代理能否访问 Google {media_label}服务。"
        return f"网页端 Gemini {label}连接失败，请检查代理与网络。"
    if stage == "download":
        return f"网页端 Gemini {media_label}已生成，但保存失败；请检查缓存目录权限、磁盘空间及任务诊断信息。"
    if stage == "inputs":
        return "网页端 Gemini 参考附件读取失败，请检查文件是否可读。"
    if stage in ("validation", "delivery"):
        return f"网页端 Gemini 结果已获取，但本地{label}失败，请查看任务诊断信息。"
    if names & {"APIError", "GeminiError", "JSONDecodeError", "ValidationError"}:
        return "网页端 Gemini 生成响应中断或无法解析；本次结果尚未确认，请勿连续重复提交。"
    return f"网页端 Gemini {label}未完成，请查看任务诊断信息。"


def task_error(error, stage, *, media_kind="image"):
    if isinstance(error, MediaError):
        return error
    return GeminiWebError(public_error(error, stage=stage, media_kind=media_kind), error_diagnostics(error, stage))


@asynccontextmanager
async def open_gemini_web(config, *, status=None):
    login = await asyncio.to_thread(resolve_login, config)
    try:
        from gemini_webapi import GeminiClient
        from gemini_webapi.constants import AccountStatus
    except ImportError:
        raise MediaError("缺少网页端 Gemini 依赖；请使用机器人 Python 环境安装 requirements.txt（需要 Python 3.11+）。") from None
    proxy, proxy_source = proxy_settings(config)
    if status is not None:
        status.update(source=login.source, proxy=proxy_source)
    client = GeminiClient(login.psid, login.psidts or None, proxy=proxy)
    try:
        try:
            await client.init(timeout=config.timeout_seconds, auto_close=False, auto_refresh=True, verbose=False)
            if client.account_status == AccountStatus.UNAUTHENTICATED:
                raise MediaError("网页端 Gemini 登录失效；请在已连接的浏览器打开 gemini.google.com 登录，再检查连接。")
            if client.account_status != AccountStatus.AVAILABLE:
                raise MediaError("网页端 Gemini 当前账号不可用，请检查网页上的账号提示。")
        except Exception as error:
            raise task_error(error, "connect") from None
        yield client
    finally:
        try:
            await client.close()
        except Exception:
            pass


class _MediaDownloadSession:
    """Follow Google CDN redirects explicitly, preserving the configured proxy.

    curl_cffi's SAFE redirect mode can reject the loopback proxy during a CDN
    redirect (curl 7 after HTTP 302). Validate each target here instead of asking
    libcurl to classify redirected connections through the local proxy.
    """

    def __init__(self, session, media_kind="image"):
        self.session = session
        self.media_label = "视频" if media_kind == "video" else "图片"

    async def get(self, url, **kwargs):
        for _ in range(6):
            try:
                url = url.strip()
                parsed = urlsplit(url)
                host = (parsed.hostname or "").lower()
                domains = ("googleusercontent.com", "usercontent.google.com")
                trusted = any(host == domain or host.endswith("." + domain) for domain in domains)
                if (parsed.scheme != "https" or not trusted or parsed.username or parsed.password or
                        parsed.port not in (None, 443) or any(char in url for char in ("\r", "\n", "\\"))):
                    raise ValueError()
            except (AttributeError, TypeError, ValueError):
                raise MediaError(f"网页端 Gemini 返回了异常{self.media_label}下载地址。") from None
            kwargs["allow_redirects"] = False
            response = await self.session.get(url, **kwargs)
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("location")
            if not location:
                raise MediaError(f"网页端 Gemini {self.media_label}下载跳转缺少目标地址。")
            url = urljoin(url, location)
        raise MediaError(f"网页端 Gemini {self.media_label}下载跳转次数过多，请稍后再试。")


_ImageDownloadSession = _MediaDownloadSession


async def save_generated_image(image, directory, filename, *, client, timeout_seconds):
    return await _save_generated_media(image, directory, filename, client=client,
        timeout_seconds=timeout_seconds, media_kind="image", full_size=True)


async def save_generated_video(video, directory, filename, *, client, timeout_seconds):
    result = await _save_generated_media(video, directory, filename, client=client,
        timeout_seconds=timeout_seconds, media_kind="video")
    if not isinstance(result, dict) or not isinstance(result.get("video"), (str, Path)):
        raise MediaError("网页端 Gemini 尚未返回可保存的视频文件，任务未交付。")
    return result["video"]


async def _save_generated_media(media, directory, filename, *, client, timeout_seconds, media_kind, **save_options):
    """Follow validated CDN redirects and reconnect once for transient failures.

    The library mutates image.url while resolving full-size images. Each attempt
    starts from the original descriptor; generation is never part of this retry.
    The deadline also bounds the SDK's polling of videos still being generated.
    """
    from curl_cffi import CurlHttpVersion
    from curl_cffi.requests import AsyncSession
    from gemini_webapi.constants import BROWSER_TYPE

    try:
        async with asyncio.timeout(timeout_seconds):
            try:
                live_session = getattr(client, "client", None) or media.client
                request = {"client": _MediaDownloadSession(live_session, media_kind)} if live_session else {}
                return await media.model_copy().save(path=str(directory), filename=filename,
                    verbose=False, **save_options, **request)
            except Exception as error:
                diagnostic = error_diagnostics(error, "download")
                status = diagnostic.get("http_status", 0)
                if status >= 400:
                    retryable = status in {408, 429, 500, 502, 503, 504}
                else:
                    retryable = diagnostic.get("curl_code") in {5, 6, 7, 16, 18, 28, 35, 52, 55, 56, 81, 92} or bool(
                        set(diagnostic["error_types"]) & {"ConnectionError", "ConnectError", "ProxyError", "TimeoutError", "Timeout", "ReadTimeout"})
                if not retryable:
                    raise
                logger.warning("Gemini %s download interrupted; retrying the same output: %s", media_kind, diagnostic)
            async with AsyncSession(proxy=getattr(client, "proxy", media.proxy), cookies=getattr(client, "cookies", None),
                    impersonate=getattr(client, "impersonate", BROWSER_TYPE), timeout=timeout_seconds,
                    allow_redirects=False, http_version=CurlHttpVersion.NONE) as download_client:
                return await media.model_copy().save(path=str(directory), filename=filename,
                    client=_MediaDownloadSession(download_client, media_kind), verbose=False, **save_options)
    except Exception as error:
        raise task_error(error, "download", media_kind=media_kind) from None


def select_model(client, name):
    name = str(name or "auto").strip()
    if name.lower() == "auto":
        return None
    try:
        model = client.resolve_model(name)
    except ValueError:
        raise MediaError("网页端 Gemini 没有该模型，请在媒体页刷新账号的可用模型。") from None
    if not model.is_available:
        raise MediaError("网页端 Gemini 当前账号无法使用所选模型，请选择其他可用模型。")
    return model


async def available_models(config):
    return (await inspect_account(config))["models"]


async def inspect_account(config):
    status = {}
    async with open_gemini_web(config, status=status) as client:
        return {**status, "models": [{"name": model.model_name, "display_name": model.display_name,
                 "available": bool(model.is_available)} for model in (client.list_models() or [])]}
