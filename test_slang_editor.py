"""Offline console CRUD checks against temporary Markdown files, without starting QQ."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox

from slang_glossary import SlangGlossary
from ui.components.console_tab import ConsoleTabWidget


class SlangEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot_slang_editor_")
        self.path = Path(self.temp.name) / "slang_glossary.md"
        self.glossary = SlangGlossary(self.path)
        self.console = ConsoleTabWidget(glossary=self.glossary)
        self.editor = self.console.slang_editor

    def tearDown(self):
        self.console.close()
        self.app.processEvents()
        self.temp.cleanup()

    def create(self, term="测试黑话", meaning="", **fields):
        self.editor.btn_new.click()
        self.editor.term.setText(term)
        self.editor.meaning.setPlainText(meaning)
        for field, value in fields.items():
            widget = getattr(self.editor, field)
            if hasattr(widget, "setPlainText"):
                widget.setPlainText(value)
            else:
                widget.setText(value)
        self.editor.btn_save.click()
        return self.glossary.entries()[-1]

    def test_console_keeps_logs_and_runtime_buttons_with_discoverable_glossary_page(self):
        self.assertEqual(self.console.workspace_tabs.count(), 2)
        self.assertEqual(self.console.workspace_tabs.tabText(1), "话术表")
        self.assertTrue(self.console.btn_start.isEnabled())
        self.console.set_running_state(True)
        self.assertFalse(self.console.btn_start.isEnabled())
        self.console.append_log("这是运行中的日志")
        self.assertIn("这是运行中的日志", self.console.log_terminal.toPlainText())
        self.assertEqual(self.editor.file_path.text(), str(self.path.resolve()))
        self.assertTrue(self.editor.file_path.isReadOnly())
        self.assertFalse(self.path.exists())

    def test_large_log_batches_keep_latest_lines_and_filter_and_clear_work(self):
        count = self.console.MAX_LOG_BLOCKS
        self.console.append_log("\n".join(f"line-{index}" for index in range(count + 100)))
        terminal = self.console.log_terminal
        self.assertEqual(terminal.document().blockCount(), count)
        self.assertEqual(terminal.toPlainText().splitlines()[0], "line-100")
        self.assertTrue(terminal.toPlainText().endswith(f"line-{count + 99}"))
        self.console.btn_clear_terminal.click()
        self.assertEqual(terminal.toPlainText(), "")
        self.console.edit_filter.setText("ERROR")
        self.console.append_log("info skipped\nERROR kept\nwarning skipped")
        self.assertEqual(terminal.toPlainText(), "ERROR kept")

    def test_appending_a_log_batch_preserves_scroll_when_autoscroll_is_off(self):
        self.console.resize(800, 500)
        self.console.show()
        self.console.append_log("\n".join(str(index) for index in range(1000)))
        self.app.processEvents()
        self.console.chk_autoscroll.setChecked(False)
        scroll = self.console.log_terminal.verticalScrollBar()
        scroll.setValue(10)
        self.console.append_log("new line 1\nnew line 2")
        self.assertEqual(scroll.value(), 10)

    def test_create_empty_meaning_generates_index_and_bot_reads_same_file_immediately(self):
        reader = SlangGlossary(self.path)
        entry = self.create(aliases="别称一; 别称二", tags="#游戏 #群聊",
                            sources="https://example.com/one\nhttps://example.com/two")
        self.assertEqual(reader.pending_terms("有人说别称二"), [entry.term])
        self.assertEqual(entry.tags, ("游戏", "群聊"))
        self.assertEqual(len(entry.sources), 2)
        self.assertTrue(entry.index.startswith("slang-"))
        self.assertEqual(self.editor.table.item(0, 2).text(), "待补全")
        self.assertIn("无需重启", self.editor.status.text())
        self.editor.meaning.setPlainText("人工解释 | 内容\n第二行")
        self.editor.btn_save.click()
        self.assertFalse(reader.pending_terms("别称一"))
        self.assertIn("人工解释", reader.prompt_section("别称一"))
        self.assertEqual(reader.entries()[0].meaning, "人工解释 | 内容\n第二行")

    def test_edit_can_clear_aliases_tags_sources_and_search_reflects_current_file(self):
        entry = self.create(meaning="旧解释", aliases="旧别名", tags="旧标签", sources="https://example.com/old")
        self.editor.term.setText("改名后的词")
        self.editor.aliases.clear()
        self.editor.tags.setText("#新标签")
        self.editor.sources.clear()
        self.editor.meaning.setPlainText("改写的解释")
        self.editor.btn_save.click()
        updated = self.glossary.entries()[0]
        self.assertEqual(updated.index, entry.index)
        self.assertEqual(updated.aliases, ())
        self.assertEqual(updated.sources, ())
        for query in ("改名后的词", "#新标签", updated.index, "改写的解释"):
            self.editor.search.setText(query)
            self.assertEqual(self.editor.table.rowCount(), 1)
        for query in ("旧别名", "旧标签"):
            self.editor.search.setText(query)
            self.assertEqual(self.editor.table.rowCount(), 0)

    def test_delete_requires_button_confirmation_then_removes_match_and_tag_index(self):
        entry = self.create(meaning="解释", tags="待删除标签")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.editor.btn_delete.click()
        self.assertEqual(self.glossary.entries(), [entry])
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.editor.btn_delete.click()
        self.assertEqual(self.glossary.entries(), [])
        self.assertEqual(self.editor.table.rowCount(), 0)
        self.assertFalse(self.editor.btn_delete.isEnabled())
        self.assertFalse(self.glossary.match(entry.term))
        self.assertNotIn("待删除标签", self.path.read_text(encoding="utf-8"))

    def test_unsaved_edits_survive_search_tab_switch_and_cancelled_refresh_or_selection(self):
        first = self.glossary.record("第一条", meaning="解释一")
        second = self.glossary.record("第二条", meaning="解释二")
        self.editor.refresh()
        self.editor.meaning.setPlainText("尚未保存的草稿")
        self.editor.search.setText(second.term)
        self.assertEqual(self.editor.meaning.toPlainText(), "尚未保存的草稿")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Cancel):
            self.editor.table.selectRow(0)
            self.editor.btn_refresh.click()
            self.editor.btn_new.click()
        self.editor.refresh(automatic=True)
        self.assertEqual(self.editor.meaning.toPlainText(), "尚未保存的草稿")
        self.assertEqual(self.editor._selected_entry, first)
        self.assertEqual(self.glossary.entries(), [first, second])
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Discard):
            self.editor.btn_refresh.click()
        self.assertEqual(self.editor.meaning.toPlainText(), first.meaning)

    def test_stale_save_and_delete_report_conflict_and_preserve_manual_draft(self):
        entry = self.create()
        self.editor.meaning.setPlainText("控制台草稿")
        filled = SlangGlossary(self.path)._fill_existing(entry.term, "后台解释", ["联网"], ["https://example.com/source"])
        self.editor.btn_save.click()
        self.assertIn("更新", self.editor.status.text())
        self.assertEqual(self.editor.meaning.toPlainText(), "控制台草稿")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.editor.btn_delete.click()
        self.assertIn("更新", self.editor.status.text())
        self.assertEqual(self.glossary.entries(), [filled])

    def test_duplicate_and_empty_terms_keep_draft_and_do_not_change_file(self):
        entry = self.create(meaning="原解释")
        before = self.path.read_bytes()
        self.editor.btn_new.click()
        self.editor.btn_save.click()
        self.assertIn("非空", self.editor.status.text())
        self.editor.term.setText(entry.term)
        self.editor.meaning.setPlainText("重复项内容")
        self.editor.btn_save.click()
        self.assertIn("已有", self.editor.status.text())
        self.assertEqual(self.editor.meaning.toPlainText(), "重复项内容")
        self.assertEqual(self.path.read_bytes(), before)

    def test_external_changes_refresh_on_return_and_parse_errors_do_not_overwrite_file(self):
        self.glossary.record("外部词条", meaning="文件新增")
        self.editor.refresh(automatic=True)
        self.assertEqual(self.editor.table.rowCount(), 1)
        self.path.write_text("手改损坏的 Markdown", encoding="utf-8")
        self.editor.meaning.setPlainText("本地草稿")
        self.editor.btn_save.click()
        self.assertIn("保存失败", self.editor.status.text())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "手改损坏的 Markdown")
        self.assertEqual(self.editor.meaning.toPlainText(), "本地草稿")

    def test_main_console_uses_runtime_glossary_and_close_keeps_unsaved_input_on_cancel(self):
        from ui.main_window import BotStudioGUI
        from PySide6.QtGui import QCloseEvent
        previous = Path.cwd()
        window = None
        os.chdir(self.temp.name)
        try:
            with patch("slang_glossary.default_slang_glossary", self.glossary):
                window = BotStudioGUI("config.yaml")
            editor = window.console_tab_widget.slang_editor
            self.assertIs(editor.glossary, self.glossary)
            editor.term.setText("尚未保存的词")
            event = QCloseEvent()
            with patch.object(QMessageBox, "question", return_value=QMessageBox.Cancel):
                window.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertEqual(editor.term.text(), "尚未保存的词")
            with patch.object(QMessageBox, "question", return_value=QMessageBox.Discard):
                window.closeEvent(event)
            self.assertTrue(event.isAccepted())
            self.assertFalse(self.path.exists())
        finally:
            if window:
                with patch.object(QMessageBox, "question", return_value=QMessageBox.Discard):
                    window.close()
            os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
