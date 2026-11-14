"""Resolve a QQ image token through OneBot without searching local disks."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import os
from pathlib import Path
import re
import tempfile
import urllib.parse

from media_downloader import MediaError, image_metadata, public_url

_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp", "BMP": ".bmp"}


def _http(value):
    value = str(value or "").strip()
    if not value.lower().startswith(("http://", "https://")):
        return ""
    try:
        return public_url(value)
    except MediaError:
        return ""


def _absolute_local(value):
    value = str(value or "").strip()
    if value.lower().startswith("file://"):
        uri = urllib.parse.urlsplit(value)
        if uri.netloc:
            return None
        value = urllib.parse.unquote(uri.path)
        if os.name == "nt" and re.match(r"^/[a-zA-Z]:/", value):
            value = value[1:]
    else:
        value = urllib.parse.unquote(value)
    # An absolute local path is distinct from a remote UNC share.
    if value.startswith(("\\\\", "//")):
        return None
    try:
        path = Path(value)
        return path if path.is_absolute() else None
    except (OSError, ValueError):
        return None


def _image_items(message):
    """Preserve image/mface order, preferring canonical structured segments."""
    items = []
    segments = message.get("message")
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, dict) or segment.get("type") not in ("image", "mface", "bface"):
                continue
            data = segment.get("data")
            if isinstance(data, dict):
                item = dict(data)
                if segment["type"] in ("mface", "bface"):
                    item.update(type="mface", subType="1")
                items.append(item)
        if items:
            return items
    raw = message.get("raw_message") or segments or ""
    if isinstance(raw, str):
        for match in re.finditer(r"\[CQ:(image|mface|bface),([^\]]*)\]", raw):
            data = {key: html.unescape(value) for part in match[2].split(",") if "=" in part
                    for key, value in [part.split("=", 1)]}
            if match[1] in ("mface", "bface"):
                data.update(type="mface", subType="1")
            items.append(data)
    return items


class QQImageSourceResolver:
    def __init__(self, send_action, event, cache_dir="data/cache/images"):
        self.send_action = send_action
        self.event = event
        self.cache_dir = Path(cache_dir).resolve()

    def _event(self, name, default=""):
        return self.event.get(name, default) if isinstance(self.event, dict) else getattr(self.event, name, default)

    def _cache_existing(self, item):
        for key in ("file", "local_path"):
            path = _absolute_local(item.get(key))
            if path is None:
                continue
            try:
                path = path.resolve(strict=True)
                if not path.is_relative_to(self.cache_dir) or not path.is_file():
                    continue
                if not 0 < path.stat().st_size <= _MAX_IMAGE_BYTES:
                    continue
                image_metadata(path)
                return str(path)
            except (OSError, RuntimeError, ValueError):
                continue
        return ""

    def _import(self, *, path=None, payload=None):
        """Copy only an exact API-provided path or payload, then verify the copy."""
        if path is not None:
            path = path.resolve(strict=True)
            if not path.is_file() or not 0 < path.stat().st_size <= _MAX_IMAGE_BYTES:
                raise MediaError("OneBot 返回的图片文件为空或超过 20MB。")
            image_metadata(path)
        if payload is not None and not 0 < len(payload) <= _MAX_IMAGE_BYTES:
            raise MediaError("OneBot 返回的内联图片为空或超过 20MB。")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="qq-source-", suffix=".part", dir=self.cache_dir)
        temporary = Path(name)
        digest, size = hashlib.sha256(), 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                if payload is not None:
                    output.write(payload)
                    digest.update(payload)
                else:
                    with path.open("rb") as source:
                        while chunk := source.read(1024 * 1024):
                            size += len(chunk)
                            if size > _MAX_IMAGE_BYTES:
                                raise MediaError("OneBot 图片在读取期间超过 20MB。")
                            output.write(chunk)
                            digest.update(chunk)
            metadata = image_metadata(temporary)
            target = self.cache_dir / ("qq-" + digest.hexdigest() + _EXTENSIONS[metadata["format"]])
            os.replace(temporary, target)
            return str(target)
        finally:
            temporary.unlink(missing_ok=True)

    def _api_source(self, data, item):
        """API fields may carry local paths, public HTTP URLs or raw base64."""
        source_url = _http(data.get("url")) or _http(data.get("file"))
        values = [data.get(key) for key in ("file", "url", "base64") if data.get(key)]
        for raw in values:
            value = str(raw).strip()
            path = _absolute_local(value)
            if path is not None:
                try:
                    local = self._import(path=path)
                    return {**item, "file": local, "local_path": local, "url": source_url,
                            "source_url": source_url or _http(item.get("url")) or _http(item.get("file")),
                            "source_resolution": "get_image_local"}
                except (OSError, RuntimeError, ValueError):
                    pass
            if _http(value):
                continue
            encoded = value[9:] if value.startswith("base64://") else value
            if encoded.startswith("data:image/") and ";base64," in encoded:
                encoded = encoded.split(";base64,", 1)[1]
            if len(encoded) > 4 * ((_MAX_IMAGE_BYTES + 2) // 3) + 1024:
                continue
            encoded = re.sub(r"\s+", "", encoded)
            if len(encoded) < 16 or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", encoded):
                continue
            try:
                payload = base64.b64decode(encoded, validate=True)
                local = self._import(payload=payload)
                return {**item, "file": local, "local_path": local, "url": source_url,
                        "source_url": source_url or _http(item.get("url")) or _http(item.get("file")),
                        "source_resolution": "get_image_base64"}
            except (OSError, RuntimeError, ValueError):
                continue
        if source_url:
            return {**item, "file": "", "local_path": "", "url": source_url,
                    "source_url": source_url, "source_resolution": "get_image_url"}
        return None

    async def _get_image(self, item):
        token = str(item.get("original_file") or item.get("file") or "").strip()
        if not token:
            return None
        try:
            response = await self.send_action("get_image", {"file": token}, timeout=8)
            if (not isinstance(response, dict) or response.get("status") == "failed"
                    or response.get("retcode", 0) != 0 or not isinstance(response.get("data"), dict)):
                return None
            return await asyncio.to_thread(self._api_source, response["data"], item)
        except Exception:
            return None

    async def _refreshed_item(self, original, index):
        message_id = str(self._event("message_id") or "")
        if not message_id:
            return None
        try:
            response = await self.send_action("get_msg", {"message_id": int(message_id) if message_id.lstrip("-").isdigit() else message_id}, timeout=8)
            if (not isinstance(response, dict) or response.get("status") == "failed"
                    or response.get("retcode", 0) != 0 or not isinstance(response.get("data"), dict)):
                return None
            message = response["data"]
            if message.get("message_id") is not None and str(message["message_id"]) != message_id:
                return None
            group = str(self._event("group_id") or "")
            if group:
                if str(message.get("group_id") or "") != group or message.get("message_type") == "private":
                    return None
            else:
                peer = str(self._event("user_id") or "")
                sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
                if (not peer or message.get("group_id") or message.get("message_type") == "group"
                        or str(message.get("user_id") or sender.get("user_id") or "") != peer):
                    return None
            items = _image_items(message)
            tokens = {str(original.get(key) or "") for key in ("file", "url", "original_file")}
            tokens.discard("")
            matched = next((item for item in items if tokens.intersection(
                str(item.get(key) or "") for key in ("file", "url"))), None)
            candidate = matched if matched is not None else items[index] if index < len(items) else None
            if candidate is not None:
                orig_is_mface = bool(original.get("type") in ("mface", "bface") or str(original.get("subType")) == "1")
                cand_is_mface = bool(candidate.get("type") in ("mface", "bface") or str(candidate.get("subType")) == "1")
                if orig_is_mface != cand_is_mface:
                    candidate = None
            return {**original, **candidate} if candidate is not None else None
        except Exception:
            return None

    async def resolve(self, item, index=0, refresh=False):
        if not isinstance(item, dict) or not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise MediaError("图片来源或图片序号无效。")
        original = dict(item)
        cached = await asyncio.to_thread(self._cache_existing, original)
        if cached and not refresh:
            return {**original, "file": cached, "local_path": cached,
                    "source_url": _http(original.get("source_url")) or _http(original.get("url"))}
        initial = await self._get_image(original)
        if initial is not None and not refresh:
            return initial
        fresh = await self._refreshed_item(original, index)
        if fresh is not None:
            # The new token must take precedence over a resolver's earlier token.
            fresh.pop("original_file", None)
            resolved = await self._get_image(fresh)
            if resolved is not None:
                return resolved
            fresh_url = _http(fresh.get("url")) or _http(fresh.get("file"))
            if fresh_url:
                return {**fresh, "file": "", "local_path": "", "url": fresh_url,
                        "source_url": fresh_url, "source_resolution": "get_msg_url"}
        if initial is not None:
            return initial
        if cached:
            return {**original, "file": cached, "local_path": cached,
                    "source_url": _http(original.get("source_url")) or _http(original.get("url"))}
        # Do not copy a user-supplied path outside the cache. A live public URL
        # remains available to the caller's normal validated HTTP downloader.
        url = _http(original.get("url")) or _http(original.get("file"))
        return {**original, "file": "", "local_path": "", "url": url, "source_url": url}
