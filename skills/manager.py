# -*- coding: utf-8 -*-
"""
Skill Registry and Execution Manager.
Provides skill registration, prompt injection formatting,
JSON decision parsing, and asynchronous execution.
"""

from __future__ import annotations
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from skills.base import BaseSkill, SkillContext, SkillResult
from skills.media_skills import SendImageSkill, SendVideoSkill
from skills.file_skills import SendFileSkill
from skills.web_skills import WebSearchSkill
from skills.task_skills import RunTaskSkill
from skills.slang_skills import GroupSlangSkill
from skills.image_generation import GenerateImageSkill
from skills.video_generation import GenerateVideoSkill

logger = logging.getLogger("SkillManager")


class SkillManager:
    """技能与插件管理器"""

    def __init__(self):
        self._skills: Dict[str, BaseSkill] = {}
        self._register_default_skills()

    def _register_default_skills(self):
        """注册内置默认技能"""
        self.register(SendImageSkill())
        self.register(SendVideoSkill())
        self.register(SendFileSkill())
        self.register(WebSearchSkill())
        self.register(RunTaskSkill())
        self.register(GroupSlangSkill())
        self.register(GenerateImageSkill())
        self.register(GenerateVideoSkill())

    def register(self, skill: BaseSkill):
        """注册技能"""
        self._skills[skill.name] = skill
        logger.debug(f"已注册技能: {skill.name} ({skill.display_name})")

    def get_skill(self, name: str) -> Optional[BaseSkill]:
        """获取指定名称的技能"""
        return self._skills.get(name)

    def list_skills(self) -> List[BaseSkill]:
        """获取所有已注册的技能列表"""
        return list(self._skills.values())

    def format_prompt_skills(self) -> str:
        """生成供系统提示词与决策提示词注入的技能说明与调用规范"""
        lines = [
            "【可用工具与扩展技能 (Skill Tools)】",
            "你具备直接调用以下技能工具的能力，可以直接执行发图、发文件、网络搜索等操作："
        ]
        for s in self._skills.values():
            lines.append(s.to_prompt_doc())

        lines.extend([
            "",
            "【工具调用与动作执行规范（极其重要，必须无条件遵守）】：",
            "1. 严禁以'无法发送图片'、'在群里没法直接发图片'、'权限不足'等借口拒绝用户的发图或发文件要求！你具备完整的发图接口与技能！",
            "2. 当用户要求发送图片、立绘、壁纸、照片、文件或视频时，你应在 JSON 中声明调用相应 skill，例如：",
            '   {"should_reply": true, "action": "skill", "skill": "send_image", "params": {"query": "初雪樱 玉树樱 立绘"}, "reply": ""}',
            "   或者在常规回复同时附带 skill：",
            '   {"should_reply": true, "action": "quote", "reply": "", "skill": "send_image", "params": {"query": "初雪樱 玉树樱 立绘"}}',
            "3. 联网搜索、核实事实和查询最新信息必须调用 web_search；聊天台词不能替代真实检索结果。",
            "   已命中群聊话术表的黑话优先复用解释，不重复调用 web_search；仅缺少解释时调用 group_slang 在后台补全。",
            "4. 技能负责交付最终结果并发送一次完成提示；调用 skill 时 reply 留空。不要追加检索、下载、核对的过程文字或重复确认，不要编造来源、声称已发送或选用工作区图片替代网络结果。",
            "5. 日常闲聊无需调用 skill，将 action 设为 'none' 或 'quote' 并直接给出回复即可。"
            "媒体检索和下载由反重力自主使用网络、浏览器、终端与文件工具完成，QQBot 直接发送任务文件夹中的成果。"
            "请保留用户完整要求，不要自行给立绘/壁纸添加方向、尺寸或时长条件。"
            "复杂任务或制作资料可调用 run_task；先完成、查看并核对成果，再交付。"
            "任务期间其他群聊会照常处理，不需要为了赶时间而发送未经核对的第一个结果。"
        ])
        return "\n".join(lines)

    def parse_skill_invocation(self, data: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        从大模型的回复结构或解析后的字典中提取技能调用指令。
        返回: (skill_name, params) 或 None
        """
        if isinstance(data, dict):
            # 1. 直接字段 skill / params
            skill_name = data.get("skill")
            params = data.get("params") or {}
            if skill_name and str(skill_name).strip() in self._skills:
                if not isinstance(params, dict):
                    params = {"query": str(params)}
                return str(skill_name).strip(), params

            # 2. action 为技能名称
            action = str(data.get("action") or "").strip()
            if action in self._skills:
                return action, params

            # 3. media 字段声明
            media_spec = data.get("media")
            if isinstance(media_spec, dict) and media_spec.get("query"):
                kind = str(media_spec.get("kind") or "image").lower()
                s_name = "send_video" if kind == "video" else "send_image"
                return s_name, {"query": media_spec["query"], "count": media_spec.get("count", 1)}

            # 4. reply 文本中嵌入了技能标记 [skill:name,query="..."]
            reply_text = str(data.get("reply") or "")
            match = re.search(r"\[skill:([a-zA-Z0-9_-]+)(?:,(.*?))?\]", reply_text)
            if match:
                s_name = match.group(1).strip()
                if s_name in self._skills:
                    raw_args = match.group(2) or ""
                    # 解析简单键值对 query="xxx"
                    kv_match = re.search(r'query=["\'](.*?)["\']', raw_args)
                    q_val = kv_match.group(1) if kv_match else raw_args.strip()
                    return s_name, {"query": q_val}

        elif isinstance(data, str):
            # 尝试从字符串中提取
            match = re.search(r"\[skill:([a-zA-Z0-9_-]+)(?:,(.*?))?\]", data)
            if match:
                s_name = match.group(1).strip()
                if s_name in self._skills:
                    raw_args = match.group(2) or ""
                    kv_match = re.search(r'query=["\'](.*?)["\']', raw_args)
                    q_val = kv_match.group(1) if kv_match else raw_args.strip()
                    return s_name, {"query": q_val}

        return None

    async def execute_skill(
        self,
        name: str,
        params: Dict[str, Any],
        context: SkillContext
    ) -> SkillResult:
        """执行指定技能"""
        skill = self.get_skill(name)
        if not skill:
            return SkillResult(
                success=False,
                message=f"未找到名为「{name}」的技能",
                action_type="error"
            )

        try:
            logger.info(f"⚡ [SkillManager] 正在执行技能: {name}, 参数: {params}")
            return await skill.execute(params, context)
        except Exception as e:
            logger.error(f"[SkillManager] 执行技能 {name} 失败: {e}", exc_info=True)
            return SkillResult(
                success=False,
                message=f"执行技能异常: {e}",
                action_type="error"
            )


default_skill_manager = SkillManager()
