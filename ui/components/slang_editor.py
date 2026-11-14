"""Form editor for the same fixed-path Markdown glossary read by the bot."""
from __future__ import annotations

import unicodedata

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import slang_glossary
from ui.theme import create_vector_icon


class SlangGlossaryEditor(QWidget):
    def __init__(self, glossary=None, parent=None):
        super().__init__(parent)
        self.glossary = glossary if glossary is not None else slang_glossary.default_slang_glossary
        self._entries = []
        self._selected_entry = None
        self._dirty = False
        self._loading = False
        self._setup_ui()
        self.refresh()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        note = QLabel("仅收录已确认的黑话。解释可留空，群聊遇到后由后台补全；保存后下一轮聊天即可使用。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.file_path = QLineEdit(str(self.glossary.path))
        self.file_path.setReadOnly(True)
        self.file_path.setToolTip("话术表固定位置；控制台与 bot 使用同一份 Markdown 文件。")
        layout.addWidget(self.file_path)

        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("查找词语、别名、标签、索引或解释…")
        self.search.setClearButtonEnabled(True)
        tools.addWidget(self.search, 1)
        self.btn_new = QPushButton("新增词条")
        self.btn_new.setIcon(create_vector_icon("plus", "#ffffff", 14))
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setIcon(create_vector_icon("refresh", "#ffffff", 14))
        tools.addWidget(self.btn_new)
        tools.addWidget(self.btn_refresh)
        layout.addLayout(tools)

        splitter = QSplitter(Qt.Horizontal)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["词语或句子", "标签", "状态"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStyleSheet(
            "QHeaderView::section { background-color: #202632; color: #cbd5e1; "
            "border: 1px solid #303847; padding: 4px; }")
        splitter.addWidget(self.table)

        form_widget = QWidget()
        form_widget.setObjectName("slangForm")
        form_widget.setStyleSheet("QWidget#slangForm { background-color: #181b24; }")
        form_layout = QVBoxLayout(form_widget)
        form_layout.setContentsMargins(8, 0, 0, 0)
        self.form_title = QLabel("新增词条")
        form_layout.addWidget(self.form_title)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.term = QLineEdit()
        self.term.setPlaceholderText("已确认的黑话词语或完整句子")
        self.aliases = QLineEdit()
        self.aliases.setPlaceholderText("多个别名用分号分隔")
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("例如：#群聊 #游戏")
        self.meaning = QPlainTextEdit()
        self.meaning.setPlaceholderText("填写解释，或留空等待群聊遇到时后台检索")
        self.meaning.setMinimumHeight(90)
        self.meaning.setMaximumHeight(160)
        self.sources = QPlainTextEdit()
        self.sources.setPlaceholderText("来源 URL，每行一个或用分号分隔，可留空")
        self.sources.setFixedHeight(68)
        self.entry_index = QLabel("保存时自动生成")
        self.entry_index.setTextFormat(Qt.PlainText)
        self.entry_index.setTextInteractionFlags(Qt.TextSelectableByMouse)
        for label, field in (("词条：", self.term), ("别名：", self.aliases),
                             ("标签：", self.tags), ("解释：", self.meaning),
                             ("来源：", self.sources), ("索引：", self.entry_index)):
            form.addRow(label, field)
        form_layout.addLayout(form)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.btn_save = QPushButton("保存词条")
        self.btn_save.setObjectName("primaryBtn")
        self.btn_delete = QPushButton("删除词条")
        self.btn_delete.setObjectName("dangerBtn")
        self.btn_delete.setEnabled(False)
        actions.addWidget(self.btn_save)
        actions.addWidget(self.btn_delete)
        form_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(form_widget)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 340])
        layout.addWidget(splitter, 1)
        layout.addLayout(actions)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)

        self.search.textChanged.connect(self._render_table)
        self.table.itemSelectionChanged.connect(self._select_entry)
        self.btn_new.clicked.connect(self.new_entry)
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_save.clicked.connect(self.save_entry)
        self.btn_delete.clicked.connect(self.delete_entry)
        for field in (self.term, self.aliases, self.tags, self.meaning, self.sources):
            field.textChanged.connect(self._mark_dirty)

    def _set_status(self, text, *, error=False):
        self.status.setText(text)
        self.status.setStyleSheet("color: #fca5a5;" if error else "color: #94a3b8;")

    def _mark_dirty(self):
        if not self._loading:
            self._dirty = True
            self.form_title.setText(("编辑词条" if self._selected_entry else "新增词条") + "（未保存）")

    def _confirm_discard(self):
        if not self._dirty:
            return True
        return QMessageBox.question(self, "未保存的修改", "放弃当前未保存的词条修改？",
            QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel) == QMessageBox.Discard

    def _load_entry(self, entry):
        self._loading = True
        try:
            self._selected_entry = entry
            self.term.setText(entry.term if entry else "")
            self.aliases.setText("; ".join(entry.aliases) if entry else "")
            self.tags.setText(" ".join("#" + tag for tag in entry.tags) if entry else "")
            self.meaning.setPlainText(entry.meaning if entry else "")
            self.sources.setPlainText("\n".join(entry.sources) if entry else "")
            self.entry_index.setText(entry.index if entry else "保存时自动生成")
            self.btn_delete.setEnabled(entry is not None)
            self.form_title.setText("编辑词条" if entry else "新增词条")
            self._dirty = False
        finally:
            self._loading = False

    def _render_table(self):
        token = unicodedata.normalize("NFKC", self.search.text()).casefold().strip().lstrip("#")
        rows = [entry for entry in self._entries if not token or any(
            token in unicodedata.normalize("NFKC", value).casefold()
            for value in (entry.index, entry.term, entry.meaning, *entry.aliases, *entry.tags))]
        self.table.blockSignals(True)
        try:
            self.table.clearSelection()
            self.table.setRowCount(len(rows))
            for row, entry in enumerate(rows):
                for col, text in enumerate((entry.term, " ".join("#" + tag for tag in entry.tags),
                                             "待补全" if entry.needs_explanation else "已解释")):
                    item = QTableWidgetItem(text)
                    item.setData(Qt.UserRole, entry)
                    item.setToolTip(entry.meaning or "已确认是黑话，解释待后台补全。")
                    self.table.setItem(row, col, item)
                if self._selected_entry and entry.index == self._selected_entry.index:
                    self.table.selectRow(row)
        finally:
            self.table.blockSignals(False)

    def _select_entry(self):
        selected = self.table.selectedItems()
        if not selected:
            return
        entry = selected[0].data(Qt.UserRole)
        if self._selected_entry and entry.index == self._selected_entry.index:
            return
        if not self._confirm_discard():
            self._render_table()
            return
        self._load_entry(entry)

    def refresh(self, *, automatic=False):
        if automatic and self._dirty:
            return
        if not automatic and not self._confirm_discard():
            return
        try:
            entries = self.glossary.entries()
        except (OSError, ValueError) as error:
            self._set_status(f"话术表读取失败：{error}", error=True)
            return
        self._entries = entries
        index = self._selected_entry.index if self._selected_entry else None
        entry = next((item for item in entries if item.index == index), None)
        if entry is None and entries:
            entry = entries[0]
        self._load_entry(entry)
        self._render_table()
        pending = sum(entry.needs_explanation for entry in entries)
        self._set_status(f"共 {len(entries)} 个词条，{pending} 个待补全。")

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh(automatic=True)

    def new_entry(self):
        if not self._confirm_discard():
            return
        self._load_entry(None)
        self._render_table()
        self.term.setFocus()
        self._set_status("填写已确认的黑话，点击“保存词条”。")

    def confirm_close(self):
        if not self._confirm_discard():
            return False
        self._dirty = False
        return True

    def save_entry(self):
        previous = self._selected_entry
        try:
            entry = self.glossary.save_entry(self.term.text(),
                index=previous.index if previous else None, expected=previous,
                aliases=self.aliases.text(), tags=self.tags.text(),
                meaning=self.meaning.toPlainText(), sources=self.sources.toPlainText().replace("\n", ";"))
        except (OSError, ValueError) as error:
            self._set_status(f"保存失败：{error}", error=True)
            return
        self._dirty = False
        self._selected_entry = entry
        self.search.clear()
        self.refresh()
        self._set_status(f"已保存“{entry.term}”，下一轮聊天即可读取，无需重启 bot。")

    def delete_entry(self):
        previous = self._selected_entry
        if previous is None:
            return
        answer = QMessageBox.question(self, "删除话术表词条", f"删除“{previous.term}”及其解释、别名和标签？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            self.glossary.delete_entry(previous.index, expected=previous)
        except (OSError, ValueError) as error:
            self._set_status(f"删除失败：{error}", error=True)
            return
        self._dirty = False
        self._selected_entry = None
        self.refresh()
        self._set_status(f"已删除“{previous.term}”，词条索引已同步更新。")
