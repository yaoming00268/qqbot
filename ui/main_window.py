# -*- coding: utf-8 -*-
"""
Modern Frosted Glass Main Window for QQBot Studio.
Modular, highly interactive, configurable acrylic/translucent UI.
Orchestrates tab components and sidebar monitor with 100% backward compatibility.
"""

from __future__ import annotations
import codecs
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Optional, Dict

from PySide6.QtCore import Qt, QProcess, QTimer
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTabWidget, QSplitter, QApplication, QMessageBox
)

from config import AppConfig, GroupCustomConfig, MediaConfig
from master import default_master_manager
from persona_manager import PersonaManager
from runtime_paths import module_command
from ui.theme import build_frosted_stylesheet, create_vector_icon, ACCENT_COLORS
from ui.components.monitor_sidebar import GroupChatMonitorWidget
from ui.components.persona_tab import PersonaTabWidget
from ui.components.config_tab import ConfigTabWidget
from ui.components.console_tab import ConsoleTabWidget
from ui.components.media_tab import MediaTabWidget
from ui.components.companion_tab import CompanionTabWidget
from ui.components.actor_tab import ActorTabWidget
from ui.components.emotion_tab import EmotionTabWidget

logger = logging.getLogger("MainWindow")


class BotStudioGUI(QMainWindow):
    """QQBot & Gemini 人设控制台主窗口 (现代化毛玻璃架构)"""

    def __init__(self, config_path: str = "config.yaml"):
        super().__init__()
        self.setWindowTitle("QQBot Studio - Gemini 3.8 配额中继平台")
        self.setWindowIcon(create_vector_icon("bot", "#3b82f6", 32))
        self.resize(1100, 760)
        self.setMinimumSize(940, 640)

        # 视觉可调节参数 (组件可调节项)
        self.ui_opacity: float = 0.85
        self.ui_font_size: int = 13
        self.ui_accent: str = "blue"

        # 加载配置与人设数据并绑定全局单例
        self.config_path = config_path
        self.app_config = AppConfig.load(self.config_path)
        default_master_manager.bind_config(self.app_config, self.config_path)
        self.persona_mgr = PersonaManager("personas.yaml")

        # 机器人子进程控制器与标准流缓冲区
        self.bot_process: Optional[QProcess] = None
        self._stdout_buffer: str = ""
        self._stdout_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._restart_after_stop = False
        self._close_after_stop = False

        self._setup_ui()
        self._apply_theme()

    def _apply_theme(self):
        """动态应用现代化毛玻璃样式表"""
        qss = build_frosted_stylesheet(
            opacity=self.ui_opacity,
            font_size=self.ui_font_size,
            accent=self.ui_accent
        )
        self.setStyleSheet(qss)

    def _setup_ui(self):
        central_widget = QWidget()
        central_widget.setObjectName("centralWidget")
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(10)

        # 顶部应用标题栏卡片
        header_layout = QHBoxLayout()
        header_layout.setSpacing(12)
        app_icon_lbl = QLabel()
        app_icon_lbl.setPixmap(create_vector_icon("bot", "#3b82f6", 24).pixmap(24, 24))
        header_layout.addWidget(app_icon_lbl)

        title_label = QLabel("QQBot Studio")
        title_label.setFont(QFont("Segoe UI Variable", 16, QFont.Bold))
        header_layout.addWidget(title_label)

        sub_label = QLabel("Gemini 3.8 & 角色工坊")
        sub_label.setStyleSheet("color: #64748b; font-size: 13px; margin-top: 3px;")
        header_layout.addWidget(sub_label)

        header_layout.addStretch()

        # 侧边栏监控展开/收起切换按钮
        self.btn_toggle_sidebar = QPushButton("监控侧边栏")
        self.btn_toggle_sidebar.setIcon(create_vector_icon("chat", "#ffffff", 14))
        self.btn_toggle_sidebar.setCheckable(True)
        self.btn_toggle_sidebar.setChecked(True)
        self.btn_toggle_sidebar.clicked.connect(self._toggle_sidebar)
        header_layout.addWidget(self.btn_toggle_sidebar)

        # 状态指示灯与标签
        self.lbl_status_indicator = QLabel()
        self.lbl_status_indicator.setFixedSize(10, 10)
        self.lbl_status_indicator.setStyleSheet("background-color: #ef4444; border-radius: 5px;")
        header_layout.addWidget(self.lbl_status_indicator)

        self.lbl_status = QLabel("机器人未运行")
        self.lbl_status.setStyleSheet("color: #94a3b8; font-weight: 500;")
        header_layout.addWidget(self.lbl_status)

        main_layout.addLayout(header_layout)

        # 主水平分割器 (左侧选项卡 + 右侧监控侧边栏)
        self.main_splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(self.main_splitter, 1)

        # 左侧选项卡容器
        self.tabs = QTabWidget()
        self.main_splitter.addWidget(self.tabs)

        # 实例化 4 大功能模块组件
        # Tab 1: 人设预设工坊
        self.persona_tab_widget = PersonaTabWidget(self.persona_mgr, parent=self)
        self.persona_tab = self.persona_tab_widget
        self.tabs.addTab(self.persona_tab_widget, create_vector_icon("user", "#94a3b8", 16), "人设预设工坊")

        # Tab 2: 机器人与通道配置
        self.config_tab_widget = ConfigTabWidget(self.app_config, parent=self)
        self.config_tab = self.config_tab_widget
        self.persona_tab_widget.persona_renamed_signal.connect(self.config_tab_widget.rename_persona_references)
        self.tabs.addTab(self.config_tab_widget, create_vector_icon("settings", "#94a3b8", 16), "机器人行为与账号")

        # Tab 3: 控制台与实时日志
        self.console_tab_widget = ConsoleTabWidget(parent=self)
        self.console_tab = self.console_tab_widget
        self.tabs.addTab(self.console_tab_widget, create_vector_icon("console", "#94a3b8", 16), "运行控制台 & 日志")

        # Tab 4: 媒体功能与任务
        self.media_tab_widget = MediaTabWidget(self.app_config, parent=self)
        self.media_tab = self.media_tab_widget
        self.tabs.addTab(self.media_tab_widget, create_vector_icon("search", "#94a3b8", 16), "媒体功能与任务")
        self.companion_tab_widget = CompanionTabWidget(self.app_config, parent=self)
        self.tabs.addTab(self.companion_tab_widget, create_vector_icon("chat", "#94a3b8", 16), "表情包与自主学习")
        self.actor_tab_widget = ActorTabWidget(self.app_config, parent=self)
        self.tabs.addTab(self.actor_tab_widget, create_vector_icon("user", "#94a3b8", 16), "角色与网站账号")
        self.emotion_tab_widget = EmotionTabWidget(self.app_config, parent=self)
        self.tabs.addTab(self.emotion_tab_widget, create_vector_icon("chat", "#94a3b8", 16), "情感分析与训练")

        # 侧边栏群聊监控 (右侧侧边栏)
        self.sidebar_monitor = GroupChatMonitorWidget(parent=self)
        self.sidebar_monitor.master_updated_signal.connect(self._on_sidebar_master_updated)
        self.main_splitter.addWidget(self.sidebar_monitor)

        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 2)

        # 信号接线
        self.config_tab_widget.save_requested.connect(self._on_save_config)
        self.config_tab_widget.theme_change_requested.connect(self._on_theme_params_changed)
        self.config_tab_widget.sidebar_width_requested.connect(self._set_sidebar_width)
        self.media_tab_widget.save_requested.connect(self._on_save_config)
        self.companion_tab_widget.save_requested.connect(self._on_save_config)
        self.actor_tab_widget.save_requested.connect(self._on_save_config)
        self.emotion_tab_widget.save_requested.connect(self._on_save_config)

        self.console_tab_widget.start_requested.connect(self._on_start_bot)
        self.console_tab_widget.stop_requested.connect(self._on_stop_bot)
        self.console_tab_widget.restart_requested.connect(self._on_restart_bot)
        self.console_tab_widget.clear_memory_requested.connect(self._on_request_clear_all_memory)

        # 暴露向后兼容属性
        self._wire_compat_attributes()

        # 底部状态提示栏
        self.statusBar().showMessage("就绪 - 欢迎使用 QQBot 控制面板")

    def _wire_compat_attributes(self):
        """为既有单元测试与外部调用暴露向后兼容的属性代理"""
        # ConfigTabWidget 兼容映射
        self.combo_run_mode = self.config_tab_widget.combo_run_mode
        self.edit_qq_appid = self.config_tab_widget.edit_qq_appid
        self.edit_qq_secret = self.config_tab_widget.edit_qq_secret
        self.chk_sandbox = self.config_tab_widget.chk_sandbox
        self.combo_ob_account = self.config_tab_widget.combo_ob_account
        self.edit_ob_acc_name = self.config_tab_widget.edit_ob_acc_name
        self.edit_ob_acc_qq = self.config_tab_widget.edit_ob_acc_qq
        self.edit_onebot_ws_url = self.config_tab_widget.edit_onebot_ws_url
        self.edit_onebot_token = self.config_tab_widget.edit_onebot_token
        self.combo_model = self.config_tab_widget.combo_model
        self.spin_timeout = self.config_tab_widget.spin_timeout
        self.spin_session_timeout = self.config_tab_widget.spin_session_timeout
        self.edit_master_id = self.config_tab_widget.edit_master_id
        self.edit_master_title = self.config_tab_widget.edit_master_title
        self.chk_strip_mentions = self.config_tab_widget.chk_strip_mentions
        self.chk_actions = self.config_tab_widget.chk_actions
        self.chk_autonomous = self.config_tab_widget.chk_autonomous
        self.chk_search = self.config_tab_widget.chk_search
        self.chk_image_filter = self.config_tab_widget.chk_image_filter
        self.spin_max_reply_len = self.config_tab_widget.spin_max_reply_len
        self.chk_memory = self.config_tab_widget.chk_memory
        self.chk_memory_enabled = self.config_tab_widget.chk_memory
        self.spin_mem_max = self.config_tab_widget.spin_mem_max
        self.spin_memory_max = self.config_tab_widget.spin_mem_max
        self.spin_mem_hist = self.config_tab_widget.spin_mem_hist
        self.spin_ctx_count = self.config_tab_widget.spin_ctx_count
        self.spin_context_count = self.config_tab_widget.spin_ctx_count
        self.table_groups = self.config_tab_widget.table_groups
        self.spin_memory_history_count = self.config_tab_widget.spin_mem_hist
        self.table_group_settings = self.config_tab_widget.table_groups
        self.edit_grp_id = QLineEdit()
        self.edit_grp_name = QLineEdit()
        self.edit_grp_master_name = QLineEdit()
        self.edit_grp_master_id = QLineEdit()

        # ConsoleTabWidget 兼容映射
        self.log_terminal = self.console_tab_widget.log_terminal
        self.chk_autoscroll = self.console_tab_widget.chk_autoscroll

        # MediaTabWidget 兼容映射
        self.media_checks = self.media_tab_widget.media_checks
        self.media_numbers = self.media_tab_widget.media_numbers
        self.media_strings = self.media_tab_widget.media_strings
        self.media_job_log = self.media_tab_widget.media_job_log
        self.media_capabilities = self.media_tab_widget.media_capabilities
        self.media_preview = self.media_tab_widget.media_preview
        self.media_preview_info = self.media_tab_widget.media_preview_info

        # PersonaTabWidget 兼容映射
        self.persona_list = self.persona_tab_widget.persona_list
        self.edit_persona_name = self.persona_tab_widget.edit_persona_name
        self.edit_persona_prompt = self.persona_tab_widget.edit_persona_prompt
        self.edit_persona_avatar = self.persona_tab_widget.edit_persona_avatar

    def _toggle_sidebar(self):
        """展开或收起群聊监控侧边栏"""
        is_vis = self.sidebar_monitor.isVisible()
        self.sidebar_monitor.setVisible(not is_vis)
        self.btn_toggle_sidebar.setChecked(not is_vis)

    def _set_sidebar_width(self, width: int):
        """调节侧边栏宽度"""
        total = self.main_splitter.width()
        if total > width:
            self.main_splitter.setSizes([total - width, width])

    def _on_theme_params_changed(self, opacity: float, font_size: int, accent: str):
        self.ui_opacity = opacity
        self.ui_font_size = font_size
        self.ui_accent = accent
        self._apply_theme()

    def _on_sidebar_master_updated(self, group_id: str, user_id: str, title: str):
        """侧边栏右键修改主人后，同步重新加载配置并刷新各群表格"""
        try:
            self.app_config = AppConfig.load(self.config_path)
            default_master_manager.bind_config(self.app_config, self.config_path)
            self.config_tab_widget.load_config(self.app_config)
            self.sidebar_monitor._refresh_all_display()
        except Exception as e:
            logger.debug(f"更新侧边栏主人配置同步失败: {e}")

    def _load_config_to_ui(self):
        self.config_tab_widget.load_config(self.app_config)
        self.media_tab_widget.load_media_config()
        self.companion_tab_widget.load_config(self.app_config)
        self.actor_tab_widget.load_config(self.app_config)
        self.emotion_tab_widget.load_config(self.app_config)

    def _load_personas_to_ui(self):
        self.persona_tab_widget.load_personas()

    def _load_group_settings_to_ui(self):
        self.config_tab_widget.load_group_settings()

    def _save_group_settings_from_ui(self):
        self.config_tab_widget.save_group_settings()

    def _on_add_or_update_group_setting(self):
        gid = self.edit_grp_id.text().strip()
        gname = self.edit_grp_name.text().strip()
        mname = self.edit_grp_master_name.text().strip()
        mid = self.edit_grp_master_id.text().strip()
        if not gid:
            return
        if mname and not mid:
            QMessageBox.warning(self, "群主人鉴权提示", f"群聊 [{gid}] 仅填写了主人昵称，未绑定真实主人 QQ/OpenID。\n主人鉴权仅使用平台稳定 ID，该群将回退使用全局主人 ID。建议补全真实 QQ 号。")
        cfg = GroupCustomConfig(
            group_id=gid,
            group_name=gname,
            master_name=mname,
            master_id=mid,
            master_nickname=mname,
            master_title="主人"
        )
        self.app_config.bot.group_settings[gid] = cfg
        self._load_group_settings_to_ui()

    def _on_delete_group_setting(self, group_id: str):
        gid = str(group_id).strip()
        if gid in self.app_config.bot.group_settings:
            del self.app_config.bot.group_settings[gid]
            self._load_group_settings_to_ui()

    def _save_current_account_fields(self):
        self.config_tab_widget._save_current_ob_account_fields()

    def _refresh_media_capabilities(self):
        self.media_tab_widget.refresh_media_capabilities()

    def _show_media_asset(self, payload: dict):
        self.media_tab_widget.show_media_asset(payload)

    def _on_save_config(self, show_msg: bool = True) -> bool:
        """保存全局配置与单群隔离配置至 config.yaml"""
        try:
            sticker_config, sandbox_config = self.companion_tab_widget.collect()
            agent_config = self.actor_tab_widget.collect()
            emotion_config = self.emotion_tab_widget.collect()
        except (ValueError, TypeError) as error:
            QMessageBox.warning(self, "配置未保存", str(error))
            return False
        try:
            self.config_tab_widget._save_current_ob_account_fields()
            self.config_tab_widget.save_group_settings()

            # 读取行为参数
            self.app_config.bot.mode = self.combo_run_mode.currentData() or "official"
            self.app_config.qq.appid = self.edit_qq_appid.text().strip()
            self.app_config.qq.secret = self.edit_qq_secret.text().strip()
            self.app_config.qq.is_sandbox = self.chk_sandbox.isChecked()

            self.app_config.antigravity.default_model = self.combo_model.currentData() or "flash"
            self.app_config.antigravity.timeout_seconds = self.spin_timeout.value()
            self.app_config.antigravity.session_timeout_minutes = self.spin_session_timeout.value()

            self.app_config.bot.master_id = self.edit_master_id.text().strip()
            self.app_config.bot.master_title = self.edit_master_title.text().strip() or "主人"
            self.app_config.bot.strip_mentions = self.chk_strip_mentions.isChecked()
            self.app_config.bot.enable_action_descriptions = self.chk_actions.isChecked()
            self.app_config.bot.autonomous_enabled = self.chk_autonomous.isChecked()
            self.app_config.bot.never_sleep = self.config_tab_widget.chk_never_sleep.isChecked()
            self.app_config.bot.idle_sleep_minutes = self.config_tab_widget.spin_idle_sleep.value()
            self.app_config.bot.proactive_chat_enabled = self.config_tab_widget.chk_proactive_chat.isChecked()
            self.app_config.bot.proactive_interval_minutes = self.config_tab_widget.spin_proactive_interval.value()
            self.app_config.bot.self_activity_enabled = self.config_tab_widget.chk_self_activity.isChecked()
            self.app_config.bot.self_activity_interval_minutes = self.config_tab_widget.spin_self_activity_interval.value()
            self.app_config.bot.search_enabled = self.chk_search.isChecked()
            self.app_config.bot.image_filter_enabled = self.chk_image_filter.isChecked()
            self.app_config.bot.qq_window_capture = self.config_tab_widget.chk_qq_capture.isChecked()
            for key, spin in self.config_tab_widget.runtime_numbers.items():
                setattr(self.app_config.bot, key, spin.value())

            self.app_config.bot.max_reply_length = self.spin_max_reply_len.value()
            self.app_config.bot.memory_enabled = self.chk_memory.isChecked()
            self.app_config.bot.memory_max_entries = self.spin_mem_max.value()
            self.app_config.bot.memory_history_count = self.spin_mem_hist.value()
            self.app_config.bot.context_message_count = self.spin_ctx_count.value()

            # 收集媒体参数
            media_vals = self.media_tab_widget.collect_media_config_values()
            self.app_config.media = MediaConfig.model_validate(media_vals)
            self.app_config.stickers, self.app_config.sandbox = sticker_config, sandbox_config
            self.app_config.agent = agent_config
            self.app_config.emotion = emotion_config

            # 持久化落盘并同步绑定管理器
            self.app_config.save(self.config_path)
            default_master_manager.bind_config(self.app_config, self.config_path)
            self.media_tab_widget.refresh_media_capabilities()

            if show_msg:
                QMessageBox.information(self, "保存成功", "所有机器人配置、主人特权与群聊交互设置已成功保存至 config.yaml！")
            self.statusBar().showMessage("配置已成功保存至 config.yaml", 4000)
            return True
        except Exception as e:
            QMessageBox.warning(self, "保存配置失败", f"配置校验或写入失败: {e}")
            return False

    # ========================================================
    # 机器人进程控制与生命周期管理
    # ========================================================
    def _on_start_bot(self):
        """启动机器人子进程"""
        if self.bot_process and self.bot_process.state() != QProcess.NotRunning:
            return

        if not self._on_save_config(show_msg=False):
            return

        self._stdout_buffer = ""
        self._stdout_decoder.reset()
        self.bot_process = QProcess(self)
        self.bot_process.setProcessChannelMode(QProcess.MergedChannels)
        self.bot_process.readyReadStandardOutput.connect(self._on_process_output)
        self.bot_process.finished.connect(self._on_process_finished)
        self.bot_process.started.connect(self._on_process_started)
        self.bot_process.errorOccurred.connect(self._on_process_error)

        executable, *arguments = module_command("core")
        self.bot_process.setWorkingDirectory(str(Path.cwd()))
        self.console_tab_widget.append_log(f"[{time.strftime('%H:%M:%S')}] 正在启动 QQBot 核心进程: {executable}")
        self.bot_process.start(executable, arguments)

    def _on_process_started(self):
        """进程正式成功拉起后更新界面运行状态"""
        self.console_tab_widget.set_running_state(True)
        self.lbl_status_indicator.setStyleSheet("background-color: #10b981; border-radius: 5px;")
        self.lbl_status.setText("机器人运行中")

    def _on_process_error(self, error):
        """进程启动失败或崩溃异常处理"""
        self.console_tab_widget.set_running_state(False)
        self.lbl_status_indicator.setStyleSheet("background-color: #ef4444; border-radius: 5px;")
        self.lbl_status.setText("启动失败")
        self.console_tab_widget.append_log(f"[{time.strftime('%H:%M:%S')}] 机器人核心进程启动失败: {error}")

    def _on_process_finished(self, exit_code, exit_status):
        """进程退出后恢复控件状态"""
        self.console_tab_widget.set_running_state(False)
        self.lbl_status_indicator.setStyleSheet("background-color: #ef4444; border-radius: 5px;")
        self.lbl_status.setText("机器人未运行")
        self.console_tab_widget.append_log(f"[{time.strftime('%H:%M:%S')}] 机器人进程已退出 (代码: {exit_code})")
        if getattr(self, "_restart_after_stop", False):
            self._restart_after_stop = False
            self._on_start_bot()

    def _on_stop_bot(self):
        """停止机器人子进程"""
        if not self.bot_process or self.bot_process.state() == QProcess.NotRunning:
            return

        from runtime_control import request_stop
        request_stop()
        self.console_tab_widget.append_log(f"[{time.strftime('%H:%M:%S')}] 正在向机器人进程发送优雅退出请求...")
        self.lbl_status.setText("正在保存记忆并停止")

    def _force_kill_if_still_running(self):
        if self.bot_process and self.bot_process.state() != QProcess.NotRunning:
            self.bot_process.terminate()
            if not self.bot_process.waitForFinished(1000):
                self.bot_process.kill()

    def _on_restart_bot(self):
        """重启机器人"""
        if self.bot_process and self.bot_process.state() != QProcess.NotRunning:
            if not self._on_save_config(show_msg=False):
                return
            self._restart_after_stop = True
            self._on_stop_bot()
        else:
            self._on_start_bot()

    def _on_process_output(self):
        """处理机器人标准输出，解析实时监控事件并渲染到终端与侧边栏"""
        if not self.bot_process:
            return

        data = self.bot_process.readAllStandardOutput().data()
        self._consume_process_output(data)

    def _consume_process_output(self, data, *, final=False):
        # QProcess may split a Chinese character or emoji across byte chunks.
        chunk = self._stdout_decoder.decode(data, final=final)
        self._stdout_buffer += chunk
        if final and self._stdout_buffer and not self._stdout_buffer.endswith("\n"):
            self._stdout_buffer += "\n"

        lines = self._stdout_buffer.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._stdout_buffer = lines.pop()
        else:
            self._stdout_buffer = ""

        filtered_logs = []
        for line in lines:
            line_str = line.strip()
            if line_str.startswith("[FEED_MSG] "):
                try:
                    payload = json.loads(line_str[len("[FEED_MSG] "):])
                    self.sidebar_monitor.handle_feed_event("msg", payload)
                except Exception:
                    pass
            elif line_str.startswith("[FEED_ACTION] "):
                try:
                    payload = json.loads(line_str[len("[FEED_ACTION] "):])
                    self.sidebar_monitor.handle_feed_event("action", payload)
                except Exception:
                    pass
            elif line_str.startswith("[MEDIA_JOB] "):
                try:
                    payload = json.loads(line_str[len("[MEDIA_JOB] "):])
                    from media_jobs import STATE_NAMES
                    state = STATE_NAMES.get(payload.get("state"), payload.get("state", ""))
                    self.media_tab_widget.media_job_log.appendPlainText(
                        f"{payload.get('time', '')} {payload.get('id', '')} {state} {payload.get('detail', '')}"
                    )
                except Exception:
                    pass
            elif line_str.startswith("[CONTROL_ACK] "):
                filtered_logs.append("运行进程控制指令已确认：" + line_str[len("[CONTROL_ACK] "):])
            elif line_str.startswith("[MEDIA_ASSET] "):
                try:
                    self._show_media_asset(json.loads(line_str[len("[MEDIA_ASSET] "):]))
                except Exception:
                    pass
            else:
                filtered_logs.append(line.rstrip("\r\n"))

        if filtered_logs:
            self.console_tab_widget.append_log("\n".join(filtered_logs))

    def _on_process_finished(self, exit_code, exit_status):
        if self.bot_process:
            self._consume_process_output(self.bot_process.readAllStandardOutput().data(), final=True)
        self.console_tab_widget.set_running_state(False)
        self.lbl_status_indicator.setStyleSheet("background-color: #ef4444; border-radius: 5px;")
        self.lbl_status.setText("机器人已停止")
        self.console_tab_widget.append_log(f"[{time.strftime('%H:%M:%S')}] 机器人进程已退出 (代码: {exit_code})")
        if self._close_after_stop:
            self.close()
        elif self._restart_after_stop:
            self._restart_after_stop = False
            QTimer.singleShot(0, self._on_start_bot)

    def _on_request_clear_all_memory(self):
        """请求重置群记忆与所有会话"""
        confirm = QMessageBox.question(
            self,
            "确认重置所有记忆与会话",
            "确定要重置全部群记忆与正在进行的会话上下文吗？该操作将即刻同步至运行中机器人。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if confirm == QMessageBox.Yes:
            from runtime_control import request_clear_all
            req_id = request_clear_all()
            self.console_tab_widget.append_log(f"[{time.strftime('%H:%M:%S')}] 已下发全量清空指令 (ID: {req_id})")
            self.statusBar().showMessage("已下发清空群记忆指令", 4000)

    def closeEvent(self, event):
        """窗口关闭时自动清理运行中的子进程"""
        if not self.emotion_tab_widget.prepare_close():
            event.ignore()
            return
        if not self.console_tab_widget.slang_editor.confirm_close():
            event.ignore()
            return
        if self.bot_process and self.bot_process.state() != QProcess.NotRunning:
            self._close_after_stop = True
            self._restart_after_stop = False
            self._on_stop_bot()
            event.ignore()
            return
        event.accept()
