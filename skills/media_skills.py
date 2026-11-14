# -*- coding: utf-8 -*-
"""
Media Skills: Send Image, Character Standing Art (立绘), Wallpaper, and Video.
Native tasks are completed by Antigravity before sending local artifacts.
"""

from __future__ import annotations
import logging
from typing import Any, Dict, List

from skills.base import BaseSkill, SkillParameter, SkillContext, SkillResult
from media_models import MediaIntent

logger = logging.getLogger("MediaSkills")


async def _send_media(intent: MediaIntent, context: SkillContext) -> SkillResult:
    if not context.media_context:
        return SkillResult(success=False, message="当前上下文缺少有效媒体发送通道", action_type="error")
    from media_jobs import MediaService
    from config import MediaConfig

    media_cfg = getattr(context.app_config, "media", None) or MediaConfig()
    ctx = context.media_context
    if context.ai_client is not None:
        ctx.ai_client = context.ai_client
        ctx.ai_session_key = context.session_key
    service = context.media_service or MediaService(media_cfg, namespace=f"{ctx.transport}:{ctx.account_id}")
    outcome = {}
    await service.handle(ctx, intent=intent, outcome=outcome)
    success = bool(outcome.get("success"))
    # handle() means the request was routed; only a platform receipt proves a
    # send succeeded. Errors/waiting states are already reported by the service.
    reply_handled = bool(outcome.get("reply_handled"))
    message = outcome.get("reply_text", "") if reply_handled else outcome.get("message", "媒体请求未完成。")
    return SkillResult(success=success, message=message,
                       action_type="media", data=outcome, reply_handled=reply_handled)


class SendImageSkill(BaseSkill):
    """搜索并发送图片/立绘/壁纸/插画技能"""
    name = "send_image"
    display_name = "发送图片/立绘"
    description = "在群聊或私聊中搜索并发送动漫立绘、角色插画、壁纸、照片或表情包到当前会话。严禁谎称无法发图！"
    parameters = [
        SkillParameter(name="query", param_type="string", description="图片或角色立绘搜索关键词，如'初雪樱 玉树樱 立绘'或'柴犬 壁纸'", required=True),
        SkillParameter(name="count", param_type="integer", description="用户指定的图片数量；未指定时留空", required=False),
        SkillParameter(name="orientation", param_type="string", description="仅用户明确指定方向时填写 portrait（竖图）或 landscape（横图）；不要由立绘、壁纸等类型推导", required=False, default="")
    ]

    async def execute(self, params: Dict[str, Any], context: SkillContext) -> SkillResult:
        query = str(params.get("query") or "").strip()
        if not query:
            return SkillResult(success=False, message="缺少图片搜索关键词", action_type="error")

        count = max(1, int(params.get("count") or 1))
        orientation = str(params.get("orientation") or "").strip()

        logger.info(f"🎨 [SendImageSkill] 正在检索并发送图片: query='{query}', count={count}, orientation='{orientation}'")

        return await _send_media(MediaIntent(operation="search_send", kind="image", query=query,
            request_text=context.media_context.text if context.media_context else query,
            count=count, count_requested=bool(params.get("count")), orientation=orientation,
            send_requested=True, silent_summary=True), context)


class SendVideoSkill(BaseSkill):
    """搜索并发送短视频技能"""
    name = "send_video"
    display_name = "发送视频"
    description = "在群聊或私聊中搜索并发送相关短视频片段"
    parameters = [
        SkillParameter(name="query", param_type="string", description="视频搜索关键词", required=True),
        SkillParameter(name="max_duration", param_type="integer", description="用户明确要求的最大时长（秒）；未指定时留空", required=False)
    ]

    async def execute(self, params: Dict[str, Any], context: SkillContext) -> SkillResult:
        query = str(params.get("query") or "").strip()
        if not query:
            return SkillResult(success=False, message="缺少视频搜索关键词", action_type="error")

        max_dur = max(0, int(params.get("max_duration") or 0))
        return await _send_media(MediaIntent(operation="search_send", kind="video", query=query,
            request_text=context.media_context.text if context.media_context else query,
            count=1, max_duration=max_dur, send_requested=True, silent_summary=True), context)
