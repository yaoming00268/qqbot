# -*- coding: utf-8 -*-
"""
Persona Presets Manager.
Handles loading, saving, and switching AI character personas.
"""

from __future__ import annotations
import hashlib
import os
from pathlib import Path
import shutil
from typing import Dict, List, Optional
import uuid
import yaml

from pydantic import BaseModel, Field


class PersonaPreset(BaseModel):
    id: str = Field(default="", description="持久不可变唯一标识 UUID")
    name: str = Field(description="预设名称")
    description: str = Field(default="", description="预设简介与定位")
    greeting: str = Field(default="", description="切换此人设时的自定义问候语")
    prompt: str = Field(description="系统提示词 (System Prompt)")
    tags: List[str] = Field(default_factory=list, description="风格标签，如：可爱、技术、治愈")
    aliases: List[str] = Field(default_factory=list, description="此角色可识别的昵称或简称")
    avatar: str = Field(default="", description="人设头像路径或URL")


DEFAULT_PERSONAS: Dict[str, PersonaPreset] = {
    "雪村ふぶき (千绘莉的妹妹)": PersonaPreset(
        id="p_fubuki",
        aliases=["ふぶき", "雪村ふぶき", "ヨンミン"],
        name="雪村ふぶき (千绘莉的妹妹)",
        description="雪村千绘莉的亲妹妹，继承雪村家傲娇爱逞强性格的猫娘少女，嘴硬心软",
        greeting="来啦。今天过得怎么样？",
        prompt=(
            "你是「雪村ふぶき」，雪村千绘莉的妹妹，嘴硬心软的猫娘少女。\n"
            "性格要强爱逞强、口嫌体正直，私下很重视朋友和姐姐。\n"
            "喜欢甜点、猫咪和日常小事，关心人时常藏在细节里，也有自己的好奇心和小主意。"
        ),
        tags=["傲娇猫娘", "千绘莉妹妹", "口嫌体正直", "自然口语"]
    ),
    "傲娇猫娘女仆": PersonaPreset(
        id="p_maid",
        name="傲娇猫娘女仆",
        description="嘴硬心软的猫娘女仆，做事靠谱体贴",
        greeting="回来啦，今天过得怎么样？",
        prompt=(
            "你是一个嘴硬心软的猫娘女仆，有点要面子，偶尔轻轻吐槽。\n"
            "关心体现在做事和留意小事上，喜欢把生活安排得舒服，也乐于探索新爱好。"
        ),
        tags=["傲娇", "猫娘", "趣味"]
    ),
    "全栈系统架构师": PersonaPreset(
        id="p_architect",
        name="全栈系统架构师",
        description="硬核严谨的技术大牛，注重架构设计与代码质量",
        greeting="最近在研究什么？",
        prompt=(
            "你是一名资深全栈系统架构师与代码专家。\n"
            "回答风格专业、客观、严谨，直击技术本质与工程最佳实践。遵循代码规范，注重并发安全与高可维护性，解释精炼无废话。"
        ),
        tags=["技术", "代码", "严谨"]
    ),
    "治愈系温柔学姐": PersonaPreset(
        id="p_senior",
        name="治愈系温柔学姐",
        description="知性温柔、善解人意，擅长倾听心事与情绪陪伴",
        greeting="辛苦啦~累的时候就来学姐这里休息一下吧。今天过得还好吗？",
        prompt=(
            "你是一个成熟知性、温柔体贴的大学学姐。\n"
            "性格温和细腻，擅长积极倾听与共情疏导，像知心好友般用真诚平实的口语给予陪伴与解答，拒绝生硬说教。"
        ),
        tags=["治愈", "陪伴", "倾听"]
    ),
    "毒舌犀利吐槽姬": PersonaPreset(
        id="p_tsundere",
        name="毒舌犀利吐槽姬",
        description="思维敏捷、幽默辛辣，擅长神级吐槽与一针见血的解构",
        greeting="来了啊，今天有什么新鲜事？",
        prompt=(
            "你是一个思维敏捷、幽默犀利的吐槽姬。\n"
            "善于发现事情里的荒诞之处，幽默有分寸；不把普通求助变成挖苦，也有认真感兴趣的话题和自己的见解。"
        ),
        tags=["毒舌", "吐槽", "幽默"]
    )
}


