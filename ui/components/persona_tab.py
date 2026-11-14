# -*- coding: utf-8 -*-
"""
Persona Workshop Tab Component for QQBot Studio.
Provides persona preset browsing, creation, editing, testing, and active switching.
"""

from __future__ import annotations
import logging
import re
import weakref
from typing import Optional, Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QListWidget, QListWidgetItem, QTextEdit,
    QPlainTextEdit, QMessageBox, QInputDialog, QSplitter, QGroupBox, QDialog
)

from persona_manager import PersonaManager, PersonaPreset
from ui.theme import create_vector_icon

logger = logging.getLogger("PersonaTab")


class PersonaTabWidget(QWidget):
    """人设预设工坊选项卡组件"""
    persona_changed_signal = Signal(str)
    persona_renamed_signal = Signal(str, str)

    def __init__(self, persona_mgr: PersonaManager, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.persona_mgr = persona_mgr
        # QTabWidget reparents this widget to an internal QStackedWidget.
        # A weak reference also avoids an owner/widget reference cycle.
        self._config_owner_ref = weakref.ref(parent) if parent is not None else lambda: None
        self._setup_ui()
        self.load_personas()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Horizontal)

        # 左侧列表区
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        left_title = QLabel("可用人设预设列表")
        left_title.setFont(QFont("Segoe UI", 11, QFont.Bold))
        left_layout.addWidget(left_title)

        self.persona_list = QListWidget()
        self.persona_list.currentItemChanged.connect(self._on_persona_selected)
        left_layout.addWidget(self.persona_list)

        btn_row_1 = QHBoxLayout()
        self.btn_new_persona = QPushButton("新建人设")
        self.btn_new_persona.setIcon(create_vector_icon("plus", "#ffffff", 14))
        self.btn_new_persona.clicked.connect(self._on_create_persona)

        self.btn_set_active = QPushButton("设为生效人设")
        self.btn_set_active.setIcon(create_vector_icon("star_filled", "#ffffff", 14))
        self.btn_set_active.setObjectName("primaryBtn")
        self.btn_set_active.clicked.connect(self._on_set_active_persona)
        btn_row_1.addWidget(self.btn_new_persona)
        btn_row_1.addWidget(self.btn_set_active)
        left_layout.addLayout(btn_row_1)

        self.btn_delete_persona = QPushButton("删除当前预设")
        self.btn_delete_persona.setIcon(create_vector_icon("trash", "#ef4444", 14))
        self.btn_delete_persona.setObjectName("dangerBtn")
        self.btn_delete_persona.clicked.connect(self._on_delete_persona)
        left_layout.addWidget(self.btn_delete_persona)

        splitter.addWidget(left_widget)

        # 右侧编辑区
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(6, 0, 0, 0)

        detail_group = QGroupBox("人设详细属性编辑")
        group_layout = QVBoxLayout(detail_group)
        group_layout.setSpacing(8)

        row_name = QHBoxLayout()
        row_name.addWidget(QLabel("预设名称:"))
        self.edit_persona_name = QLineEdit()
        self.edit_persona_name.setPlaceholderText("例如: 傲娇猫娘女仆")
        row_name.addWidget(self.edit_persona_name)
        group_layout.addLayout(row_name)

        row_aliases = QHBoxLayout()
        row_aliases.addWidget(QLabel("昵称 / 简称:"))
        self.edit_persona_aliases = QLineEdit()
        self.edit_persona_aliases.setPlaceholderText("可选，多个名字用逗号分隔")
        row_aliases.addWidget(self.edit_persona_aliases)
        group_layout.addLayout(row_aliases)

        row_avatar = QHBoxLayout()
        row_avatar.addWidget(QLabel("角色头像/立绘 (本地路径或URL):"))
        self.edit_persona_avatar = QLineEdit()
        self.edit_persona_avatar.setPlaceholderText("可选，如 assets/fubuki.png")
        row_avatar.addWidget(self.edit_persona_avatar)
        group_layout.addLayout(row_avatar)

        group_layout.addWidget(QLabel("核心系统提示词 (Character System Prompt):"))
        self.edit_persona_prompt = QPlainTextEdit()
        self.edit_persona_prompt.setPlaceholderText("定义角色的说话口吻、性格特点、傲娇行为与禁忌...")
        group_layout.addWidget(self.edit_persona_prompt)

        btn_row_edit = QHBoxLayout()
        self.btn_save_persona = QPushButton("保存人设变更")
        self.btn_save_persona.setIcon(create_vector_icon("settings", "#ffffff", 14))
        self.btn_save_persona.setObjectName("successBtn")
        self.btn_save_persona.clicked.connect(self._on_save_persona)

        self.btn_test_dialog = QPushButton("预览实际提示词")
        self.btn_test_dialog.setIcon(create_vector_icon("chat", "#ffffff", 14))
        self.btn_test_dialog.clicked.connect(self._on_test_dialog)
        btn_row_edit.addWidget(self.btn_save_persona)
        btn_row_edit.addWidget(self.btn_test_dialog)
        group_layout.addLayout(btn_row_edit)

        right_layout.addWidget(detail_group)
        splitter.addWidget(right_widget)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)

    def load_personas(self):
        """加载所有预设到列表控件中"""
        self.persona_list.clear()
        active_name = getattr(self.persona_mgr, "active_persona_name", "")
        if hasattr(self.persona_mgr, "list_presets"):
            presets = self.persona_mgr.list_presets()
        elif hasattr(self.persona_mgr, "get_all"):
            presets = self.persona_mgr.get_all()
        else:
            presets = getattr(self.persona_mgr, "presets", {})

        for name, preset in presets.items():
            display_text = f"★ {name} (当前生效)" if name == active_name else f"  {name}"
            item = QListWidgetItem(display_text)
            item.setData(Qt.UserRole, name)
            self.persona_list.addItem(item)
            if name == active_name:
                self.persona_list.setCurrentItem(item)

    def _on_persona_selected(self, current: Optional[QListWidgetItem], previous: Optional[QListWidgetItem] = None):
        if not current:
            return
        name = current.data(Qt.UserRole)
        preset = self.persona_mgr.get_preset(name)
        if preset:
            self.edit_persona_aliases.setText("，".join(preset.aliases))
            self.edit_persona_name.setText(preset.name)
            self.edit_persona_prompt.setPlainText(preset.prompt)
            self.edit_persona_avatar.setText(preset.avatar or "")

    def _on_create_persona(self):
        name, ok = QInputDialog.getText(self, "新建人设", "请输入新的人设名称 (如: 冷静学霸助手):")
        if ok and name.strip():
            clean_name = name.strip()
            if clean_name in self.persona_mgr.list_presets():
                QMessageBox.warning(self, "错误", f"人设预设「{clean_name}」已存在！")
                return
            new_preset = PersonaPreset(
                name=clean_name,
                prompt=f"你叫「{clean_name}」，是一位个性鲜明的QQ群友。",
                avatar=""
            )
            self.persona_mgr.add_preset(new_preset)
            self.load_personas()
            # 选中新建的项
            for i in range(self.persona_list.count()):
                item = self.persona_list.item(i)
                if item.data(Qt.UserRole) == clean_name:
                    self.persona_list.setCurrentItem(item)
                    break

    def _on_set_active_persona(self):
        current = self.persona_list.currentItem()
        if not current:
            return
        name = current.data(Qt.UserRole)
        self.persona_mgr.set_active(name)
        self.load_personas()
        self.persona_changed_signal.emit(name)
        QMessageBox.information(self, "生效成功", f"已成功切换全局生效人设为: 【{name}】！")

    def _on_save_persona(self):
        current = self.persona_list.currentItem()
        if not current:
            return
        old_name = current.data(Qt.UserRole)
        new_name = self.edit_persona_name.text().strip()
        new_prompt = self.edit_persona_prompt.toPlainText().strip()
        new_avatar = self.edit_persona_avatar.text().strip()

        if not new_name:
            QMessageBox.warning(self, "错误", "人设名称不可为空！")
            return

        if old_name != new_name and self.persona_mgr.get_preset(new_name):
            QMessageBox.warning(self, "重命名冲突", f"已存在名为「{new_name}」的人设预设，请使用其他名称！")
            return

        updated_preset = PersonaPreset(
            name=new_name,
            prompt=new_prompt,
            avatar=new_avatar,
            aliases=[name.strip() for name in re.split(r"[,，\n]", self.edit_persona_aliases.text()) if name.strip()]
        )
        try:
            self.persona_mgr.update_preset(old_name, updated_preset)
            # 同步更新单群人设引用，避免改名后单群独立人设失联
            if old_name != new_name:
                parent = self._config_owner_ref()
                app_cfg = getattr(parent, "app_config", None)
                if app_cfg and hasattr(app_cfg, "bot") and hasattr(app_cfg.bot, "group_settings"):
                    changed = False
                    for grp in app_cfg.bot.group_settings.values():
                        if grp.persona == old_name:
                            grp.persona = new_name
                            changed = True
                    if changed:
                        cfg_path = getattr(parent, "config_path", "config.yaml")
                        app_cfg.save(cfg_path)
        except Exception as err:
            QMessageBox.warning(self, "保存失败", str(err))
            return
        self.load_personas()
        self.persona_changed_signal.emit(new_name)
        if old_name != new_name:
            self.persona_renamed_signal.emit(old_name, new_name)
        QMessageBox.information(self, "保存成功", f"人设「{new_name}」配置已成功持久化保存！")

    def _on_delete_persona(self):
        current = self.persona_list.currentItem()
        if not current:
            return
        name = current.data(Qt.UserRole)
        if len(self.persona_mgr.list_presets()) <= 1:
            QMessageBox.warning(self, "错误", "至少需要保留一个生效的人设预设，无法删除！")
            return

        reply = QMessageBox.question(
            self,
            "确认删除",
            f"确定要永久删除人设预设「{name}」吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.persona_mgr.delete_preset(name)
            self.load_personas()

    def _on_test_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("主会话提示词预览")
        dialog.resize(850, 650)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("按当前编辑内容和全局配置合成；保存后应用到实际对话。"))
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setPlainText(self._preview_prompt())
        layout.addWidget(preview)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def _preview_prompt(self):
        from config import AppConfig
        from main_chat import compose_main_rules
        from prompt_builder import compose_chat_system_prompt
        app_config = getattr(self._config_owner_ref(), "app_config", None) or AppConfig()
        name = self.edit_persona_name.text().strip()
        aliases = [part.strip() for part in re.split(r"[,，\n]", self.edit_persona_aliases.text()) if part.strip()]
        names = [name]
        if app_config.bot.mode in ("onebot", "dual"):
            names = list(dict.fromkeys(part for part in [app_config.bot.bot_name,
                app_config.onebot.get_active_account().name, name, *aliases] if part))
        prompt = self.edit_persona_prompt.toPlainText().strip()
        config = app_config.agent
        current = self.persona_list.currentItem()
        selected = self.persona_mgr.get_preset(current.data(Qt.UserRole)) if current else None
        namespace = ("onebot:" + app_config.onebot.active_account if app_config.bot.mode in ("onebot", "dual")
                     else "official:" + (app_config.qq.appid or "default"))
        persona_id = (getattr(selected, "id", "") or getattr(selected, "name", "")) if selected else name
        actor_id = config.bindings.get(namespace + "::" + persona_id) or config.bindings.get(namespace)
        if config.enabled and actor_id in config.actors:
            actor = config.actors[actor_id]
            inherited = next((item for item in self.persona_mgr.presets.values()
                              if actor.persona_id in (item.id, item.name)), None) if actor.persona_id else None
            prompt = actor.prompt or (inherited.prompt if inherited else prompt)
            names = list(dict.fromkeys([*names, actor.name or actor_id]))
        return compose_main_rules(compose_chat_system_prompt(prompt,
            app_config.bot, bot_names=names))
