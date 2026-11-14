"""Bounded public HTTP access, verified files, and cache path boundaries."""
from __future__ import annotations

import asyncio
import codecs
import hashlib
import ipaddress

import json
import os
from pathlib import Path
import socket
import time
import urllib.parse
import uuid

import aiohttp
from aiohttp.abc import AbstractResolver
from PIL import Image


class MediaError(ValueError):
    """A short error safe to display in a chat."""


def public_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(url))
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        if any(c in url for c in ("\r", "\n", "\\")) or len(url) > 8192:
            raise ValueError()
        host = parsed.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "%" in host:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        _ = parsed.port
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    except (ValueError, TypeError, AttributeError):
        raise MediaError("资源地址必须是可访问的公网 HTTP(S) 地址。") from None


class PublicResolver(AbstractResolver):
    """Validate and pin DNS results used by the actual connection, including redirects."""
    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await asyncio.get_running_loop().getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        resolved = []
        for af, _, proto, _, sockaddr in records:
            address = ipaddress.ip_address(sockaddr[0])
            if not address.is_global or (getattr(address, "ipv4_mapped", None) and not address.ipv4_mapped.is_global):
                raise OSError("Private address rejected")
            resolved.append({"hostname": host, "host": str(address), "port": port,
                             "family": af, "proto": proto, "flags": socket.AI_NUMERICHOST})
        if not resolved:
            raise OSError("No public addresses")
        return resolved

    async def close(self):
        pass


async def http_request(url: str, *, max_bytes=2 * 1024 * 1024, timeout=20,
                       target: Path | None = None, headers=None, method="GET", data=None,
                       stop_at_media=False):
    current = public_url(url)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    default_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    request_headers = {
        "User-Agent": default_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        **(headers or {})
    }
    temporary = None
    try:
        async with asyncio.timeout(timeout):
            async with aiohttp.ClientSession(connector=connector, trust_env=False,
                    timeout=aiohttp.ClientTimeout(total=timeout), auto_decompress=True) as session:
                for redirect in range(6):
                    async with session.request(method, current, headers=request_headers, data=data,
                                               allow_redirects=False) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            # Uploads must not forward credentials or bodies to another endpoint.
                            if method != "GET" or redirect == 5 or not response.headers.get("Location"):
                                raise MediaError("资源重定向异常。")
                            redirected = public_url(urllib.parse.urljoin(current, response.headers["Location"]))
                            orig_parts = urllib.parse.urlsplit(current)
                            new_parts = urllib.parse.urlsplit(redirected)
                            orig_port = orig_parts.port or (443 if orig_parts.scheme == "https" else 80)
                            new_port = new_parts.port or (443 if new_parts.scheme == "https" else 80)
                            if (orig_parts.scheme, orig_parts.hostname, orig_port) != (new_parts.scheme, new_parts.hostname, new_port):
                                request_headers.pop("Authorization", None)
                                request_headers.pop("Cookie", None)
                            current = redirected
                            continue

                        if response.status != 200:
                            raise MediaError(f"资源服务器返回 HTTP {response.status}。")
                        mime = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                        # A page crawl can land on a large original/download. Close
                        # the response here; the bounded file downloader handles it.
                        if stop_at_media and target is None and (
                                mime.startswith(("image/", "video/")) or mime in (
                                    "application/pdf", "application/zip", "application/octet-stream",
                                    "application/ogg", "application/msword") or
                                mime.startswith("application/vnd.openxmlformats-officedocument.")):
                            return b"", mime, current
                        if response.content_length is not None and response.content_length > max_bytes:
                            raise MediaError("资源超过允许的大小。")
                        total = 0
                        parts = []
                        if target is not None:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".part")
                        with temporary.open("wb") if temporary else _NullWriter() as writer:
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                total += len(chunk)
                                if total > max_bytes:
                                    raise MediaError("资源超过允许的大小。")
                                if temporary:
                                    writer.write(chunk)
                                else:
                                    parts.append(chunk)
                        if not total:
                            raise MediaError("资源为空。")
                        if target is not None:
                            os.replace(temporary, target)
                            temporary = None
                        return b"".join(parts), mime, current
        raise MediaError("资源重定向次数过多。")
    except (asyncio.TimeoutError, TimeoutError):
        raise MediaError("下载超时，请稍后重试或缩小文件。") from None
    except (aiohttp.ClientError, OSError):
        raise MediaError("无法连接资源服务器，请稍后重试。") from None
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


class _NullWriter:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


async def get_json(url, *, timeout=20, headers=None, max_bytes=4 * 1024 * 1024):
    payload, _, _ = await http_request(url, timeout=timeout, headers=headers, max_bytes=max_bytes)
    try:
        return json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise MediaError("搜索服务返回了无效数据。") from None


