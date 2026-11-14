# -*- coding: utf-8 -*-
"""
Extensible Skills & Plugin Architecture for QQBot.
Exports core abstractions, built-in skills, and registry manager.
"""

from skills.base import BaseSkill, SkillParameter, SkillContext, SkillResult
from skills.media_skills import SendImageSkill, SendVideoSkill
from skills.file_skills import SendFileSkill
from skills.web_skills import WebSearchSkill
from skills.slang_skills import GroupSlangSkill
from skills.image_generation import GenerateImageSkill
from skills.video_generation import GenerateVideoSkill
from skills.manager import SkillManager, default_skill_manager

__all__ = [
    "BaseSkill",
    "SkillParameter",
    "SkillContext",
    "SkillResult",
    "SendImageSkill",
    "SendVideoSkill",
    "SendFileSkill",
    "WebSearchSkill",
    "GroupSlangSkill",
    "GenerateImageSkill",
    "GenerateVideoSkill",
    "SkillManager",
    "default_skill_manager"
]