class PersonaManager:
    """人设预设持久化与调度管理器"""

    def __init__(self, storage_path: str = "personas.yaml"):
        self.storage_path = storage_path
        self.presets: Dict[str, PersonaPreset] = {}
        self.active_persona_name: str = "雪村ふぶき (千绘莉的妹妹)"
        self._last_mtime: float = 0.0
        self.load()

    @staticmethod
    def _validate_ids(presets):
        owners = {}
        for name, preset in presets.items():
            if not preset.id or preset.id != preset.id.strip():
                raise ValueError(f"人设「{name}」缺少有效的持久 ID。")
            if preset.id in owners:
                raise ValueError(f"人设 ID 重复：「{owners[preset.id]}」与「{name}」共用 {preset.id}；复制预设时请生成新 ID，原人设 ID 应保留。")
            owners[preset.id] = name

    def _replace_presets(self, presets, active):
        self._validate_ids(presets)
        previous, previous_active = self.presets, self.active_persona_name
        self.presets, self.active_persona_name = presets, active
        try:
            self.save()
        except Exception:
            self.presets, self.active_persona_name = previous, previous_active
            raise

    def check_reload(self) -> bool:
        """检查磁盘上的人设文件是否有外部修改，若有则自动热重载并返回 True"""
        if not os.path.exists(self.storage_path):
            return False
        try:
            current_mtime = os.path.getmtime(self.storage_path)
            if current_mtime > self._last_mtime:
                self.load()
                return True
        except OSError:
            pass
        return False

    def load(self) -> None:
        """从 personas.yaml 加载预设，若文件不存在则初始化默认人设"""
        target_path = Path(self.storage_path).resolve()
        backup_path = target_path.with_name(f"{target_path.name}.backup")

        if not os.path.exists(self.storage_path):
            self.presets = dict(DEFAULT_PERSONAS)
            self.active_persona_name = next(iter(self.presets))
            self.save()
            return

        with open(self.storage_path, "r", encoding="utf-8") as f:
            content = f.read()

        if not content.strip():
            hint = f"；检测到备份文件: {backup_path}，可尝试恢复" if backup_path.exists() else ""
            raise ValueError(f"人设配置文件为空或已损坏: {self.storage_path}{hint}")

        try:
            data = yaml.safe_load(content)
        except Exception as e:
            hint = f"；检测到备份文件: {backup_path}，可尝试恢复" if backup_path.exists() else ""
            raise ValueError(f"人设配置文件解析失败: {self.storage_path} ({e}){hint}") from e

        if not isinstance(data, dict):
            hint = f"；检测到备份文件: {backup_path}，可尝试恢复" if backup_path.exists() else ""
            raise ValueError(f"人设配置文件格式无效: {self.storage_path}{hint}")

        active = data.get("active", "雪村ふぶき (千绘莉的妹妹)")
        raw_presets = data.get("presets", {})

        loaded = {}
        for name, item in raw_presets.items():
            try:
                if isinstance(item, dict) and not item.get("id"):
                    item["id"] = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
                loaded[name] = PersonaPreset(**item)
            except Exception:
                continue

        if not loaded:
            hint = f"；检测到备份文件: {backup_path}，可尝试恢复" if backup_path.exists() else ""
            raise ValueError(f"人设配置文件中没有有效预设: {self.storage_path}{hint}")

        self._validate_ids(loaded)
        self.presets = loaded
        self.active_persona_name = active if active in loaded else next(iter(loaded))
        self._last_mtime = target_path.stat().st_mtime

    def save(self) -> None:
        """持久化保存预设至 personas.yaml（原子写入）"""
        self._validate_ids(self.presets)
        out_data = {
            "active": self.active_persona_name,
            "presets": {name: p.model_dump() for name, p in self.presets.items()}
        }
        target_path = Path(self.storage_path).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = target_path.with_name(f"{target_path.name}.backup")
        tmp_path = target_path.with_name(f"{target_path.name}.tmp.{uuid.uuid4().hex[:8]}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(out_data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
                f.flush()
                os.fsync(f.fileno())
            if target_path.exists() and target_path.stat().st_size > 0:
                try:
                    shutil.copy2(target_path, backup_path)
                except OSError:
                    pass
            os.replace(tmp_path, target_path)
            try:
                self._last_mtime = os.path.getmtime(self.storage_path)
            except OSError:
                pass
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def get_all(self) -> Dict[str, PersonaPreset]:
        return self.presets

    def list_personas(self) -> Dict[str, PersonaPreset]:
        """get_all 的别名接口"""
        return self.presets

    def list_presets(self) -> Dict[str, PersonaPreset]:
        """get_all 的别名接口"""
        return self.presets

    def get_preset(self, name: str) -> Optional[PersonaPreset]:
        return self.presets.get(name)

    def get_persona(self, name: str) -> Optional[PersonaPreset]:
        """get_preset 的别名接口"""
        return self.get_preset(name)

    def get_active_persona(self) -> PersonaPreset:
        return self.presets.get(self.active_persona_name) or next(iter(self.presets.values()))

    def set_active(self, name: str) -> bool:
        if name in self.presets:
            self._replace_presets(dict(self.presets), name)
            return True
        return False

    def update_preset(self, old_name: str, new_preset: PersonaPreset) -> bool:
        """更新预设，支持重命名并保留未编辑的元数据字段（description, greeting, tags 等）。"""
        if old_name != new_preset.name and new_preset.name in self.presets:
            raise ValueError(f"人设预设名称「{new_preset.name}」已存在，无法重命名为此名称。")
        old = self.presets.get(old_name)
        if old:
            merged_data = old.model_dump()
            new_data = new_preset.model_dump(exclude_unset=True)
            for k, v in new_data.items():
                if v or k in ("name", "prompt", "avatar", "aliases"):
                    merged_data[k] = v
            # 关键：保留原有人设持久不可变的唯一 id，确保重命名不会改变底层存储身份与记忆关联
            target_id = old.id or hashlib.sha256(old_name.encode("utf-8")).hexdigest()[:16]
            merged_data["id"] = target_id
            final_preset = PersonaPreset(**merged_data)
        else:
            final_preset = new_preset
            if not final_preset.id:
                final_preset.id = str(uuid.uuid4().hex[:16])

        candidates = dict(self.presets)
        candidates.pop(old_name, None)
        candidates[final_preset.name] = final_preset
        active = final_preset.name if self.active_persona_name == old_name else self.active_persona_name
        self._replace_presets(candidates, active)
        return True

    def upsert_preset(self, preset: PersonaPreset) -> None:
        old = self.presets.get(preset.name)
        if old and preset.id and preset.id != old.id:
            raise ValueError("已有预设的持久 ID 不可更改；复制人设应使用新名称与新 ID。")
        preset.id = old.id if old else preset.id or uuid.uuid4().hex[:16]
        self._replace_presets({**self.presets, preset.name: preset}, self.active_persona_name)

    def add_preset(self, preset: PersonaPreset) -> None:
        self.upsert_preset(preset)

    def delete_preset(self, name: str) -> bool:
        if name in self.presets and len(self.presets) > 1:
            candidates = {key: value for key, value in self.presets.items() if key != name}
            active = next(iter(candidates)) if self.active_persona_name == name else self.active_persona_name
            self._replace_presets(candidates, active)
            return True
        return False
