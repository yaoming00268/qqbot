"""Receive QQ uploads through a scoped console request, without model work."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from console_tools.files import ConsoleFileError, safe_file_name
from media_downloader import download_url, public_url, sha256_file


async def receive_qq_file(client, broker, token, file_id, ctx):
    record = broker.attachment(token, file_id)
    info = record["info"]
    config = client._get_media_service().config
    root = Path(config.cache_dir).resolve() / "received" / hashlib.sha256(ctx.scope.encode()).hexdigest()[:20]
    target = root / file_id / safe_file_name(info["name"])
    if target.is_file() and info.get("sha256"):
        if await asyncio.to_thread(sha256_file, target) == info["sha256"]:
            return {"file_id": file_id, "name": info["name"], "local_path": str(target),
                    "size": target.stat().st_size, "sha256": info["sha256"], "state": "received"}
    limit = config.max_file_mb * 1024 * 1024
    if info["size"] > limit:
        raise ConsoleFileError("FILE_TOO_LARGE", "上传文件超过接收大小限制。")
    url = info.get("url")
    if info.get("native_id") and ctx.transport == "onebot":
        video = info.get("media_type") == "video"
        action = "get_video" if video else "get_group_file_url" if ctx.is_group else "get_private_file_url"
        params = {"file" if video else "file_id": info["native_id"]}
        if ctx.is_group and not video:
            params.update(group_id=int(ctx.conversation_id), busid=info["busid"])
        try:
            response = await client.send_action(action, params)
            url = (response.get("data") or {}).get("url") or url
        except Exception:
            if not url:
                raise
    if not url:
        raise ConsoleFileError("FILE_URL_UNAVAILABLE", "网关没有返回文件下载地址。", "请重新上传文件后使用新的 file_id。")
    ctx.check_current()
    target.parent.mkdir(parents=True, exist_ok=True)
    await download_url(public_url(url), target, max_bytes=limit, timeout=config.download_timeout_seconds)
    ctx.check_current()
    if info["size"] and target.stat().st_size != info["size"]:
        target.unlink(missing_ok=True)
        raise ConsoleFileError("FILE_SIZE_MISMATCH", "下载文件大小与 QQ 上传记录不一致。", "重新接收或让群友重新上传。")
    digest = await asyncio.to_thread(sha256_file, target)
    broker.save_received_file(token, file_id, local_path=str(target), sha256=digest)
    return {"file_id": file_id, "name": info["name"], "local_path": str(target),
            "size": target.stat().st_size, "sha256": digest, "state": "received"}
