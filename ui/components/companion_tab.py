"""Tagged sticker library and supervised game learning controls."""
from __future__ import annotations

import json
from pathlib import Path
import time

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QCheckBox, QLabel,
    QLineEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QFileDialog, QMessageBox,
    QPlainTextEdit, QScrollArea, QHeaderView, QComboBox)

from config import StickerConfig, SandboxConfig


class CompanionTabWidget(QWidget):
    save_requested = Signal()

    def __init__(self, app_config, parent=None):
        super().__init__(parent)
        self.app_config = app_config
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setStyleSheet("QScrollArea { border: none; background: #0c0e14; }")
        scroll.setWidgetResizable(True)
        panel = QWidget()
        panel.setObjectName("centralWidget")
        layout = QVBoxLayout(panel)
        group = QGroupBox("表情包：按标签与聊天语境选择")
        rows = QVBoxLayout(group)
        self.stickers_enabled = QCheckBox("启用表情包")
        self.stickers_enabled.setChecked(app_config.stickers.enabled)
        rows.addWidget(self.stickers_enabled)
        self.sticker_directory = QLineEdit(app_config.stickers.directory)
        rows.addWidget(QLabel("表情包库目录"))
        rows.addWidget(self.sticker_directory)
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("导入标签，用逗号分隔，例如：开心,害羞,安慰")
        rows.addWidget(self.tags)
        self.description = QLineEdit()
        self.description.setPlaceholderText("表情含义、适用语境（可选）")
        rows.addWidget(self.description)
        buttons = QHBoxLayout()
        for label, callback in (("导入表情包", self.import_stickers), ("刷新", self.refresh), ("移除选中项", self.remove_sticker)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        rows.addLayout(buttons)
        self.stickers = QTableWidget(0, 3)
        self.stickers.setHorizontalHeaderLabels(["ID", "标签", "语境说明"])
        self.stickers.horizontalHeader().setStyleSheet("QHeaderView::section { background: #262b38; color: #cbd5e1; padding: 6px; border: none; }")
        self.stickers.verticalHeader().hide()
        self.stickers.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.stickers.setMinimumHeight(150)
        self.stickers.setEditTriggers(QTableWidget.NoEditTriggers)
        rows.addWidget(self.stickers)
        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 3600)
        self.cooldown.setValue(app_config.stickers.cooldown_seconds)
        self.cooldown.setSuffix(" 秒：同群表情包间隔")
        rows.addWidget(self.cooldown)
        layout.addWidget(group)
        group = QGroupBox("软件使用、游戏游玩与自主学习")
        rows = QVBoxLayout(group)
        note = QLabel('主人可直接在 QQ 说：玩一下 "E:\\gal2\\Template!!\\Tenpure.exe"，记录剧情和感受。\n'
            "默认在独立后台桌面启动游戏，截图和键鼠消息只发给游戏窗口，不切换当前桌面或移动系统光标。\n"
            "普通对白可本地逐句读取并汇总，遇选项、场景变化或识别失败再看图；也可选择已配置的 QEMU 虚拟机。\n"
            "游戏截图、剧情、个人感受和续玩进度独立保存，不会自动改写人设或群友记忆。")
        note.setText(note.text() + "\n也可提供浏览器或软件路径，让 bot 自己刷视频、看帖子、阅读资料。Chrome/Edge 使用独立登录目录，账号和登录态由主人提供。")
        note.setWordWrap(True)
        rows.addWidget(note)
        self.sandbox_enabled = QCheckBox("启用主人软件、游戏与资料操作接口")
        self.sandbox_enabled.setChecked(app_config.sandbox.enabled)
        rows.addWidget(self.sandbox_enabled)
        self.mode = QComboBox()
        self.mode.addItem("后台窗口（不占用当前桌面）", "background")
        self.mode.addItem("QEMU 虚拟机", "qemu")
        self.mode.setCurrentIndex(self.mode.findData(app_config.sandbox.mode))
        rows.addWidget(QLabel("运行方式（通用软件使用后台窗口；QEMU 保留游戏流程）"))
        rows.addWidget(self.mode)
        self.startup_timeout = QSpinBox()
        self.startup_timeout.setRange(1, 300)
        self.startup_timeout.setSuffix(" 秒")
        self.startup_timeout.setValue(app_config.sandbox.startup_timeout_seconds)
        rows.addWidget(QLabel("启动取图等待（内容窗口和首张有效截图，不限制后续任务时长）"))
        rows.addWidget(self.startup_timeout)
        self.autonomous = QCheckBox("开启软件自主使用与续玩调度（仅主人已开启 autoplay 的项目）")
        self.autonomous.setChecked(app_config.sandbox.autonomous_enabled)
        rows.addWidget(self.autonomous)
        self.sandbox_directory = QLineEdit(app_config.sandbox.directory)
        rows.addWidget(QLabel("软件/游戏观察、任务与学习记录目录"))
        rows.addWidget(self.sandbox_directory)
        duration_note = QLabel("后台任务持续时间：不限时；阶段性回复后继续执行，完成明确目标或主人停止时结束。")
        duration_note.setWordWrap(True)
        rows.addWidget(duration_note)
        self.actions = QSpinBox()
        self.actions.setRange(0, 2147483647)
        self.actions.setSpecialValueText("不限次数")
        self.actions.setValue(app_config.sandbox.max_actions)
        self.actions.setSuffix(" 次：软件/游戏操作上限")
        rows.addWidget(QLabel("操作次数上限（0 表示不限次数）"))
        rows.addWidget(self.actions)
        reading = app_config.sandbox.galgame_reading
        self.game_reading = QCheckBox("galgame 优先本地阅读对白，按需返回画面")
        self.game_reading.setChecked(reading.enabled)
        rows.addWidget(self.game_reading)
        self.reading_batch = QSpinBox()
        self.reading_batch.setRange(1, 20)
        self.reading_batch.setValue(reading.batch_lines)
        self.reading_batch.setSuffix(" 次：每批最多逐句推进（选项与异常提前停）")
        rows.addWidget(self.reading_batch)
        self.dialogue_top = QSpinBox()
        self.dialogue_top.setRange(35, 90)
        self.dialogue_top.setValue(round(reading.dialogue_top * 100))
        self.dialogue_top.setSuffix(" %：对白区域上边界（从窗口顶部计算）")
        self.dialogue_top.setToolTip("用于 OCR；对白框位置特殊时调整。区域外检测到文字会先停下看图。")
        rows.addWidget(self.dialogue_top)
        self.reading_settle = QSpinBox()
        self.reading_settle.setRange(200, 2000)
        self.reading_settle.setSingleStep(100)
        self.reading_settle.setValue(reading.settle_ms)
        self.reading_settle.setSuffix(" 毫秒：对白稳定等待")
        rows.addWidget(self.reading_settle)
        self.interval = QSpinBox()
        self.interval.setRange(1, 1440)
        self.interval.setValue(app_config.sandbox.interval_minutes)
        self.interval.setSuffix(" 分钟：已结束任务的自主续用间隔")
        self.interval.setToolTip("仅控制已结束任务何时再次自主启动，不限制正在运行的任务时长。")
        rows.addWidget(self.interval)
        self.profiles = QPlainTextEdit(json.dumps(app_config.sandbox.profiles, ensure_ascii=False, indent=2))
        self.profiles.setPlaceholderText('{"游戏资料": "E:/资料/游戏"}')
        self.profiles.setMaximumHeight(85)
        rows.addWidget(QLabel("资料目录别名（JSON，只读；填写专用目录，不填密码）"))
        rows.addWidget(self.profiles)
        self.browser_profiles = QPlainTextEdit(json.dumps(app_config.sandbox.browser_profiles, ensure_ascii=False, indent=2))
        self.browser_profiles.setPlaceholderText('{"视频账号": "E:/bot-browser/video-profile"}')
        self.browser_profiles.setMaximumHeight(85)
        rows.addWidget(QLabel("浏览器专用登录目录（JSON；本地保存登录态，不填密码；省略时自动创建）"))
        rows.addWidget(self.browser_profiles)
        self.browser_executable = QLineEdit(app_config.sandbox.browser_executable)
        self.browser_executable.setPlaceholderText("留空自动查找 Chrome/Edge；也可填写专用浏览器完整 exe 路径")
        rows.addWidget(QLabel("默认浏览器（主人只给网址时使用）"))
        rows.addWidget(self.browser_executable)
        self.learning = QPlainTextEdit()
        self.learning.setReadOnly(True)
        self.learning.setMinimumHeight(160)
        rows.addWidget(self.learning)
        refresh = QPushButton("查看软件使用、游玩与学习记录")
        refresh.clicked.connect(self.refresh_learning)
        rows.addWidget(refresh)
        self.recovery_session = QComboBox()
        rows.addWidget(QLabel("恢复后台任务（先刷新记录，再选择任务）"))
        rows.addWidget(self.recovery_session)
        self.recovery_url = QLineEdit()
        self.recovery_url.setPlaceholderText("可选：跳过受阻页面后使用的其他入口 https://...")
        rows.addWidget(self.recovery_url)
        recovery_buttons = QHBoxLayout()
        for text, action in (("跳过当前内容", "skip"), ("重启执行分支", "restart")):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, operation=action: self.recover_activity(operation))
            recovery_buttons.addWidget(button)
        rows.addLayout(recovery_buttons)
        self.recovery_result = QLabel("保留软件窗口和登录态；实际恢复状态以刷新后的回执为准。")
        self.recovery_result.setWordWrap(True)
        rows.addWidget(self.recovery_result)
        layout.addWidget(group)
        save = QPushButton("保存表情包与学习配置")
        save.clicked.connect(self.save_requested.emit)
        layout.addWidget(save)
        layout.addStretch()
        scroll.setWidget(panel)
        outer.addWidget(scroll)
        if (Path(self.sticker_directory.text()) / "library.sqlite3").is_file():
            self.refresh()

    def load_config(self, app_config):
        self.app_config = app_config
        self.stickers_enabled.setChecked(app_config.stickers.enabled)
        self.sticker_directory.setText(app_config.stickers.directory)
        self.cooldown.setValue(app_config.stickers.cooldown_seconds)
        self.sandbox_enabled.setChecked(app_config.sandbox.enabled)
        self.mode.setCurrentIndex(self.mode.findData(app_config.sandbox.mode))
        self.autonomous.setChecked(app_config.sandbox.autonomous_enabled)
        self.startup_timeout.setValue(app_config.sandbox.startup_timeout_seconds)
        self.sandbox_directory.setText(app_config.sandbox.directory)
        self.actions.setValue(app_config.sandbox.max_actions)
        self.game_reading.setChecked(app_config.sandbox.galgame_reading.enabled)
        self.reading_batch.setValue(app_config.sandbox.galgame_reading.batch_lines)
        self.dialogue_top.setValue(round(app_config.sandbox.galgame_reading.dialogue_top * 100))
        self.reading_settle.setValue(app_config.sandbox.galgame_reading.settle_ms)
        self.interval.setValue(app_config.sandbox.interval_minutes)
        self.profiles.setPlainText(json.dumps(app_config.sandbox.profiles, ensure_ascii=False, indent=2))
        self.browser_profiles.setPlainText(json.dumps(app_config.sandbox.browser_profiles, ensure_ascii=False, indent=2))
        self.browser_executable.setText(app_config.sandbox.browser_executable)
        self.stickers.setRowCount(0)
        if (Path(self.sticker_directory.text()) / "library.sqlite3").is_file():
            self.refresh()

    def collect(self):
        profiles = json.loads(self.profiles.toPlainText() or "{}")
        if not isinstance(profiles, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in profiles.items()):
            raise ValueError("资料别名必须是名称到目录路径的 JSON 对象。")
        browser_profiles = json.loads(self.browser_profiles.toPlainText() or "{}")
        if not isinstance(browser_profiles, dict) or any(not isinstance(key, str) or not key.strip() or
                not isinstance(value, str) or not value.strip() for key, value in browser_profiles.items()):
            raise ValueError("浏览器登录别名必须是名称到专用目录路径的 JSON 对象。")
        return (StickerConfig(enabled=self.stickers_enabled.isChecked(), directory=self.sticker_directory.text().strip() or "data/stickers",
                    cooldown_seconds=self.cooldown.value(), expire_seconds=self.app_config.stickers.expire_seconds),
                SandboxConfig(enabled=self.sandbox_enabled.isChecked(), directory=self.sandbox_directory.text().strip() or "data/sandbox",
                    mode=self.mode.currentData(), vm=self.app_config.sandbox.vm,
                    startup_timeout_seconds=self.startup_timeout.value(),
                    max_actions=self.actions.value(), autonomous_enabled=self.autonomous.isChecked(),
                    galgame_reading=self.app_config.sandbox.galgame_reading.model_copy(update={
                        "enabled": self.game_reading.isChecked(), "batch_lines": self.reading_batch.value(),
                        "dialogue_top": self.dialogue_top.value() / 100, "settle_ms": self.reading_settle.value()}),
                    interval_minutes=self.interval.value(), profiles=profiles, browser_profiles=browser_profiles,
                    browser_executable=self.browser_executable.text().strip().strip('"')))

    def refresh(self):
        from sticker_library import StickerLibrary
        items = StickerLibrary(self.sticker_directory.text()).search("", 50)
        self.stickers.setRowCount(len(items))
        for row, item in enumerate(items):
            for column, value in enumerate((item["id"][:16] + "…", "、".join(item["tags"]), item["description"])):
                self.stickers.setItem(row, column, QTableWidgetItem(value))
            self.stickers.item(row, 0).setData(Qt.UserRole, item["id"])
            self.stickers.item(row, 0).setToolTip(item["id"])
        self.stickers.resizeColumnToContents(0)
        self.stickers.resizeColumnToContents(1)
        self.stickers.setColumnWidth(0, max(170, self.stickers.columnWidth(0)))
        self.stickers.setColumnWidth(1, max(160, self.stickers.columnWidth(1)))

    def import_stickers(self):
        from sticker_library import StickerLibrary
        paths, _ = QFileDialog.getOpenFileNames(self, "选择表情包", "", "图片 (*.png *.jpg *.jpeg *.gif *.webp)")
        if not paths:
            return
        tags = self.tags.text().replace("，", ",").replace("、", ",").split(",")
        try:
            library = StickerLibrary(self.sticker_directory.text())
            for path in paths:
                library.import_file(path, tags, self.description.text())
            self.refresh()
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "导入未完成", str(error))

    def remove_sticker(self):
        row = self.stickers.currentRow()
        if row >= 0:
            from sticker_library import StickerLibrary
            StickerLibrary(self.sticker_directory.text()).remove(self.stickers.item(row, 0).data(Qt.UserRole))
            self.refresh()

    def refresh_learning(self):
        from sandbox_runtime import SandboxRuntime
        runtime = SandboxRuntime(self.sandbox_directory.text())
        with runtime.connect() as db:
            sessions = db.execute("SELECT games.title,sessions.* FROM sessions "
                "JOIN games ON games.id=sessions.target ORDER BY sessions.created DESC LIMIT 10").fetchall()
            notes = db.execute("SELECT games.title,notes.kind,notes.text,notes.created FROM notes JOIN games ON games.id=notes.target "
                "ORDER BY notes.created DESC LIMIT 20").fetchall()
        states = {"running": "使用/游玩中", "paused": "已暂停", "completed": "已完成本轮", "failed": "未完成", "cancelled": "已停止", "interrupted": "已中断"}
        kinds = {"plot": "虚构剧情", "observation": "软件页面观察", "reflection": "主观感受", "preference": "个人偏好"}
        lines = ["最近游玩与软件使用"]
        self.recovery_session.clear()
        for row in sessions:
            if row["state"] in ("running", "paused", "failed", "interrupted"):
                self.recovery_session.addItem(f"{row['title']} · {row['state']} · {row['scope']}", dict(row))
            lines.extend([f"{row['title']} · {states.get(row['state'], row['state'])} · {row['actions']} 次操作",
                          row["summary"] or "本轮尚无总结。", ""])
        if not sessions:
            lines.append("暂无游玩或软件使用记录。")
        lines.extend(["", "页面观察、虚构剧情与主观感受"])
        for row in notes:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(row["created"]))
            lines.extend([f"{stamp} · {row['title']} · {kinds.get(row['kind'], row['kind'])}", row["text"], ""])
        if not notes:
            lines.append("暂无学习记录。")
        self.learning.setPlainText("\n".join(lines))

    def recover_activity(self, operation):
        from console_tools.broker import ConsoleBroker
        from console_tools.cli import execute
        from console_tools.files import file_error
        session = self.recovery_session.currentData()
        if not session:
            self.recovery_result.setText("请先刷新并选择要恢复的后台任务。")
            return
        path = Path(self.app_config.bot.runtime_database_path).resolve().parent / "main_chat" / "console.sqlite3"
        if not path.is_file():
            self.recovery_result.setText("当前运行目录没有对应的控制台任务记录。")
            return
        try:
            broker = ConsoleBroker(path)
            request = broker.record(session["request_id"])
            if not request:
                raise ValueError("原任务记录不存在，不能重放操作。")
            original = broker.context(request["token"], allow_expired=True)
            context = {**original, "sandbox_session_id": session["id"], "parent_request_id": request["id"],
                "activity_controller": True, "activity_target_id": session["target"], "source_text": "",
                "sandbox_config": {**self.app_config.sandbox.model_dump(), "directory": str(Path(self.sandbox_directory.text()).resolve())},
                "artifact_roots": [], "output_dir": ""}
            file = broker.issue_context(context, path.parent / "gui_controls")
            result = execute("sandbox", file, operation=operation, params={"url": self.recovery_url.text().strip()})
        except (OSError, ValueError, KeyError, TypeError) as error:
            result = file_error(error)
        self.recovery_result.setText(json.dumps(result, ensure_ascii=False))