def contained_path(path, roots) -> Path:
    candidate = Path(path).expanduser().resolve()
    if not any(candidate.is_relative_to(Path(root).expanduser().resolve()) for root in roots):
        raise MediaError("该文件不属于已登记资料库或媒体缓存。")
    if not candidate.is_file():
        raise MediaError("文件不存在或已经过期。")
    return candidate


def image_metadata(path: Path) -> dict:
    try:
        with Image.open(path) as img:
            if img.width * img.height > 40_000_000 or getattr(img, "n_frames", 1) > 500:
                raise MediaError("图片像素或帧数过大。")
            meta = {"width": img.width, "height": img.height, "format": img.format, "mode": img.mode,
                    "animated": getattr(img, "is_animated", False)}
            img.verify()
        if meta["format"] not in ("PNG", "JPEG", "GIF", "WEBP", "BMP", "AVIF", "TIFF"):
            raise MediaError("不支持这种图片格式。")
        # verify() alone does not decode JPEG scan data and can accept a
        # truncated download. Decode each frame before making it available.
        with Image.open(path) as img:
            for index in range(getattr(img, "n_frames", 1)):
                img.seek(index)
                img.load()
        return meta
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise MediaError("图片损坏或格式不受支持。") from None


def file_signature(path: Path, kind: str) -> str:
    if kind == "image":
        return {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp", "BMP": "image/bmp", "AVIF": "image/avif", "TIFF": "image/tiff"}[image_metadata(path)["format"]]
    with path.open("rb") as stream:
        head = stream.read(512)
    suffix = path.suffix.lower()
    if suffix == ".pdf" and head.startswith(b"%PDF-"):
        return "application/pdf"
    if suffix in (".zip", ".docx", ".xlsx", ".pptx") and head.startswith(b"PK\x03\x04"):
        return "application/zip"
    if suffix in (".txt", ".csv", ".md", ".json", ".log"):
        if b"\0" in head:
            raise MediaError("文件内容与所请求的类型不符，或格式尚不支持。")
        try:
            decoder = codecs.getincrementaldecoder("utf-8-sig")(errors="strict")
            with path.open("rb") as stream:
                while chunk := stream.read(65536):
                    decoder.decode(chunk, final=False)
                decoder.decode(b"", final=True)
        except UnicodeError:
            raise MediaError("文本文件不是有效的 UTF-8 编码。") from None
        return "text/plain"

    if kind == "video":
        # ffprobe provides the final format, stream, and duration validation.
        if b"ftyp" in head[:32] or head.startswith((b"\x1aE\xdf\xa3", b"OggS", b"RIFF")):
            return "video/mp4" if b"ftyp" in head[:32] else "video/webm"
    raise MediaError("文件内容与所请求的类型不符，或格式尚不支持。")


async def download_url(url, target: Path, *, max_bytes, timeout, headers=None, expected_kind=None):
    # Publish only a complete, validated file. A failed retry must never delete
    # or overwrite a previously completed original at the requested path.
    target = Path(target)
    staging = target.with_name(target.stem + "." + uuid.uuid4().hex + ".download" + target.suffix)
    try:
        _, mime, final_url = await http_request(url, target=staging, max_bytes=max_bytes, timeout=timeout,
                                              headers=headers)
        if expected_kind:
            if expected_kind == "image" and mime in ("text/html", "application/xhtml+xml", "application/json"):
                raise MediaError("图片地址返回了网页或错误数据，请检查来源页、Referer 或登录状态。")
            await asyncio.to_thread(file_signature, staging, expected_kind)
        os.replace(staging, target)
        return mime, final_url
    finally:
        staging.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_cache(directory, *, max_bytes, ttl_seconds, protected=()):
    root = Path(directory).resolve()
    if not root.exists():
        return
    def is_protected(path):
        current = protected() if callable(protected) else protected
        resolved_str = str(path.resolve())
        return any(resolved_str == str(Path(p).resolve()) or resolved_str.startswith(str(Path(p).resolve()) + os.sep) for p in current if p)
    files = sorted((p for p in root.rglob("*") if p.is_file() and not p.is_symlink()), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    now = time.time()
    for path in files:
        if is_protected(path) or path.suffix == ".part":
            continue
        if now - path.stat().st_mtime > ttl_seconds or total > max_bytes:
            size = path.stat().st_size
            try:
                path.unlink()
                total -= size
                parent = path.parent
                while parent != root:
                    try:
                        parent.rmdir()
                        parent = parent.parent
                    except OSError:
                        break
            except OSError:
                pass

