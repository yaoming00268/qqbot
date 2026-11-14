"""Image creation through a native tool worker or a configured generation API."""
from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import urllib.parse
import uuid

import aiohttp

from media_downloader import MediaError, http_request
from media_models import MediaAsset
from skills.base import BaseSkill, SkillParameter, SkillResult


class GenerateImageSkill(BaseSkill):
    name = "generate_image"
    display_name = "生成图片"
    description = "通过网页端 Gemini、原生生图工具或图片生成 API 生成图片，支持参考原图。"
    parameters = [SkillParameter("prompt", description="完整图片描述"),
                  SkillParameter("count", param_type="integer", description="生成数量 1–10", required=False, default=1),
                  SkillParameter("size", description="auto 或宽x高", required=False, default="auto"),
                  SkillParameter("provider", description="native、api 或 gemini_web；省略沿用配置", required=False),
                  SkillParameter("model", description="单次使用的网页端/API 模型", required=False)]

    async def execute(self, params, context):
        from skills.task_skills import RunTaskSkill
        prompt = str(params.get("prompt") or "").strip()
        count, size = params.get("count", 1), params.get("size", "auto")
        if not prompt or type(count) is not int or not 1 <= count <= 10 or not isinstance(size, str) or not re.fullmatch(r"auto|[1-9][0-9]{1,4}x[1-9][0-9]{1,4}", size):
            return SkillResult(False, "图片描述、数量或尺寸无效。", action_type="error")
        if context.media_service is None or context.media_context is None:
            return SkillResult(False, "图片生成缺少任务与交付上下文。", action_type="error")
        cfg = context.media_service.config
        if not cfg.enabled or not cfg.image_enabled or not cfg.image_generation_enabled:
            return SkillResult(False, "图片生成或图片发送已关闭。", action_type="error")
        from command_backends import backend_options
        try:
            route = backend_options("generate_image", params.get("provider", ""), params.get("model", ""))
        except ValueError as error:
            return SkillResult(False, str(error), action_type="error")
        provider = route.get("provider") or cfg.image_generation_provider
        if provider == "gemini_web":
            from skills.gemini_web import GeminiWebTaskSkill
            return await GeminiWebTaskSkill().execute({**params, "_image_generation": True}, context)
        if provider == "native":
            return await RunTaskSkill().execute({"task": prompt + f"\n生成数量：{count}；尺寸：{size}",
                "output_kind": "image", "_image_generation": True, "_generation_count": count,
                "_input_files": params.get("_input_files", []),
                "_defer_delivery": params.get("_defer_delivery", True)}, context)
        api_model = route.get("model") or cfg.image_generation_model
        if not cfg.image_generation_api_url or not api_model:
            return SkillResult(False, "请配置图片生成 API 的完整地址和模型。", action_type="error")
        ctx, service = context.media_context, context.media_service
        parsed = urllib.parse.urlsplit(cfg.image_generation_api_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            return SkillResult(False, "图片生成 API 地址必须是有效 HTTP(S) 地址。", action_type="error")
        job, created = service.store.create_job(ctx.media_owner, ctx.media_scope, ctx.message_id)
        if not created:
            return SkillResult(False, "该生图任务已有记录，请查询状态。", data={"job_id": job["id"]})
        directory = Path(cfg.cache_dir).resolve() / "tasks" / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=True)
        service.tasks[job["id"]] = (asyncio.current_task(), ctx.media_owner, ctx.media_scope)
        if hasattr(service, "register_task_directory"):
            service.register_task_directory(job["id"], directory)
        service.update(job, "searching", "图片生成接口正在创建图片。")
        try:
            ctx.check_current()
            headers = {}
            key = os.environ.get(cfg.image_generation_api_key_env, "")
            if key:
                headers["Authorization"] = "Bearer " + key
            body = {"model": api_model, "prompt": prompt, "n": count}
            if size != "auto":
                body["size"] = size
            max_image_bytes = cfg.max_image_mb * 1024 * 1024
            # The endpoint is explicitly configured by the owner and may be a
            # local image server. Never follow redirects with its credentials.
            with ExitStack() as streams:
                endpoint = cfg.image_generation_api_url
                request = {"json": body}
                references = params.get("_input_files") or []
                if references:
                    from PIL import Image
                    form = aiohttp.FormData()
                    for name, value in body.items():
                        form.add_field(name, str(value))
                    for item in references:
                        path = Path(item["local_path"])
                        if path.stat().st_size > max_image_bytes:
                            raise MediaError("参考图片超过配置的图片大小上限。")
                        try:
                            with Image.open(path) as image:
                                fmt = image.format
                                image.verify()
                            if fmt not in ("PNG", "JPEG", "WEBP"):
                                raise ValueError("unsupported image")
                        except (OSError, ValueError):
                            raise MediaError("当前生图 API 的参考附件需为 PNG/JPEG/WebP 图片；其他资料请使用 native 生图分支处理。") from None
                        form.add_field("image" if len(references) == 1 else "image[]",
                            streams.enter_context(path.open("rb")), filename=path.name, content_type=Image.MIME[fmt])
                    # OpenAI-compatible providers distinguish generation from
                    # reference-image editing. Preserve the configured host,
                    # credentials and query; arbitrary endpoints stay as set.
                    if parsed.path.endswith("/images/generations"):
                        endpoint = urllib.parse.urlunsplit(parsed._replace(
                            path=parsed.path[:-len("generations")] + "edits"))
                    request = {"data": form}
                async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=None)) as session:
                    data = await self._request_images(session, endpoint, headers, request, max_image_bytes, count)
            ctx.check_current()
            rows = data.get("data") if isinstance(data, dict) else None
            if not isinstance(rows, list) or len(rows) != count:
                raise MediaError("图片生成接口没有返回所请求数量的图片，结果保留且不自动重复生成。")
            assets = []
            for index, item in enumerate(rows):
                if not isinstance(item, dict):
                    raise MediaError("图片生成接口返回了无效图片项。")
                path = directory / f"generated-{index + 1}.png"
                if item.get("b64_json"):
                    try:
                        raw = base64.b64decode(item["b64_json"], validate=True)
                    except (ValueError, TypeError, binascii.Error):
                        raise MediaError("图片生成接口返回的图片编码无效。") from None
                    if not raw or len(raw) > max_image_bytes:
                        raise MediaError("生成图片为空或超过配置大小。")
                    await asyncio.to_thread(path.write_bytes, raw)
                elif item.get("url"):
                    await http_request(item["url"], max_bytes=max_image_bytes, timeout=cfg.download_timeout_seconds, target=path)
                else:
                    raise MediaError("图片生成接口没有返回 b64_json 或图片 URL。")
                ctx.check_current()
                asset = await service.processor.accept_artifact(MediaAsset(kind="image", title=path.name,
                    local_path=str(path), provider="image_generation_api", metadata={"generated": True}), directory)
                assets.append(asset)
            return await RunTaskSkill()._deliver_result(context, prompt, directory, job,
                {"report": "通过配置的图片生成 API 创建并校验图片。"}, [], assets, "图片已生成。",
                params={"_defer_delivery": params.get("_defer_delivery", True)})
        except BaseException as error:
            service.update(job, "cancelled" if isinstance(error, asyncio.CancelledError) else "failed", str(error))
            if isinstance(error, asyncio.CancelledError):
                raise
            return SkillResult(False, str(error), action_type="error", data={"job_id": job["id"], "directory": str(directory)})
        finally:
            service.tasks.pop(job["id"], None)
            if hasattr(service, "release_task_directory"):
                service.release_task_directory(job["id"])

    @staticmethod
    async def _request_images(session, endpoint, headers, request, max_image_bytes, count):
        async with session.post(endpoint, headers=headers, allow_redirects=False, **request) as response:
            if response.status != 200:
                raise MediaError(f"图片生成接口返回 HTTP {response.status}，未自动重试。")
            chunks, total = [], 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > max_image_bytes * count * 2 + 1024 * 1024:
                    raise MediaError("图片生成接口响应超过配置的图片大小上限。")
                chunks.append(chunk)
            return json.loads(b"".join(chunks))
