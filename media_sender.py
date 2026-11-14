"""Transport adapters accept verified assets, never free-form model commands."""
from __future__ import annotations

import json
import asyncio
import base64
import math
import os
from pathlib import Path
import urllib.parse
import uuid

from botpy import errors as qq_errors

from config import MediaConfig
from media_downloader import MediaError, contained_path, http_request, public_url
from media_models import MediaAsset, MessageEnvelope
from media_processor import safe_name
from messaging import SendError, SendReceipt, SendUnknown, check_onebot_response


class MediaSender:
    def __init__(self, config: MediaConfig):
        self.config = config

    async def send(self, ctx: MessageEnvelope, asset: MediaAsset, *, as_file=False, before_submit=None):
        ctx.check_current()
        # Use actual bytes on disk, even for stale/misreported asset metadata.
        # Reject before reconnect waits, publishing or gateway upload calls.
        if asset.local_path:
            from delivery_policy import check_file_size
            check_file_size(contained_path(asset.local_path, [self.config.cache_dir, "data/cache/images",
                "data/cache/exports", *self.config.local_library_roots]), self.config.delivery_max_file_mb * 1024 * 1024)
        if ctx.wait_transport:
            await ctx.wait_transport()
        ctx.check_reply_available()
        def submitting():
            ctx.check_reply_available()
            if before_submit:
                before_submit()
        if not asset.local_path and not asset.url and asset.source_url and asset.kind == "video":
            submitting()
            body = "已尝试抓取来源页，仍无法直接下载视频文件：" + public_url(asset.source_url)
            if ctx.transport == "official":
                await ctx.send_text(body, msg_seq=ctx.allocate_sequence())
            else:
                await ctx.send_text(body)
            return SendReceipt(status="link")
        path = contained_path(asset.local_path, [self.config.cache_dir, "data/cache/images", "data/cache/exports", str(Path(self.config.cache_dir) / "exports"), *self.config.local_library_roots])
        if ctx.transport == "onebot":
            gateway_path = str(path)
            gateway = getattr(getattr(ctx.send_action, "__self__", None), "account_config", None)
            if gateway:
                host = urllib.parse.urlsplit(gateway.ws_url).hostname
                if host not in ("127.0.0.1", "localhost", "::1"):
                    if not self.config.napcat_stream_enabled:
                        raise MediaError("跨设备 NapCat 请启用 napcat_stream_enabled，且网关需支持 upload_file_stream。")
                    gateway_path = await self.stream_to_gateway(ctx, asset, path)
            submitting()
            if as_file or asset.kind == "file":
                action = "upload_group_file" if ctx.is_group else "upload_private_file"
                params = {"group_id" if ctx.is_group else "user_id": int(ctx.conversation_id),
                          "file": gateway_path, "name": safe_name(asset.title or path.name)}
            else:
                action = "send_group_msg" if ctx.is_group else "send_private_msg"
                params = {"group_id" if ctx.is_group else "user_id": int(ctx.conversation_id),
                          "message": [{"type": asset.kind, "data": {"file": path.as_uri() if gateway_path == str(path) else gateway_path}}]}
            result = await ctx.send_action(action, params, timeout=self.config.upload_timeout_seconds)
            check_onebot_response(action, result)
            data = result.get("data") or {}
            receipt = SendReceipt(message_id=str(data.get("message_id", "")), file_id=str(data.get("file_id", "")))
            if ctx.cache_sent:
                ctx.cache_sent(asset, receipt)
            return receipt

        is_file = as_file or asset.kind == "file"
        if is_file and not self.config.official_file_enabled:
            link = asset.source_url or asset.url
            if not link and self.config.publish_upload_url:
                link = await self.public_asset(asset)
            if link:
                submitting()
                await ctx.send_text("当前官方通道未启用通用文件发送，可从来源获取：" + public_url(link), msg_seq=ctx.allocate_sequence())
                return SendReceipt(status="link")
            raise MediaError("当前官方通道未启用通用文件发送；请使用 OneBot 通道或配置文件发布服务。")
        remote = await self.public_asset(asset)
        ctx.check_current()
        file_type = 4 if is_file else 2 if asset.kind == "video" else 1
        async with asyncio.timeout(self.config.upload_timeout_seconds):
            if ctx.is_group:
                uploaded = await ctx.api.post_group_file(group_openid=ctx.conversation_id, file_type=file_type,
                                                        url=remote, srv_send_msg=False)
            else:
                uploaded = await ctx.api.post_c2c_file(openid=ctx.conversation_id, file_type=file_type,
                                                      url=remote, srv_send_msg=False)
        if not isinstance(uploaded, dict) or not uploaded.get("file_info"):
            raise SendError("媒体上传未返回有效凭据。")
        ctx.check_current()
        kwargs = {"msg_type": 7, "media": {"file_info": uploaded["file_info"]},
                  "msg_id": ctx.message_id, "msg_seq": ctx.allocate_sequence()}
        if before_submit:
            before_submit()
        try:
            async with asyncio.timeout(self.config.upload_timeout_seconds):
                if ctx.is_group:
                    result = await ctx.api.post_group_message(group_openid=ctx.conversation_id, **kwargs)
                else:
                    result = await ctx.api.post_c2c_message(openid=ctx.conversation_id, **kwargs)
        except (qq_errors.AuthenticationFailedError, qq_errors.NotFoundError, qq_errors.MethodNotAllowedError,
                qq_errors.ForbiddenError, qq_errors.SequenceNumberError):
            raise SendError("官方接口拒绝本次媒体发送，请检查账号权限与回复频率。") from None
        except TimeoutError:
            raise SendUnknown("媒体发送超时，状态未知，请查询结果后再决定是否重新发送。") from None
        if not isinstance(result, dict) or not result.get("id"):
            raise SendUnknown("官方接口没有返回有效消息回执，请核对聊天记录。")
        receipt = SendReceipt(message_id=str(result["id"]))
        if ctx.cache_sent:
            ctx.cache_sent(asset, receipt)
        return receipt

    async def stream_to_gateway(self, ctx, asset, path):
        """Upload verified bytes using the bundled NapCat Stream API contract."""
        stream_id = uuid.uuid4().hex
        chunk_size = 256 * 1024
        size = path.stat().st_size
        total = max(1, math.ceil(size / chunk_size))
        try:
            with path.open("rb") as stream:
                for index in range(total):
                    ctx.check_current()
                    await ctx.send_action("upload_file_stream", {
                        "stream_id": stream_id, "chunk_index": index,
                        "chunk_data": base64.b64encode(stream.read(chunk_size)).decode("ascii"),
                        "total_chunks": total, "file_size": size,
                        "filename": stream_id + path.suffix, "expected_sha256": asset.sha256,
                        "file_retention": max(300000, self.config.upload_timeout_seconds * 2000),
                    }, timeout=self.config.upload_timeout_seconds, terminal_status="chunk_received")
            result = await ctx.send_action("upload_file_stream", {"stream_id": stream_id, "is_complete": True},
                timeout=self.config.upload_timeout_seconds, terminal_status="file_complete")
            data = result.get("data") or {}
            if (data.get("status") != "file_complete" or data.get("file_size") != size
                    or not data.get("file_path") or (asset.sha256 and data.get("sha256") != asset.sha256)):
                raise MediaError("远程 NapCat 上传回执不完整，未发送媒体。")
            return data["file_path"]
        except BaseException:
            try:
                await ctx.send_action("upload_file_stream", {"stream_id": stream_id, "reset": True}, timeout=3)
            except Exception:
                pass
            raise

    async def public_asset(self, asset):
        if asset.public_url:
            return public_url(asset.public_url)
        if self.config.publish_upload_url:
            if urllib.parse.urlsplit(self.config.publish_upload_url).scheme != "https":
                raise MediaError("文件发布接口必须使用 HTTPS。")
            headers = {"Content-Type": asset.mime or "application/octet-stream", "X-Asset-ID": asset.asset_id}
            token = os.environ.get(self.config.publish_token_env, "")
            if token:
                headers["Authorization"] = "Bearer " + token
            # Endpoint is existing user-configured storage, never a server
            # exposing the bot workspace. One file is published per request.
            with Path(asset.local_path).open("rb") as stream:
                body, _, _ = await http_request(self.config.publish_upload_url, method="POST", data=stream,
                    headers=headers, max_bytes=1024 * 1024, timeout=self.config.upload_timeout_seconds)
            try:
                published = public_url(json.loads(body)["url"])
                if urllib.parse.urlsplit(published).scheme != "https":
                    raise ValueError()
                return published
            except (ValueError, KeyError, TypeError):
                raise MediaError("文件发布服务未返回有效公网地址。") from None
        if asset.url and not asset.metadata.get("converted") and not asset.metadata.get("extract_pdf"):
            if asset.kind == "image" and asset.mime not in ("image/png", "image/jpeg"):
                raise MediaError("官方通道首期支持 PNG/JPG，请换用该格式或配置发布服务。")
            if asset.kind == "video" and Path(urllib.parse.urlsplit(asset.url).path).suffix.lower() != ".mp4":
                raise MediaError("转换后的视频需要配置 HTTPS 文件发布服务，或使用 OneBot 通道。")
            return public_url(asset.url)
        raise MediaError("本地生成或转换的资源需要配置 HTTPS 文件发布服务，或使用 OneBot 通道。")
