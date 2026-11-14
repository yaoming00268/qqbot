"""Video creation defaults to the authenticated Gemini website."""
from __future__ import annotations

from command_backends import backend_options
from skills.base import BaseSkill, SkillParameter, SkillResult


class GenerateVideoSkill(BaseSkill):
    name = "generate_video"
    display_name = "生成视频"
    description = "默认通过网页版 Gemini 生成真实视频，支持模型选择及参考图。"
    parameters = [SkillParameter("prompt", description="完整视频要求，含运动、场景和时长等需求"),
                  SkillParameter("provider", description="gemini_web 或 native；省略使用配置，默认网页版", required=False),
                  SkillParameter("model", description="单次使用的网页端账号模型", required=False)]

    async def execute(self, params, context):
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            return SkillResult(False, "缺少视频生成描述。", action_type="error")
        if context.media_service is None or context.media_context is None:
            return SkillResult(False, "视频生成缺少任务与交付上下文。", action_type="error")
        cfg = context.media_service.config
        if not cfg.enabled or not cfg.video_enabled or not cfg.video_generation_enabled:
            return SkillResult(False, "视频生成或视频发送已关闭。", action_type="error")
        try:
            route = backend_options(self.name, params.get("provider", ""), params.get("model", ""))
        except ValueError as error:
            return SkillResult(False, str(error), action_type="error")
        if (route.get("provider") or cfg.video_generation_provider) == "gemini_web":
            from skills.gemini_web import GeminiWebTaskSkill
            return await GeminiWebTaskSkill().execute({**params, "_video_generation": True}, context)
        from skills.task_skills import RunTaskSkill
        return await RunTaskSkill().execute({"task": prompt, "output_kind": "video", "_video_generation": True,
            "_requires_tools": True, "_input_files": params.get("_input_files", []),
            "_defer_delivery": params.get("_defer_delivery", True)}, context)
