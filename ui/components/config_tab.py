# -*- coding: utf-8 -*-
"""
Configuration Tab Component for QQBot Studio.
Encapsulates Bot Behavior, Gemini Engine parameters, OneBot Account switcher,
and Group-isolated Master/Persona configuration.
"""

from __future__ import annotations
import logging
import os
from typing import Optional, Dict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QCheckBox, QSpinBox, QSlider,
    QGroupBox, QTableWidget, QTableWidgetItem, QMessageBox,
    QScrollArea, QFrame, QInputDialog
)

from config import AppConfig, GroupCustomConfig, OneBotAccountConfig
from master import default_master_manager
from napcat_manager import NapCatManager
from ui.theme import create_vector_icon, ACCENT_COLORS

logger = logging.getLogger("ConfigTab")


class ConfigTabWidget(QWidget):
    """全局配置选项卡组件"""
    save_requested = Signal()
    theme_change_requested = Signal(float, int, str)
    sidebar_width_requested = Signal(int)

    def __init__(self, app_config: AppConfig, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.app_config = app_config
        self.napcat_mgr = NapCatManager(self.app_config.onebot.napcat_path)
        self._current_ob_account_key: str = self.app_config.onebot.active_account
        self._setup_ui()
        self.load_config(self.app_config)

    def _setup_ui(self):
        tab_layout = QVBoxLayout(self)
        tab_layout.setContentsMargins(6, 6, 6, 6)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(12)
        layout.setContentsMargins(8, 8, 8, 8)

        # 0. 现代化毛玻璃与组件可调节项 (UI Customization)
        visual_group = QGroupBox("现代化毛玻璃视觉与组件调节 (UI Customization)")
        vis_layout = QVBoxLayout(visual_group)
        vis_layout.setSpacing(8)

        row_vis_1 = QHBoxLayout()
        row_vis_1.addWidget(QLabel("背景不透明度 (毛玻璃深度):"))
        self.slider_opacity = QSlider(Qt.Horizontal)
        self.slider_opacity.setRange(55, 100)
        self.slider_opacity.setValue(85)
        self.slider_opacity.valueChanged.connect(self._on_theme_param_changed)
        row_vis_1.addWidget(self.slider_opacity)

        self.lbl_opacity_val = QLabel("85%")
        self.lbl_opacity_val.setFixedWidth(40)
        row_vis_1.addWidget(self.lbl_opacity_val)

        row_vis_1.addWidget(QLabel("基础字体大小:"))
        self.spin_font_size = QSpinBox()
        self.spin_font_size.setRange(10, 20)
        self.spin_font_size.setValue(13)
        self.spin_font_size.valueChanged.connect(self._on_theme_param_changed)
        row_vis_1.addWidget(self.spin_font_size)

        row_vis_1.addWidget(QLabel("主题强调色:"))
        self.combo_accent = QComboBox()
        self.combo_accent.addItem("极简钛金蓝 (Slate Blue)", "blue")
        self.combo_accent.addItem("曜石深靛青 (Deep Indigo)", "navy")
        self.combo_accent.addItem("钛合金板岩 (Titanium Slate)", "slate")
        self.combo_accent.addItem("极光翡翠绿 (Nordic Emerald)", "emerald")
        self.combo_accent.currentIndexChanged.connect(self._on_theme_param_changed)
        row_vis_1.addWidget(self.combo_accent)
        vis_layout.addLayout(row_vis_1)

        row_vis_2 = QHBoxLayout()
        row_vis_2.addWidget(QLabel("侧边栏宽度预设:"))
        btn_w_compact = QPushButton("紧凑 (280px)")
        btn_w_compact.clicked.connect(lambda: self.sidebar_width_requested.emit(280))
        btn_w_standard = QPushButton("标准 (360px)")
        btn_w_standard.clicked.connect(lambda: self.sidebar_width_requested.emit(360))
        btn_w_wide = QPushButton("宽屏 (460px)")
        btn_w_wide.clicked.connect(lambda: self.sidebar_width_requested.emit(460))
        row_vis_2.addWidget(btn_w_compact)
        row_vis_2.addWidget(btn_w_standard)
        row_vis_2.addWidget(btn_w_wide)
        row_vis_2.addStretch()
        vis_layout.addLayout(row_vis_2)
        layout.addWidget(visual_group)

        # 1. 运行通道模式选择 Group
        mode_group = QGroupBox("机器人运行通道选择 (支持官方开放平台 / 本地个人小号双模)")
        mode_layout = QVBoxLayout(mode_group)
        row_mode = QHBoxLayout()
        row_mode.addWidget(QLabel("选择当前激活通道:"))
        self.combo_run_mode = QComboBox()
        self.combo_run_mode.addItem("official (腾讯官方开放平台 Bot - 需填 AppID / Secret)", "official")
        self.combo_run_mode.addItem("onebot (本地 QQNT 个人小号 - NapCat / LLOneBot 免 AppID)", "onebot")
        self.combo_run_mode.addItem("dual (双通道同时在线 - 官方 Bot 与 个人小号并发驱动)", "dual")
        self.combo_run_mode.addItem("agent (独立角色与通用网站账号，无需 QQ)", "agent")
        row_mode.addWidget(self.combo_run_mode)
        mode_layout.addLayout(row_mode)
        layout.addWidget(mode_group)

        # 2. QQ 官方凭证 Group
        qq_group = QGroupBox("QQ 官方开放平台参数 (https://q.qq.com/#/apps/bot)")
        qq_layout = QVBoxLayout(qq_group)
        row_qq1 = QHBoxLayout()
        row_qq1.addWidget(QLabel("机器人 AppID:"))
        self.edit_qq_appid = QLineEdit()
        row_qq1.addWidget(self.edit_qq_appid)
        row_qq1.addWidget(QLabel("机器人 AppSecret:"))
        self.edit_qq_secret = QLineEdit()
        self.edit_qq_secret.setEchoMode(QLineEdit.Password)
        row_qq1.addWidget(self.edit_qq_secret)
        self.chk_sandbox = QCheckBox("沙箱环境")
        row_qq1.addWidget(self.chk_sandbox)
        qq_layout.addLayout(row_qq1)
        layout.addWidget(qq_group)

        # 3. OneBot 本地小号配置 Group
        ob_group = QGroupBox("OneBot 本地 QQNT 个人小号配置 (NapCat / LLOneBot)")
        ob_layout = QVBoxLayout(ob_group)

        row_ob_acc = QHBoxLayout()
        row_ob_acc.addWidget(QLabel("当前配置小号:"))
        self.combo_ob_account = QComboBox()
        self.combo_ob_account.currentIndexChanged.connect(self._on_ob_account_changed)
        row_ob_acc.addWidget(self.combo_ob_account, 1)

        self.btn_add_ob_acc = QPushButton("新增小号")
        self.btn_add_ob_acc.setIcon(create_vector_icon("plus", "#ffffff", 14))
        self.btn_add_ob_acc.clicked.connect(self._on_add_ob_account)
        row_ob_acc.addWidget(self.btn_add_ob_acc)

        self.btn_del_ob_acc = QPushButton("删除此小号")
        self.btn_del_ob_acc.setIcon(create_vector_icon("trash", "#ef4444", 14))
        self.btn_del_ob_acc.setObjectName("dangerBtn")
        self.btn_del_ob_acc.clicked.connect(self._on_remove_ob_account)
        row_ob_acc.addWidget(self.btn_del_ob_acc)
        ob_layout.addLayout(row_ob_acc)

        row_ob1 = QHBoxLayout()
        row_ob1.addWidget(QLabel("账号备注:"))
        self.edit_ob_acc_name = QLineEdit()
        row_ob1.addWidget(self.edit_ob_acc_name)

        row_ob1.addWidget(QLabel("本地 QQ 号:"))
        self.edit_ob_acc_qq = QLineEdit()
        self.edit_ob_acc_qq.setPlaceholderText("填入小号 QQ 号")
        row_ob1.addWidget(self.edit_ob_acc_qq)

        row_ob1.addWidget(QLabel("OneBot WebSocket 地址:"))
        self.edit_onebot_ws_url = QLineEdit()
        row_ob1.addWidget(self.edit_onebot_ws_url, 1)

        row_ob1.addWidget(QLabel("Access Token:"))
        self.edit_onebot_token = QLineEdit()
        self.edit_onebot_token.setEchoMode(QLineEdit.Password)
        row_ob1.addWidget(self.edit_onebot_token)
        ob_layout.addLayout(row_ob1)

        row_napcat = QHBoxLayout()
        self.btn_download_napcat = QPushButton("下载/安装 NapCatQQ")
        self.btn_download_napcat.setIcon(create_vector_icon("download", "#ffffff", 14))
        self.btn_download_napcat.clicked.connect(self._on_download_napcat)
        row_napcat.addWidget(self.btn_download_napcat)

        self.btn_setup_napcat = QPushButton("为双账号配置独立端口")
        self.btn_setup_napcat.setIcon(create_vector_icon("settings", "#ffffff", 14))
        self.btn_setup_napcat.clicked.connect(self._on_setup_napcat_accounts)
        row_napcat.addWidget(self.btn_setup_napcat)

        self.btn_launch_napcat = QPushButton("拉起 NapCatQQ 登录")
        self.btn_launch_napcat.setIcon(create_vector_icon("play", "#ffffff", 14))
        self.btn_launch_napcat.clicked.connect(self._on_launch_napcat)
        row_napcat.addWidget(self.btn_launch_napcat)

        self.btn_open_webui = QPushButton("打开 NapCat 控制台")
        self.btn_open_webui.setIcon(create_vector_icon("search", "#ffffff", 14))
        self.btn_open_webui.clicked.connect(self._on_open_napcat_webui)
        row_napcat.addWidget(self.btn_open_webui)

        ob_layout.addLayout(row_napcat)
        layout.addWidget(ob_group)

        # 4. Antigravity & Gemini 模型配置
        ai_group = QGroupBox("Google Gemini 3.8 & Antigravity 引擎配置")
        ai_layout = QVBoxLayout(ai_group)

        row_model = QHBoxLayout()
        row_model.addWidget(QLabel("全局默认模型:"))
        self.combo_model = QComboBox()
        self.combo_model.addItem("gemini-3.8-flash (推荐日常群聊)", "flash")
        self.combo_model.addItem("gemini-3.8-pro (深度推理 / 复杂角色扮演)", "pro")
        self.combo_model.addItem("gemini-3.8-flash-lite (轻量模型)", "flash_lite")
        row_model.addWidget(self.combo_model, 1)

        ai_layout.addLayout(row_model)

        row_ai_params = QHBoxLayout()
        row_ai_params.addWidget(QLabel("Agent 工作等待:"))
        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(0, 0)
        self.spin_timeout.setSpecialValueText("不限时")
        self.spin_timeout.setEnabled(False)
        self.spin_timeout.setMaximumWidth(110)
        row_ai_params.addWidget(self.spin_timeout)

        row_ai_params.addWidget(QLabel("临时会话空闲清理 (分):"))
        self.spin_session_timeout = QSpinBox()
        self.spin_session_timeout.setRange(0, 1440)
        self.spin_session_timeout.setMaximumWidth(110)
        self.spin_session_timeout.setSpecialValueText("永不清理")
        self.spin_session_timeout.setToolTip("仅清理辅助临时会话；群聊、私聊主会话不会因空闲丢失上下文。")
        row_ai_params.addWidget(self.spin_session_timeout)
        row_ai_params.addStretch(1)
        ai_layout.addLayout(row_ai_params)
        layout.addWidget(ai_group)

        runtime_group = QGroupBox("后台任务、QQ 截图与消息排队")
        runtime_layout = QVBoxLayout(runtime_group)
        runtime_layout.addWidget(QLabel("临时任务独立并发，数量不限；主聊天按消息顺序处理。断线时缓存文字回复。"))
        self.chk_qq_capture = QCheckBox("优先使用本机 QQ 截图（不可用时后台获取原图）")
        runtime_layout.addWidget(self.chk_qq_capture)
        self.runtime_numbers = {}
        for key, label, lower, upper in (
            ("chat_buffer_seconds", "群消息固定缓存窗口（秒，回复后重新计时）", 0, 300),
            ("backlog_interrupt_messages", "积压几条消息后中断并合并回复（0 关闭）", 0, 100),
            ("passive_backlog", "繁忙时保留的自主参聊机会", 1, 20),
            ("idle_recall_minutes", "主会话空闲整理与回忆间隔（分钟）", 1, 1440),
            ("memory_archive_interval_minutes", "记忆打包归档间隔（分钟）", 1, 1440),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            spin = QSpinBox()
            spin.setRange(lower, upper)
            row.addWidget(spin)
            self.runtime_numbers[key] = spin
            runtime_layout.addLayout(row)
        layout.addWidget(runtime_group)

        # 5. 群聊自主参聊与回复行为配置
        bot_group = QGroupBox("群聊参聊行为、主人特权与记忆库设定")
        bot_layout = QVBoxLayout(bot_group)

        row_master = QHBoxLayout()
        row_master.addWidget(QLabel("全局主人 QQ / OpenID (逗号分隔):"))
        self.edit_master_id = QLineEdit()
        self.edit_master_id.setPlaceholderText("填入主人的 QQ 号（可在右侧监控直接右键设置）")
        row_master.addWidget(self.edit_master_id, 1)

        row_master.addWidget(QLabel("全局主人称谓:"))
        self.edit_master_title = QLineEdit()
        self.edit_master_title.setPlaceholderText("如: 主人 / 哥哥 / 司令")
        row_master.addWidget(self.edit_master_title)
        bot_layout.addLayout(row_master)

        row_flags = QHBoxLayout()
        self.chk_strip_mentions = QCheckBox("自动剔除消息开头的 @机器人 文本")
        self.chk_actions = QCheckBox("允许动作神态描写输出")
        self.chk_autonomous = QCheckBox("启用群聊自主参聊 (无需@也能机智接话)")
        self.chk_search = QCheckBox("启用联网搜梗与生僻语解析")
        self.chk_image_filter = QCheckBox("启用表情包语义智能识别")
        row_flags.addWidget(self.chk_strip_mentions)
        row_flags.addWidget(self.chk_actions)
        row_flags.addWidget(self.chk_autonomous)
        row_flags.addWidget(self.chk_search)
        row_flags.addWidget(self.chk_image_filter)
        bot_layout.addLayout(row_flags)

        wake_note = QLabel("新群消息自动唤醒分析，无需 @；AI 根据上下文自然接话、承接追问。繁忙时合并待分析消息并保留上下文。")
        wake_note.setWordWrap(True)
        bot_layout.addWidget(wake_note)
        row_awake = QHBoxLayout()
        self.chk_never_sleep = QCheckBox("永不休眠")
        row_awake.addWidget(self.chk_never_sleep)
        row_awake.addWidget(QLabel("空闲休眠 (分):"))
        self.spin_idle_sleep = QSpinBox()
        self.spin_idle_sleep.setRange(1, 1440)
        self.spin_idle_sleep.setMaximumWidth(110)
        self.spin_idle_sleep.setToolTip("暂停空闲主动话题；有新消息仍立即唤醒分析。")
        row_awake.addWidget(self.spin_idle_sleep)
        self.chk_never_sleep.toggled.connect(lambda checked: self.spin_idle_sleep.setEnabled(not checked))
        row_awake.addStretch(1)
        bot_layout.addLayout(row_awake)
        row_proactive = QHBoxLayout()
        self.chk_proactive_chat = QCheckBox("有见闻或愿望时主动聊天")
        row_proactive.addWidget(self.chk_proactive_chat)
        row_proactive.addWidget(QLabel("话题间隔 (分):"))
        self.spin_proactive_interval = QSpinBox()
        self.spin_proactive_interval.setRange(1, 1440)
        self.spin_proactive_interval.setMaximumWidth(110)
        self.spin_proactive_interval.setToolTip("仅对本次运行收到过消息的群生效；官方通道还需有效回复窗口。")
        row_proactive.addWidget(self.spin_proactive_interval)
        row_proactive.addStretch(1)
        bot_layout.addLayout(row_proactive)

        row_self = QHBoxLayout()
        self.chk_self_activity = QCheckBox("空闲时独立思考与浏览")
        self.chk_self_activity.setToolTip("持续记录兴趣与下一步，自主阅读 bot 工作区、已授权资料和公开网络；已有探索任务时沿用，成果进入事件簿与成长复盘。")
        row_self.addWidget(self.chk_self_activity)
        row_self.addWidget(QLabel("活动间隔 (分):"))
        self.spin_self_activity_interval = QSpinBox()
        self.spin_self_activity_interval.setRange(1, 1440)
        self.spin_self_activity_interval.setMaximumWidth(110)
        row_self.addWidget(self.spin_self_activity_interval)
        row_self.addStretch(1)
        bot_layout.addLayout(row_self)

        row_prob = QHBoxLayout()
        row_prob.addWidget(QLabel("单条消息分段阈值:"))
        self.spin_max_reply_len = QSpinBox()
        self.spin_max_reply_len.setRange(20, 2000)
        self.spin_max_reply_len.setMaximumWidth(110)
        self.spin_max_reply_len.setToolTip("只控制单条气泡的分段长度，不限制完整回答总字数。短句自然分条，详细回答完整发送；官方通道会按回复额度合并相邻段。")
        row_prob.addWidget(self.spin_max_reply_len)
        row_prob.addWidget(QLabel("引用与 @ 由主代理显式指定。"))
        row_prob.addStretch(1)
        bot_layout.addLayout(row_prob)

        row_mem = QHBoxLayout()
        self.chk_memory = QCheckBox("启用群聊专属记忆库 (自动概括与沉淀)")
        row_mem.addWidget(self.chk_memory)

        row_mem.addWidget(QLabel("记忆压缩阈值 (条目):"))
        self.spin_mem_max = QSpinBox()
        self.spin_mem_max.setRange(5, 100)
        row_mem.addWidget(self.spin_mem_max)

        row_mem.addWidget(QLabel("启动加载历史记忆数:"))
        self.spin_mem_hist = QSpinBox()
        self.spin_mem_hist.setRange(1, 20)
        row_mem.addWidget(self.spin_mem_hist)

        row_mem.addWidget(QLabel("上下文附带消息条数:"))
        self.spin_ctx_count = QSpinBox()
        self.spin_ctx_count.setRange(3, 50)
        row_mem.addWidget(self.spin_ctx_count)
        bot_layout.addLayout(row_mem)

        # 兼容映射别名
        self.chk_memory_enabled = self.chk_memory
        self.spin_context_count = self.spin_ctx_count
        self.spin_memory_max = self.spin_mem_max

        layout.addWidget(bot_group)

        # 6. 各群独立个性化配置表格 (群聊主人与人设隔离)
        grp_group = QGroupBox("各群独立个性化配置（群聊隔离：支持单群专属主人称谓与独立人设）")
        grp_layout = QVBoxLayout(grp_group)

        self.table_groups = QTableWidget(0, 6)
        self.table_groups.setHorizontalHeaderLabels([
            "群号 / 群OpenID", "群聊备注", "主人称谓 (专属)", "主人 QQ / ID", "主人昵称", "专属人设预设"
        ])
        self.table_groups.horizontalHeader().setStretchLastSection(True)
        grp_layout.addWidget(self.table_groups)

        grp_btn_row = QHBoxLayout()
        self.btn_add_group = QPushButton("添加群独立配置")
        self.btn_add_group.setIcon(create_vector_icon("plus", "#ffffff", 14))
        self.btn_add_group.clicked.connect(self._on_add_group_setting)

        self.btn_del_group = QPushButton("删除选中群配置")
        self.btn_del_group.setIcon(create_vector_icon("trash", "#ef4444", 14))
        self.btn_del_group.setObjectName("dangerBtn")
        self.btn_del_group.clicked.connect(self._on_del_group_setting)

        grp_btn_row.addWidget(self.btn_add_group)
        grp_btn_row.addWidget(self.btn_del_group)
        grp_btn_row.addStretch()
        grp_layout.addLayout(grp_btn_row)
        layout.addWidget(grp_group)

        # 底部保存主按钮
        self.btn_save_config = QPushButton("保存全部配置到 config.yaml")
        self.btn_save_config.setIcon(create_vector_icon("settings", "#ffffff", 16))
        self.btn_save_config.setObjectName("successBtn")
        self.btn_save_config.setFixedHeight(40)
        self.btn_save_config.clicked.connect(self.save_requested.emit)
        layout.addWidget(self.btn_save_config)

        scroll.setWidget(container)
        tab_layout.addWidget(scroll)

    def _on_theme_param_changed(self):
        op = self.slider_opacity.value() / 100.0
        self.lbl_opacity_val.setText(f"{self.slider_opacity.value()}%")
        fs = self.spin_font_size.value()
        acc = self.combo_accent.currentData() or "blue"
        self.theme_change_requested.emit(op, fs, acc)

    def load_config(self, config: AppConfig):
        """将 AppConfig 数据加载到界面中"""
        self.chk_qq_capture.setChecked(config.bot.qq_window_capture)
        for key, spin in self.runtime_numbers.items():
            spin.setValue(getattr(config.bot, key))
        self.app_config = config
        # 1. 运行模式
        idx = self.combo_run_mode.findData(getattr(config.bot, "mode", "official"))
        if idx >= 0:
            self.combo_run_mode.setCurrentIndex(idx)

        # 2. QQ 开放平台
        self.edit_qq_appid.setText(config.qq.appid)
        self.edit_qq_secret.setText(config.qq.secret)
        self.chk_sandbox.setChecked(config.qq.is_sandbox)

        # 3. OneBot 账号列表
        self.combo_ob_account.blockSignals(True)
        self.combo_ob_account.clear()
        for key, acc in config.onebot.accounts.items():
            self.combo_ob_account.addItem(f"{acc.name} ({acc.account_id or '未设QQ'})", key)
        active_key = config.onebot.active_account
        active_idx = self.combo_ob_account.findData(active_key)
        if active_idx >= 0:
            self.combo_ob_account.setCurrentIndex(active_idx)
            self._current_ob_account_key = active_key
        else:
            self._current_ob_account_key = self.combo_ob_account.currentData() or ""
        self.combo_ob_account.blockSignals(False)
        self._load_current_ob_account_fields()


        # 4. Gemini & Antigravity
        m_idx = self.combo_model.findData(config.antigravity.default_model)
        if m_idx >= 0:
            self.combo_model.setCurrentIndex(m_idx)
        self.spin_timeout.setValue(config.antigravity.timeout_seconds)
        self.spin_session_timeout.setValue(config.antigravity.session_timeout_minutes)

        # 5. Bot 行为
        self.edit_master_id.setText(config.bot.master_id)
        self.edit_master_title.setText(getattr(config.bot, "master_title", "主人"))
        self.chk_strip_mentions.setChecked(config.bot.strip_mentions)
        self.chk_actions.setChecked(config.bot.enable_action_descriptions)
        self.chk_autonomous.setChecked(config.bot.autonomous_enabled)
        self.chk_never_sleep.setChecked(config.bot.never_sleep)
        self.spin_idle_sleep.setValue(config.bot.idle_sleep_minutes)
        self.spin_idle_sleep.setEnabled(not config.bot.never_sleep)
        self.chk_proactive_chat.setChecked(config.bot.proactive_chat_enabled)
        self.spin_proactive_interval.setValue(config.bot.proactive_interval_minutes)
        self.chk_self_activity.setChecked(config.bot.self_activity_enabled)
        self.spin_self_activity_interval.setValue(config.bot.self_activity_interval_minutes)
        self.chk_search.setChecked(getattr(config.bot, "search_enabled", True))
        self.chk_image_filter.setChecked(getattr(config.bot, "image_filter_enabled", True))
        self.spin_max_reply_len.setValue(config.bot.max_reply_length)
        self.chk_memory.setChecked(config.bot.memory_enabled)
        self.spin_mem_max.setValue(config.bot.memory_max_entries)
        self.spin_mem_hist.setValue(config.bot.memory_history_count)
        self.spin_ctx_count.setValue(config.bot.context_message_count)

        # 6. 群专属配置
        self.load_group_settings()

    def _load_current_ob_account_fields(self):
        acc = self.app_config.onebot.accounts.get(self._current_ob_account_key)
        if acc:
            self.edit_ob_acc_name.setText(acc.name)
            self.edit_ob_acc_qq.setText(acc.account_id)
            self.edit_onebot_ws_url.setText(acc.ws_url)
            self.edit_onebot_token.setText(acc.access_token)

    def _save_current_ob_account_fields(self):
        curr_key = self._current_ob_account_key or self.combo_ob_account.currentData()
        if curr_key in self.app_config.onebot.accounts:
            self._current_ob_account_key = curr_key
            acc = self.app_config.onebot.accounts[curr_key]
            acc.name = self.edit_ob_acc_name.text().strip()
            acc.account_id = self.edit_ob_acc_qq.text().strip()
            acc.ws_url = self.edit_onebot_ws_url.text().strip()
            acc.access_token = self.edit_onebot_token.text().strip()

    def _on_ob_account_changed(self, idx: int):
        self._save_current_ob_account_fields()
        key = self.combo_ob_account.currentData()
        if key:
            self._current_ob_account_key = key
            self.app_config.onebot.switch_account(key)
            self._load_current_ob_account_fields()

    def _on_add_ob_account(self):
        key, ok = QInputDialog.getText(self, "新增小号", "输入新小号内部标识 (如 account_3):")
        if ok and key.strip():
            k = key.strip()
            if k in self.app_config.onebot.accounts:
                QMessageBox.warning(self, "错误", f"账号标识「{k}」已存在！")
                return
            new_acc = OneBotAccountConfig(name=f"新小号 ({k})", ws_url="ws://127.0.0.1:3003")
            self.app_config.onebot.accounts[k] = new_acc
            self.combo_ob_account.addItem(new_acc.name, k)
            self.combo_ob_account.setCurrentIndex(self.combo_ob_account.count() - 1)

    def _on_remove_ob_account(self):
        if len(self.app_config.onebot.accounts) <= 1:
            QMessageBox.warning(self, "错误", "至少需要保留一个 OneBot 账号！")
            return
        key = self.combo_ob_account.currentData()
        if key in self.app_config.onebot.accounts:
            del self.app_config.onebot.accounts[key]
            first_key = next(iter(self.app_config.onebot.accounts))
            self._current_ob_account_key = first_key
            self.app_config.onebot.switch_account(first_key)
            self.load_config(self.app_config)


    def load_group_settings(self):
        """刷新单群配置表格"""
        settings = self.app_config.bot.group_settings
        self.table_groups.setRowCount(0)
        for row, (g_id, grp) in enumerate(settings.items()):
            self.table_groups.insertRow(row)
            group_item = QTableWidgetItem(grp.group_id)
            group_item.setData(Qt.UserRole, g_id)
            self.table_groups.setItem(row, 0, group_item)
            self.table_groups.setItem(row, 1, QTableWidgetItem(grp.group_name or ""))
            title = getattr(grp, "master_title", "") or getattr(grp, "master_name", "") or "主人"
            self.table_groups.setItem(row, 2, QTableWidgetItem(title))
            self.table_groups.setItem(row, 3, QTableWidgetItem(grp.master_id or ""))
            nickname = getattr(grp, "master_nickname", "") or getattr(grp, "master_name", "") or ""
            self.table_groups.setItem(row, 4, QTableWidgetItem(nickname))
            self.table_groups.setItem(row, 5, QTableWidgetItem(grp.persona or ""))

    def rename_persona_references(self, old_name, new_name):
        for row in range(self.table_groups.rowCount()):
            item = self.table_groups.item(row, 5)
            if item is not None and item.text().strip() == old_name:
                item.setText(new_name)

    def save_group_settings(self):
        """从表格保存单群配置数据"""
        new_groups: Dict[str, GroupCustomConfig] = {}
        for row in range(self.table_groups.rowCount()):
            g_id = (self.table_groups.item(row, 0).text() if self.table_groups.item(row, 0) else "").strip()
            if not g_id:
                continue
            g_name = (self.table_groups.item(row, 1).text() if self.table_groups.item(row, 1) else "").strip()
            title = (self.table_groups.item(row, 2).text() if self.table_groups.item(row, 2) else "").strip() or "主人"
            m_id = (self.table_groups.item(row, 3).text() if self.table_groups.item(row, 3) else "").strip()
            m_nick = (self.table_groups.item(row, 4).text() if self.table_groups.item(row, 4) else "").strip()
            persona = (self.table_groups.item(row, 5).text() if self.table_groups.item(row, 5) else "").strip()

            item = self.table_groups.item(row, 0)
            original_id = item.data(Qt.UserRole) or g_id
            original = self.app_config.bot.group_settings.get(original_id)
            values = original.model_dump() if original else {}
            values.update(
                group_id=g_id,
                group_name=g_name,
                master_name=m_nick,
                master_id=m_id,
                master_nickname=m_nick,
                master_title=title,
                persona=persona
            )
            new_groups[g_id] = GroupCustomConfig(**values)
            item.setData(Qt.UserRole, g_id)
        self.app_config.bot.group_settings = new_groups
        nickname_only_groups = [g_id for g_id, grp in new_groups.items() if (grp.master_name or grp.master_nickname) and not grp.master_id]
        if nickname_only_groups:
            msg = f"群聊 [{', '.join(nickname_only_groups)}] 仅填写了主人昵称，未绑定真实主人 QQ/OpenID。\n主人鉴权仅使用平台稳定 ID（昵称可任意更改无法鉴权），这些群将回退使用全局主人 ID。建议补全真实 QQ 号。"
            logger.warning(msg)
            if self.isVisible():
                QMessageBox.warning(self, "群主人鉴权提示", msg)

    def _on_add_group_setting(self):
        row = self.table_groups.rowCount()
        self.table_groups.insertRow(row)
        self.table_groups.setItem(row, 0, QTableWidgetItem(f"1000{row+1}"))
        self.table_groups.setItem(row, 1, QTableWidgetItem("新群聊"))
        self.table_groups.setItem(row, 2, QTableWidgetItem("主人"))
        self.table_groups.setItem(row, 3, QTableWidgetItem(""))
        self.table_groups.setItem(row, 4, QTableWidgetItem(""))
        self.table_groups.setItem(row, 5, QTableWidgetItem(""))

    def _on_del_group_setting(self):
        row = self.table_groups.currentRow()
        if row >= 0:
            self.table_groups.removeRow(row)

    def _on_download_napcat(self):
        ok, msg = self.napcat_mgr.download_and_extract()
        if ok:
            QMessageBox.information(self, "下载完成", msg)
        else:
            QMessageBox.critical(self, "下载失败", msg)

    def _on_setup_napcat_accounts(self):
        mgr = self.napcat_mgr
        accounts = self.app_config.onebot.accounts
        if hasattr(mgr, "setup_dual_accounts"):
            res = mgr.setup_dual_accounts(accounts)
        elif hasattr(mgr, "setup_multi_account_configs"):
            acc_dict = {k: (v.model_dump() if hasattr(v, "model_dump") else v) for k, v in accounts.items()}
            res = mgr.setup_multi_account_configs(acc_dict)
        else:
            res = False, "管理器不支持账号配置接口"
        if isinstance(res, tuple):
            ok, msg = res
        else:
            ok, msg = bool(res), "NapCat 账号配置已生成" if res else "NapCat 配置文件写入失败"
        if ok:
            QMessageBox.information(self, "配置成功", msg)
        else:
            QMessageBox.critical(self, "配置失败", msg)

    def _on_launch_napcat(self):
        ok, msg = self.napcat_mgr.launch_napcat()
        if ok:
            QMessageBox.information(self, "启动成功", msg)
        else:
            QMessageBox.critical(self, "启动失败", msg)

    def _on_open_napcat_webui(self):
        res = self.napcat_mgr.open_webui()
        if isinstance(res, tuple):
            ok, msg = res
        else:
            ok, msg = bool(res), "已打开 NapCat WebUI 后台" if res else "打开 WebUI 失败"
        if not ok:
            QMessageBox.warning(self, "打开失败", msg)

