"""Validated role/account configuration and read-only receipts in the studio."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import yaml
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton

from agent_core.configuration import AgentConfig


class ActorTabWidget(QWidget):
    save_requested = Signal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        layout = QVBoxLayout(self)
        hint = QLabel("角色可绑定 QQ 与多个网站账号。网站、阅读入口和发布控件在下方配置，保存前会校验。\n"
                      "独立运行请在运行模式选择 agent；登录使用 python -m agent_core login --account 账号配置键。\n"
                      "示例与配置说明：reports/AGENT_RUNTIME_GUIDE_20261109.md")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("enabled: true\nactors: {}\naccounts: {}\nbindings: {}")
        layout.addWidget(self.editor, 3)
        buttons = QHBoxLayout()
        save = QPushButton("校验并保存配置")
        save.clicked.connect(self.save_requested.emit)
        buttons.addWidget(save)
        refresh = QPushButton("刷新角色与动作状态")
        refresh.clicked.connect(self.refresh)
        buttons.addWidget(refresh)
        layout.addLayout(buttons)
        self.status = QPlainTextEdit()
        self.status.setReadOnly(True)
        self.status.setMaximumBlockCount(150)
        layout.addWidget(self.status, 2)
        self.load_config(config)

    def load_config(self, config):
        self.config = config
        self.editor.setPlainText(yaml.safe_dump(config.agent.model_dump(mode="json"), allow_unicode=True, sort_keys=False))
        self.refresh()

    def collect(self):
        value = yaml.safe_load(self.editor.toPlainText())
        if not isinstance(value, dict):
            raise ValueError("角色与账号配置必须是 YAML 对象。")
        return AgentConfig.model_validate(value)

    def refresh(self):
        from agent_core.cli import database_path
        path = database_path(self.config)
        if not path.is_file():
            self.status.setPlainText("角色服务尚未初始化。配置保存后启动即可创建。")
            return
        try:
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                actors = db.execute("SELECT id,name,enabled FROM actors ORDER BY id").fetchall()
                actions = db.execute("SELECT id,account_id,kind,state FROM actor_actions ORDER BY created DESC LIMIT 12").fetchall()
            self.status.setPlainText(json.dumps({"角色": actors, "最近动作 [ID,账号,动作,状态]": actions}, ensure_ascii=False, indent=2))
        except sqlite3.Error as error:
            self.status.setPlainText("暂未取得角色状态：" + str(error))
