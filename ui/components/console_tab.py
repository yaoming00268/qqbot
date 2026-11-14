# -*- coding: utf-8 -*-
"""
Console & Execution Logs Tab Component for QQBot Studio.
Provides bot process lifecycle control (start/stop/restart),
live terminal logs display, and runtime memory/session control buttons.
"""

from __future__ import annotations
import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextEdit, QPlainTextEdit, QGroupBox, QMessageBox, QCheckBox, QTabWidget
)

from ui.components.slang_editor import SlangGlossaryEditor
from ui.theme import create_vector_icon

logger = logging.getLogger("ConsoleTab")


class ConsoleTabWidget(QWidget):
    """运行控制台与实时日志选项卡组件"""
    MAX_LOG_BLOCKS = 5000
    start_requested = Signal()
    stop_requested = Signal()
    restart_requested = Signal()
    clear_memory_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None, *, glossary=None):
        super().__init__(parent)
        self._glossary = glossary
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # 顶部操作卡片
        ctl_box = QGroupBox("机器人运行控制")
        ctl_layout = QHBoxLayout(ctl_box)
        ctl_layout.setContentsMargins(12, 14, 12, 10)
        ctl_layout.setSpacing(10)

        self.btn_start = QPushButton("启动机器人服务")
        self.btn_start.setIcon(create_vector_icon("bot", "#ffffff", 16))
        self.btn_start.setObjectName("primaryBtn")
        self.btn_start.clicked.connect(self.start_requested.emit)

        self.btn_stop = QPushButton("停止服务")
        self.btn_stop.setIcon(create_vector_icon("trash", "#ffffff", 16))
        self.btn_stop.setObjectName("dangerBtn")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_requested.emit)

        self.btn_restart = QPushButton("重启服务")
        self.btn_restart.setIcon(create_vector_icon("refresh", "#ffffff", 16))
        self.btn_restart.setEnabled(False)
        self.btn_restart.clicked.connect(self.restart_requested.emit)

        self.btn_clear_mem = QPushButton("重置所有群记忆与会话")
        self.btn_clear_mem.setIcon(create_vector_icon("clear", "#ffffff", 14))
        self.btn_clear_mem.clicked.connect(self.clear_memory_requested.emit)

        ctl_layout.addWidget(self.btn_start)
        ctl_layout.addWidget(self.btn_stop)
        ctl_layout.addWidget(self.btn_restart)
        ctl_layout.addStretch()
        ctl_layout.addWidget(self.btn_clear_mem)

        layout.addWidget(ctl_box)

        # 实时终端卡片
        log_box = QGroupBox("实时日志终端 (Live Output Terminal)")
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(10, 14, 10, 10)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("日志过滤:"))
        self.edit_filter = QLineEdit()
        self.edit_filter.setPlaceholderText("输入关键字过滤输出日志...")
        filter_row.addWidget(self.edit_filter)
        self.btn_clear_terminal = QPushButton("清空终端")
        self.btn_clear_terminal.clicked.connect(self.clear_terminal)
        self.chk_autoscroll = QCheckBox("自动滚屏")
        self.chk_autoscroll.setChecked(True)
        filter_row.addWidget(self.chk_autoscroll)
        filter_row.addWidget(self.btn_clear_terminal)
        log_layout.addLayout(filter_row)

        self.log_terminal = QPlainTextEdit()
        self.log_terminal.setReadOnly(True)
        self.log_terminal.setMaximumBlockCount(self.MAX_LOG_BLOCKS)
        self.log_terminal.setUndoRedoEnabled(False)
        self.log_terminal.setFont(QFont("Cascadia Code", 11) if QFont("Cascadia Code").exactMatch() else QFont("Consolas", 11))
        self.log_terminal.setStyleSheet(
            "background-color: #080a0f; color: #e2e8f0; border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 6px; padding: 6px;"
        )
        log_layout.addWidget(self.log_terminal)

        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.addTab(log_box, "运行日志")
        self.slang_editor = SlangGlossaryEditor(self._glossary, parent=self)
        self.workspace_tabs.addTab(self.slang_editor, "话术表")
        layout.addWidget(self.workspace_tabs, 1)

    def set_running_state(self, is_running: bool):
        """设置运行状态与按钮可用性"""
        self.btn_start.setEnabled(not is_running)
        self.btn_stop.setEnabled(is_running)
        self.btn_restart.setEnabled(is_running)

    def append_log(self, text: str):
        """追加一行或多行日志到终端中"""
        if not text:
            return
        filter_kw = self.edit_filter.text().strip().lower()
        lines = [line for line in text.splitlines() if not filter_kw or filter_kw in line.lower()]
        if not lines:
            return
        scroll = self.log_terminal.verticalScrollBar()
        previous_scroll = scroll.value()
        self.log_terminal.appendPlainText("\n".join(lines[-self.MAX_LOG_BLOCKS:]))
        if self.chk_autoscroll.isChecked():
            cursor = self.log_terminal.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.log_terminal.setTextCursor(cursor)
        else:
            scroll.setValue(previous_scroll)

    def clear_terminal(self):
        self.log_terminal.clear()
