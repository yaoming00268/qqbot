# -*- coding: utf-8 -*-
"""
Modern Frosted Glass Real-Time Group Chat Message Monitor Sidebar.
Provides right-click context menu for instant Master setting & title customization,
permanent config persistence, font size adjustments, and frosted glass bubble rendering.
"""

from __future__ import annotations
from html import escape
import json
import logging
import time
from typing import Dict, List, Optional, Set

from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtGui import QAction, QClipboard, QCursor, QFont, QGuiApplication, QTextCursor, QTextBlockUserData
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QCheckBox, QTextEdit, QMenu,
    QInputDialog, QMessageBox, QGroupBox, QSlider
)

from master import default_master_manager
from ui.theme import create_vector_icon

logger = logging.getLogger("MonitorSidebar")


class MessageBlockData(QTextBlockUserData):
    """绑定在 QTextEdit 文本块上的消息元数据，实现右键确定性精准命中"""
    def __init__(self, record: dict):
        super().__init__()
        self.record = record


class FrostedChatBrowser(QTextEdit):
    """支持右键上下文菜单解析消息块与发送者信息的文本展示控件"""

    def __init__(self, owner_sidebar: "GroupChatMonitorWidget", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.owner = owner_sidebar
        self.setReadOnly(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos: QPoint):
        cursor = self.cursorForPosition(pos)
        block = cursor.block()
        u_data = block.userData() if block.isValid() else None
        record = u_data.record if isinstance(u_data, MessageBlockData) else self.owner.get_record_for_block(cursor.blockNumber())
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #1a1e29; color: #f8fafc; border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 6px; padding: 4px; }"
            "QMenu::item { padding: 6px 20px; border-radius: 4px; }"
            "QMenu::item:selected { background-color: rgba(59, 130, 246, 0.2); color: #60a5fa; }"
            "QMenu::separator { height: 1px; background: rgba(255, 255, 255, 0.08); margin: 4px 8px; }"
        )

        if record and record.get("type") == "msg":
            sender = record.get("sender", "群友")
            user_id = record.get("user_id", "")
            group_id = record.get("group_id", "")
            content = record.get("content", "")

            # 标题头
            hdr_act = QAction(f"用户: {sender} (ID: {user_id or '未知'})", self)
            hdr_act.setEnabled(False)
            menu.addAction(hdr_act)
            menu.addSeparator()

            # 判定当前是否已为主人的状态
            is_master = default_master_manager.is_master(group_id, user_id, sender)
            master_rec = default_master_manager.get_master_record(group_id, user_id, sender)

            if not is_master:
                act_set_group_master = QAction(create_vector_icon("crown", "#eab308", 16), "设为本群主人 (自定义称谓)...", self)
                act_set_group_master.triggered.connect(lambda: self.owner.prompt_set_master(record, is_global=False))
                menu.addAction(act_set_group_master)

                act_set_global_master = QAction(create_vector_icon("star_filled", "#f59e0b", 16), "设为全局主人 (自定义称谓)...", self)
                act_set_global_master.triggered.connect(lambda: self.owner.prompt_set_master(record, is_global=True))
                menu.addAction(act_set_global_master)
            else:
                curr_title = master_rec.title if master_rec else default_master_manager.get_master_title(group_id, user_id)
                tag_label = f"已是主人 (专属称谓: {curr_title})"
                status_act = QAction(create_vector_icon("crown", "#eab308", 16), tag_label, self)
                status_act.setEnabled(False)
                menu.addAction(status_act)

                act_edit_title = QAction(create_vector_icon("settings", "#38bdf8", 16), "修改主人专属称谓...", self)
                act_edit_title.triggered.connect(lambda: self.owner.prompt_edit_master_title(record, master_rec))
                menu.addAction(act_edit_title)

                act_remove_master = QAction(create_vector_icon("trash", "#ef4444", 16), "取消/删除主人身份", self)
                act_remove_master.triggered.connect(lambda: self.owner.confirm_remove_master(record, master_rec))
                menu.addAction(act_remove_master)

            menu.addSeparator()

            if user_id:
                act_copy_id = QAction("复制 QQ 号", self)
                act_copy_id.triggered.connect(lambda: QGuiApplication.clipboard().setText(user_id))
                menu.addAction(act_copy_id)

            if sender:
                act_copy_name = QAction("复制用户昵称", self)
                act_copy_name.triggered.connect(lambda: QGuiApplication.clipboard().setText(sender))
                menu.addAction(act_copy_name)

            if content:
                act_copy_msg = QAction("复制发言文本", self)
                act_copy_msg.triggered.connect(lambda: QGuiApplication.clipboard().setText(content))
                menu.addAction(act_copy_msg)

            if group_id:
                act_filter_grp = QAction(f"仅查看群 {group_id} 监控", self)
                act_filter_grp.triggered.connect(lambda: self.owner.filter_by_group(group_id))
                menu.addAction(act_filter_grp)

            menu.addSeparator()

        # 通用操作
        act_clear = QAction("清空监控记录", self)
        act_clear.triggered.connect(self.owner.clear_feed)
        menu.addAction(act_clear)

        menu.exec(self.mapToGlobal(pos))


