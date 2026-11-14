# -*- coding: utf-8 -*-
"""
Core Plugin & Skill Architecture for QQBot + Antigravity.
Defines Skill, Parameter, Execution Context and Result abstractions.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
import json
import logging

logger = logging.getLogger("SkillsBase")


@dataclass
class SkillParameter:
    """技能参数定义"""
    name: str
    param_type: str = "string"  # "string", "integer", "boolean", "number"
    description: str = ""
    required: bool = True
    default: Any = None

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "type": self.param_type,
            "description": self.description,
            "required": self.required
        }
        if self.default is not None:
            d["default"] = self.default
        return d


@dataclass
class SkillContext:
    """技能执行上下文"""
    session_key: str
    conversation_id: str
    user_id: str
    sender_name: str = ""
    is_group: bool = True
    is_master: bool = False
    media_context: Any = None
    app_config: Any = None
    ai_client: Any = None
    media_service: Any = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillResult:
    """技能执行结果"""
    success: bool
    message: str = ""
    data: Any = None
    action_type: str = "text"  # "text", "media", "file", "error"
    assets: List[Any] = field(default_factory=list)
    # The skill/runtime owns the public reply; callers must not add model prose.
    reply_handled: bool = False

    def to_summary(self) -> str:
        status = "成功" if self.success else "失败"
        return f"[{status}] {self.message}"


class BaseSkill:
    """插件/技能基础接口"""
    name: str = "base_skill"
    display_name: str = "基础技能"
    description: str = "基础技能定义"
    parameters: List[SkillParameter] = []

    def __init__(self):
        pass

    def get_parameter_names(self) -> List[str]:
        return [p.name for p in self.parameters]

    def to_schema(self) -> dict:
        properties = {}
        required = []
        for p in self.parameters:
            properties[p.name] = {
                "type": p.param_type,
                "description": p.description
            }
            if p.default is not None:
                properties[p.name]["default"] = p.default
            if p.required:
                required.append(p.name)

        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }

    def to_prompt_doc(self) -> str:
        """生成供大模型 Prompt 阅读理解的紧凑技能说明"""
        params_str = ", ".join([
            f"{p.name}: {p.param_type}{' (必填)' if p.required else ' (选填)'}"
            for p in self.parameters
        ])
        return f"- **{self.name}** ({self.display_name}): {self.description} [参数: {params_str or '无'}]"

    async def execute(self, params: Dict[str, Any], context: SkillContext) -> SkillResult:
        """执行该技能的主逻辑，子类必须实现"""
        raise NotImplementedError("子类必须实现 execute 方法")
