"""Gemini website generation with scoped inputs and durable console delivery."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import shutil
import uuid

from gemini_page_prompt import build_conversational_prompt
from gemini_web_client import error_diagnostics, open_gemini_web, save_generated_image, save_generated_video, select_model, task_error
from media_downloader import MediaError, contained_path
from media_models import MediaAsset
from skills.base import BaseSkill, SkillResult


logger = logging.getLogger("GeminiWebTask")


class GeminiWebTaskSkill(BaseSkill):
    name = "gemini_web_task"

    async def execute(self, params, context):
        from skills.task_skills import RunTaskSkill
        service, ctx = context.media_service, context.media_context
        if service is None or ctx is None:
            return SkillResult(False, "网页端 Gemini 缺少任务与交付上下文。", action_type="error")
        cfg = service.config
        if not cfg.enabled:
            return SkillResult(False, "媒体任务已关闭。", action_type="error")
        task = str(params.get("task") or params.get("prompt") or "").strip()
        if not task:
            return SkillResult(False, "缺少网页端 Gemini 任务描述。", action_type="error")
        image_task = bool(params.get("_image_generation"))
        video_task = bool(params.get("_video_generation"))
        if video_task and (not cfg.video_enabled or not cfg.video_generation_enabled):
            return SkillResult(False, "视频生成或视频发送已关闭。", action_type="error")
        job, created = service.store.create_job(ctx.media_owner, ctx.media_scope, ctx.message_id)
        if not created:
            return SkillResult(False, "该任务已有记录，请查询状态。", data={"job_id": job["id"]})
        directory = Path(cfg.cache_dir).resolve() / "tasks" / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=True)
        service.tasks[job["id"]] = (asyncio.current_task(), ctx.media_owner, ctx.media_scope)
        service.register_task_directory(job["id"], directory)
        assets = []
        stage = "inputs"
        stage_state = {}
        try:
            ctx.check_current()
            service.update(job, "searching", "网页端 Gemini 正在处理任务。")
            files = await asyncio.to_thread(self._copy_inputs, params.get("_input_files") or [], directory, cfg, image_task or video_task)
            count, size = params.get("count", 1), params.get("size", "auto")
            instruction = build_conversational_prompt(
                task,
                image_task=image_task,
                video_task=video_task,
                count=count,
                size=size,
                has_files=bool(files),
                requires_network=bool(params.get("_requires_network")),
            )
            model_name = params.get("model") or cfg.gemini_web.model
            transport = getattr(cfg.gemini_web, "transport", "cookie")

            if transport == "browser":
                from gemini_page_browser import generate_page_content
                stage = "generation"
                service.update(job, "searching", "网页端 Gemini 浏览器正在处理任务。")
                async with asyncio.timeout(cfg.gemini_web.timeout_seconds):
                    output = await generate_page_content(cfg.gemini_web, instruction, files=files,
                        model=model_name or "auto", media_kind="video" if video_task else "image" if image_task else "text",
                        directory=directory)
                ctx.check_current()
                stage = "validation"
                for path in [*(output.images or []), *(output.videos or [])]:
                    contained_path(path, [directory])
                answer = await self._accept_output(output.text, output.images or [], output.videos or [],
                    image_task, video_task, count, cfg, service, job, ctx, directory, assets, model_name, stage_state=stage_state)
            else:
                stage = "connect"
                async with open_gemini_web(cfg.gemini_web) as client:
                    from gemini_webapi.types import GeneratedImage, GeneratedVideo
                    model = select_model(client, model_name)
                    stage = "generation"
                    async with asyncio.timeout(cfg.gemini_web.timeout_seconds):
                        output = await client.generate_content(instruction, files=files or None, model=model, temporary=True)
                    ctx.check_current()
                    images = [image for image in (output.images or []) if isinstance(image, GeneratedImage)]
                    videos = [video for video in (getattr(output, "videos", None) or []) if isinstance(video, GeneratedVideo)]
                    stage = "download"
                    answer = await self._accept_output(output.text, images, videos, image_task, video_task, count,
                        cfg, service, job, ctx, directory, assets, model_name, client, stage_state)

            if not answer and not assets:
                raise MediaError("网页端 Gemini 返回了空结果。")
            ctx.check_current()
            stage_state.clear()
            stage = "delivery"
            return await RunTaskSkill()._deliver_result(context, task, directory, job,
                {"report": "网页端 Gemini 任务结果。", "result": {"provider": "gemini_web", "model": model_name}},
                [], assets, "图片已生成。" if image_task else "视频已生成。" if video_task else answer,
                params={"_defer_delivery": params.get("_defer_delivery", True)})
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                service.update(job, "cancelled", "网页端 Gemini 任务已取消。")
                raise
            stage = stage_state.get("stage", stage)
            failure = task_error(error, stage, media_kind="video" if video_task else "image")
            diagnostic = getattr(failure, "diagnostic", None) or error_diagnostics(error, stage)
            logger.warning("Gemini web task failed: %s", diagnostic)
            service.update(job, "failed", str(failure))
            return SkillResult(False, str(failure), action_type="error", data={"job_id": job["id"], "diagnostic": diagnostic,
                "directory": str(directory), "files": [asset.model_dump() for asset in assets]})
        finally:
            service.tasks.pop(job["id"], None)
            service.release_task_directory(job["id"])

    @staticmethod
    async def _accept_output(answer, images, videos, image_task, video_task, count, cfg,
                             service, job, ctx, directory, assets, model_name, client=None, stage_state=None):
        answer = str(answer or "").strip()
        if image_task and not images:
            raise MediaError("网页端 Gemini 没有返回生成图片；可能需要更换模型或检查网页端生图权限。")
        if video_task and not videos:
            raise MediaError("网页端 Gemini 没有返回生成视频；请检查网页端账号的视频生成权限、配额与所选模型。")
        if image_task and not cfg.image_enabled:
            raise MediaError("图片发送已关闭，生成结果未交付。")
        if video_task and not cfg.video_enabled:
            raise MediaError("视频发送已关闭，生成结果未交付。")
        for kind, items, enabled, limit in (
            ("image", images if not video_task else [], cfg.image_enabled, cfg.max_image_mb),
            ("video", videos if not image_task else [], cfg.video_enabled, cfg.max_video_mb),
        ):
            label = "图片" if kind == "image" else "视频"
            if items and not enabled:
                raise MediaError(f"{label}发送已关闭，生成结果未交付。")
            for index, item in enumerate(items, 1):
                ctx.check_current()
                service.update(job, "downloading", f"正在保存网页端生成{label} {index}/{len(items)}。")
                if client is not None:
                    stage_state["stage"] = "download"
                    save = save_generated_image if kind == "image" else save_generated_video
                    suffix = ".png" if kind == "image" else ".mp4"
                    item = await save(item, directory, f"generated-{index}{suffix}",
                        client=client, timeout_seconds=cfg.gemini_web.timeout_seconds)
                stage_state["stage"] = "validation"
                path = contained_path(item, [directory])
                if path.stat().st_size > limit * 1024 * 1024:
                    raise MediaError(f"网页端生成{label}超过配置的{label}大小上限，原件已保留。")
                asset = await service.processor.accept_artifact(MediaAsset(kind=kind, title=path.name,
                    local_path=str(path), provider="gemini_web", metadata={"generated": True, "model": model_name}), directory)
                assets.append(asset)
            if image_task and kind == "image" and len(assets) != count:
                raise MediaError("网页端返回的生成图片数量与请求不一致，已有图片已保留，请检查任务记录。")
        return answer

    @staticmethod
    def _copy_inputs(inputs, directory, config, images_only):
        from PIL import Image
        folder = directory / "inputs"
        files = []
        for index, item in enumerate(inputs, 1):
            path = Path(item["local_path"]).resolve()
            limit = config.max_image_mb if images_only else config.max_file_mb
            if not path.is_file() or not path.stat().st_size or path.stat().st_size > limit * 1024 * 1024:
                raise MediaError("网页端 Gemini 参考附件为空、不存在或超过大小上限。")
            suffix = path.suffix
            if images_only:
                try:
                    with Image.open(path) as picture:
                        fmt = picture.format
                        picture.verify()
                    suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[fmt]
                except (OSError, ValueError, KeyError):
                    raise MediaError("网页端生图或视频生成的参考图需要 PNG/JPEG/WebP 原图，请移除其他类型附件。") from None
            folder.mkdir(exist_ok=True)
            target = folder / f"reference-{index}{suffix}"
            shutil.copy2(path, target)
            files.append(target)
        return files