class GroupChatMonitorWidget(QWidget):
    """
    现代化毛玻璃群聊消息实时监控侧边栏面板。
    提供：
    1. 实时展示指定群或全量群的群友发言、表情图片感知状态以及 AI 参聊与技能调用；
    2. 支持在消息流中直接右键群友名字设为主人、定制专属称谓并永久落盘；
    3. 组件可调节项：字体大小无级缩放、自动滚动、快捷群锁定与一键清空；
    4. 采用现代无 AI 配色与无 Emoji 的毛玻璃微质感气泡。
    """
    master_updated_signal = Signal(str, str, str)  # group_id, user_id, title

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._all_messages: List[dict] = []
        self._monitored_groups: Set[str] = set()
        self._current_filter_group: str = ""
        self._font_size: int = 12
        self._block_to_record_map: Dict[int, dict] = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # 头部监控标题与控制栏卡片
        header_box = QGroupBox("侧边栏: 群聊消息实时监控")
        h_layout = QVBoxLayout(header_box)
        h_layout.setContentsMargins(8, 12, 8, 8)
        h_layout.setSpacing(6)

        # 第一行: 群筛选下拉框 + 清空按钮
        row_filter = QHBoxLayout()
        lbl_monitor = QLabel("监控群:")
        lbl_monitor.setStyleSheet("color: #94a3b8; font-weight: 500;")
        row_filter.addWidget(lbl_monitor)

        self.combo_target_group = QComboBox()
        self.combo_target_group.addItem("全部活跃群聊 (All Groups)", "")
        self.combo_target_group.currentIndexChanged.connect(self._on_group_filter_changed)
        row_filter.addWidget(self.combo_target_group, 1)

        self.btn_clear_feed = QPushButton("清空")
        self.btn_clear_feed.setIcon(create_vector_icon("clear", "#ffffff", 14))
        self.btn_clear_feed.setFixedWidth(64)
        self.btn_clear_feed.clicked.connect(self.clear_feed)
        row_filter.addWidget(self.btn_clear_feed)
        h_layout.addLayout(row_filter)

        # 第二行: 快捷锁定输入框 + 自动滚动 + 字体调节项
        row_controls = QHBoxLayout()
        lbl_lock = QLabel("锁定群号:")
        lbl_lock.setStyleSheet("color: #94a3b8; font-weight: 500;")
        row_controls.addWidget(lbl_lock)

        self.edit_custom_group = QLineEdit()
        self.edit_custom_group.setPlaceholderText("输入群号直接锁定")
        self.edit_custom_group.textChanged.connect(self._on_custom_group_input)
        row_controls.addWidget(self.edit_custom_group, 1)

        self.chk_autoscroll = QCheckBox("自动滚动")
        self.chk_autoscroll.setChecked(True)
        row_controls.addWidget(self.chk_autoscroll)

        # 字体调节按钮 (组件可调节项)
        btn_font_dec = QPushButton("A-")
        btn_font_dec.setFixedWidth(28)
        btn_font_dec.setToolTip("缩小监控字体")
        btn_font_dec.clicked.connect(self._decrease_font_size)
        row_controls.addWidget(btn_font_dec)

        btn_font_inc = QPushButton("A+")
        btn_font_inc.setFixedWidth(28)
        btn_font_inc.setToolTip("放大监控字体")
        btn_font_inc.clicked.connect(self._increase_font_size)
        row_controls.addWidget(btn_font_inc)

        h_layout.addLayout(row_controls)
        layout.addWidget(header_box)

        # 实时消息流展示框 (基于毛玻璃半透底色)
        self.feed_browser = FrostedChatBrowser(owner_sidebar=self, parent=self)
        self.feed_browser.document().setMaximumBlockCount(2000)
        self._apply_browser_style()
        layout.addWidget(self.feed_browser, 1)

        # 底部状态指示条
        self.lbl_stats = QLabel("监控就绪 - 暂无实时群消息")
        self.lbl_stats.setStyleSheet("color: #64748b; font-size: 11px;")
        layout.addWidget(self.lbl_stats)

    def _apply_browser_style(self):
        self.feed_browser.setStyleSheet(
            f"background-color: rgba(14, 17, 24, 0.88); color: #f8fafc; "
            f"border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px; "
            f"padding: 8px; font-size: {self._font_size}px;"
        )

    def _decrease_font_size(self):
        if self._font_size > 10:
            self._font_size -= 1
            self._apply_browser_style()
            self._refresh_all_display()

    def _increase_font_size(self):
        if self._font_size < 18:
            self._font_size += 1
            self._apply_browser_style()
            self._refresh_all_display()

    def filter_by_group(self, group_id: str):
        self._current_filter_group = group_id
        self.edit_custom_group.setText(group_id)
        self._refresh_all_display()

    def get_record_for_block(self, block_num: int) -> Optional[dict]:
        """依据文本行号获取对应的原始消息记录"""
        blk = self.feed_browser.document().findBlockByNumber(block_num)
        if blk.isValid() and blk.userData():
            u = blk.userData()
            if hasattr(u, "record"):
                return u.record
        if block_num in self._block_to_record_map:
            return self._block_to_record_map[block_num]
        candidates = [k for k in self._block_to_record_map.keys() if k <= block_num]
        if candidates:
            return self._block_to_record_map[max(candidates)]
        if self._all_messages:
            return self._all_messages[-1]
        return None

    def prompt_set_master(self, record: dict, is_global: bool = False):
        """弹出对话框设置主人专属称谓并永久落盘"""
        sender = record.get("sender", "群友")
        user_id = record.get("user_id", "")
        group_id = record.get("group_id", "")

        scope_desc = "全局主人" if is_global else f"群 {group_id} 专属主人"
        title, ok = QInputDialog.getText(
            self,
            "设置主人与专属称谓",
            f"将 [{sender}] (QQ: {user_id}) 设为【{scope_desc}】。\n"
            f"请输入对主人的专属称谓 (如: 哥哥 / 主人 / 司令 / 亲爱的):",
            text="主人"
        )
        if not ok or not title.strip():
            return

        clean_title = title.strip()
        success = default_master_manager.set_master(
            user_id=user_id,
            nickname=sender,
            title=clean_title,
            group_id="" if is_global else group_id,
            is_global=is_global,
            persist=True
        )

        if success:
            QMessageBox.information(
                self,
                "主人设置成功",
                f"已成功将 [{sender}] 设为【{scope_desc}】！\n专属称谓:「{clean_title}」\n配置已永久保存至 config.yaml。"
            )
            self._refresh_all_display()
            self.master_updated_signal.emit(group_id, user_id, clean_title)
        else:
            QMessageBox.warning(self, "错误", "保存主人设置失败，请检查配置文件权限。")

    def prompt_edit_master_title(self, record: dict, master_rec: Optional[Any]):
        sender = record.get("sender", "群友")
        user_id = record.get("user_id", "")
        group_id = record.get("group_id", "")
        old_title = master_rec.title if master_rec else default_master_manager.get_master_title(group_id, user_id)
        is_global = master_rec.is_global if master_rec else False

        title, ok = QInputDialog.getText(
            self,
            "修改主人称谓",
            f"修改主人 [{sender}] 的专属称谓：",
            text=old_title
        )
        if not ok or not title.strip():
            return

        clean_title = title.strip()
        success = default_master_manager.set_master(
            user_id=user_id,
            nickname=sender,
            title=clean_title,
            group_id="" if is_global else group_id,
            is_global=is_global,
            persist=True
        )
        if success:
            QMessageBox.information(self, "更新成功", f"主人称谓已更新为:「{clean_title}」，已永久保存。")
            self._refresh_all_display()
            self.master_updated_signal.emit(group_id, user_id, clean_title)

    def confirm_remove_master(self, record: dict, master_rec: Optional[Any]):
        sender = record.get("sender", "群友")
        user_id = record.get("user_id", "")
        group_id = record.get("group_id", "")
        is_global = master_rec.is_global if master_rec else False

        reply = QMessageBox.question(
            self,
            "确认移除主人特权",
            f"确定要取消 [{sender}] (ID: {user_id}) 的主人身份吗？\n取消后将不再享有 100% 优先回复特权。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            success = default_master_manager.remove_master(
                user_id=user_id,
                group_id="" if is_global else group_id,
                is_global=is_global,
                persist=True
            )
            if success:
                QMessageBox.information(self, "已移除", f"已成功移除 [{sender}] 的主人身份，配置已永久生效。")
                self._refresh_all_display()
                self.master_updated_signal.emit(group_id, user_id, "")

    def handle_feed_event(self, event_type: str, data: dict):
        """处理来自主进程的实时广播事件"""
        if event_type == "msg":
            self.add_incoming_message(
                group_id=str(data.get("group_id", "")),
                sender_name=str(data.get("sender", "")),
                user_id=str(data.get("user_id", "")),
                content=str(data.get("content", "")),
                message_id=str(data.get("message_id", "")),
                is_at_bot=bool(data.get("is_at_bot", False)),
                time_str=str(data.get("time", ""))
            )
        elif event_type == "action":
            self.add_bot_action(
                group_id=str(data.get("group_id", "")),
                action=str(data.get("action", "")),
                reply_text=str(data.get("reply", "")),
                target_user_id=str(data.get("target", "")),
                reason=str(data.get("reason", "")),
                time_str=str(data.get("time", "")),
                source_message_id=str(data.get("source_message_id", "")),
                source_text=str(data.get("source_text", "")), quoted=bool(data.get("quoted", False)),
                segment_index=int(data.get("segment_index", 1)), segment_count=int(data.get("segment_count", 1))
            )

    def add_incoming_message(
        self,
        group_id: str,
        sender_name: str,
        user_id: str,
        content: str,
        message_id: str = "",
        is_at_bot: bool = False,
        time_str: str = ""
    ):
        t = time_str or time.strftime("%H:%M:%S")
        record = {
            "type": "msg",
            "time": t,
            "group_id": group_id,
            "sender": sender_name,
            "user_id": user_id,
            "content": content,
            "is_at_bot": is_at_bot,
            "message_id": message_id
        }
        # Receipt and recognition are updates to one source message. Preserve
        # arrival order/time even when independent image branches finish later.
        if message_id:
            existing = next((item for item in reversed(self._all_messages) if item.get("type") == "msg"
                and item.get("group_id") == group_id and item.get("message_id") == message_id), None)
            if existing is not None:
                record["time"] = existing["time"]
                existing.update(record)
                self._refresh_all_display()
                return
        self._all_messages.append(record)
        if len(self._all_messages) > 1000:
            self._all_messages = self._all_messages[-800:]

        if group_id and group_id not in self._monitored_groups:
            self._monitored_groups.add(group_id)
            self.combo_target_group.addItem(f"群 {group_id}", group_id)

        self._render_message(record)

    def add_bot_action(
        self,
        group_id: str,
        action: str,
        reply_text: str,
        target_user_id: str = "",
        reason: str = "",
        time_str: str = "", *, source_message_id: str = "", source_text: str = "", quoted: bool = False,
        segment_index: int = 1, segment_count: int = 1
    ):
        t = time_str or time.strftime("%H:%M:%S")
        record = {
            "type": "action",
            "time": t,
            "group_id": group_id,
            "action": action,
            "reply": reply_text,
            "target": target_user_id,
            "reason": reason, "source_message_id": source_message_id, "source_text": source_text,
            "quoted": quoted, "segment_index": segment_index, "segment_count": segment_count
        }
        self._all_messages.append(record)
        if len(self._all_messages) > 1000:
            self._all_messages = self._all_messages[-800:]
        self._render_message(record)

    def _render_message(self, record: dict):
        filter_grp = self._current_filter_group.strip()
        if filter_grp and record.get("group_id") != filter_grp:
            return

        time_str = record.get("time", "")
        group_id = record.get("group_id", "")
        r_type = record.get("type")

        curr_blocks = self.feed_browser.document().blockCount()
        self._block_to_record_map[curr_blocks] = record

        html = ""
        if r_type == "msg":
            sender = record.get("sender", "群友")
            user_id = record.get("user_id", "")
            content = escape(record.get("content", "")).replace("\n", "<br/>")

            # 主人徽章渲染
            is_master = default_master_manager.is_master(group_id, user_id, sender)
            master_badge = ""
            if is_master:
                title = default_master_manager.get_master_title(group_id, user_id)
                tag_name = f"主人: {title}" if title and title != "主人" else "主人"
                master_badge = (
                    f"<span style='background: rgba(234, 179, 8, 0.16); color: #facc15; "
                    f"border: 1px solid rgba(234, 179, 8, 0.4); padding: 1px 5px; "
                    f"border-radius: 4px; font-size: 10px; margin-left: 5px; font-weight: 600;'>"
                    f"[{escape(tag_name)}]</span> "
                )

            at_badge = (
                "<span style='background: rgba(59, 130, 246, 0.15); color: #60a5fa; "
                "border: 1px solid rgba(59, 130, 246, 0.35); padding: 1px 4px; "
                "border-radius: 3px; font-size: 10px; font-weight: bold;'>[@提及]</span> "
                if record.get("is_at_bot") else ""
            )

            html = (
                f"<div style='margin-bottom: 7px; padding: 7px 10px; background-color: rgba(255, 255, 255, 0.03); "
                f"border: 1px solid rgba(255, 255, 255, 0.06); border-left: 3px solid #3b82f6; border-radius: 6px;'>"
                f"<span style='color: #64748b; font-size: 10px;'>[{time_str}] 群 {group_id}</span><br/>"
                f"<strong style='color: #93c5fd;'>{escape(sender)}</strong>{master_badge}: {at_badge}{content}"
                f"</div>"
            )

        elif r_type == "action":
            act = record.get("action", "none")
            reply = record.get("reply", "").replace("<", "&lt;").replace(">", "&gt;")
            reason = record.get("reason", "")
            if act == "pass":
                html = (
                    f"<div style='margin-bottom: 5px; padding: 4px 8px; background-color: rgba(255, 255, 255, 0.015); "
                    f"border: 1px solid rgba(255, 255, 255, 0.04); border-left: 2px solid #64748b; "
                    f"border-radius: 4px; font-size: 11px; color: #94a3b8;'>"
                    f"<span>[{time_str}] [AI评估不参与 - {reason}]</span>"
                    f"</div>"
                )
            else:
                act_desc = {"at": "定向回复", "quote": "引用回复", "both": "引用并@", "skill": "技能调用", "none": "全群发言"}.get(act, "发言")
                source = ""
                if record.get("source_message_id"):
                    source_id = escape(str(record["source_message_id"]))
                    preview = escape(str(record.get("source_text") or "")[:80])
                    source = f"<div style='color: #94a3b8; font-size: 10px;'>回复消息 {source_id}：{preview}</div>"
                html = (
                    f"<div style='margin-bottom: 7px; padding: 7px 10px; background-color: rgba(16, 185, 129, 0.06); "
                    f"border: 1px solid rgba(16, 185, 129, 0.2); border-left: 3px solid #10b981; border-radius: 6px;'>"
                    f"<span style='color: #6ee7b7; font-size: 10px; font-weight: 500;'>[{time_str}] [AI回复 ({act_desc})]:</span><br/>"
                    f"{source}"
                    f"<span style='color: #ecfdf5;'>{reply}</span>"
                    f"</div>"
                )

        if html:
            cursor = self.feed_browser.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            if self.feed_browser.document().characterCount() > 1:
                cursor.insertBlock()
            start_b = cursor.blockNumber()
            cursor.insertHtml(html)
            end_b = self.feed_browser.document().blockCount()

            for b in range(start_b, end_b):
                blk = self.feed_browser.document().findBlockByNumber(b)
                if blk.isValid():
                    # QTextBlock owns its user data. Sharing one C++ pointer
                    # between blocks causes a double free on clear/destruction.
                    blk.setUserData(MessageBlockData(record))
                    self._block_to_record_map[b] = record

            if self.chk_autoscroll.isChecked():
                cursor = self.feed_browser.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.End)
                self.feed_browser.setTextCursor(cursor)

        self.lbl_stats.setText(f"总计接收 {len(self._all_messages)} 条事件 | 活跃群数: {len(self._monitored_groups)}")

    def _on_group_filter_changed(self, idx: int):
        val = self.combo_target_group.currentData()
        self._current_filter_group = str(val or "")
        self.edit_custom_group.blockSignals(True)
        self.edit_custom_group.clear()
        self.edit_custom_group.blockSignals(False)
        self._refresh_all_display()

    def _on_custom_group_input(self, text: str):
        t = text.strip()
        if t:
            self._current_filter_group = t
        else:
            self._current_filter_group = str(self.combo_target_group.currentData() or "")
        self._refresh_all_display()

    def _refresh_all_display(self):
        self.feed_browser.clear()
        self._block_to_record_map.clear()
        for msg in self._all_messages:
            self._render_message(msg)

    def clear_feed(self):
        self._all_messages.clear()
        self._block_to_record_map.clear()
        self.feed_browser.clear()
        self.lbl_stats.setText("监控已清空")
