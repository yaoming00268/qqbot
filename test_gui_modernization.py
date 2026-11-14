# -*- coding: utf-8 -*-
"""
Tests for Modern GUI, Frosted Glass Styling, and Media Intent Keywords.
Verifies build_frosted_stylesheet, vector icons, font size scaling,
and media_router recognition of standing art (立绘), CG, etc.
"""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import os
import unittest
from PySide6.QtWidgets import QApplication

from ui.theme import build_frosted_stylesheet, create_vector_icon, ACCENT_COLORS
from ui.components.monitor_sidebar import GroupChatMonitorWidget
from media_router import parse_media_intent


class TestGUIModernizationAndMedia(unittest.TestCase):
    """测试现代化毛玻璃 GUI 样式、矢量图标与立绘意图识别"""

    def test_runtime_settings_roundtrip_and_media_config_is_preserved(self):
        import tempfile
        from unittest.mock import patch
        from config import AppConfig
        from ui.main_window import BotStudioGUI
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            cfg = AppConfig()
            cfg.media.local_library_roots = ["library"]
            cfg.bot.passive_backlog = 3
            cfg.bot.mention_probability = 0.37  # Compatibility field is preserved, never edited by a hidden control.
            cfg.save(path)
            gui = BotStudioGUI(path)
            self.assertFalse(hasattr(gui.config_tab_widget, "spin_mention_prob"))
            self.assertFalse(hasattr(gui.config_tab_widget, "edit_mention_prob"))
            gui.config_tab_widget.runtime_numbers["idle_recall_minutes"].setValue(12)
            gui.config_tab_widget.chk_qq_capture.setChecked(False)
            gui.media_tab_widget.media_numbers["task_timeout_seconds"].setValue(0)
            gui.emotion_tab_widget.provider.setCurrentIndex(gui.emotion_tab_widget.provider.findData("bert"))
            gui.emotion_tab_widget.bert_service_file.setText("emotion_studio/runtime/inference.json")
            gui._on_save_config(show_msg=False)
            restored = AppConfig.load(path)
            self.assertEqual(restored.bot.idle_recall_minutes, 12)
            self.assertEqual(restored.bot.mention_probability, 0.37)
            self.assertFalse(restored.bot.qq_window_capture)
            self.assertEqual(restored.media.task_timeout_seconds, 0)
            self.assertEqual(restored.media.local_library_roots, ["library"])
            self.assertEqual(restored.emotion.provider, "bert")
            self.assertEqual(restored.emotion.bert_service_file, "emotion_studio/runtime/inference.json")
            gui.close()

    def test_frosted_stylesheet_generation(self):
        qss_blue = build_frosted_stylesheet(opacity=0.85, font_size=13, accent="blue")
        self.assertIn("rgba(24, 27, 36, 0.85)", qss_blue)
        self.assertIn("13px", qss_blue)
        self.assertIn("#3b82f6", qss_blue)

        qss_emerald = build_frosted_stylesheet(opacity=0.70, font_size=15, accent="emerald")
        self.assertIn("rgba(24, 27, 36, 0.70)", qss_emerald)
        self.assertIn("15px", qss_emerald)
        self.assertIn("#10b981", qss_emerald)

    def test_vector_icons_generation(self):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        app = QApplication.instance() or QApplication([])

        for icon_name in ("bot", "chat", "settings", "crown", "user", "clear", "plus", "trash", "star_filled", "filter"):
            icon = create_vector_icon(icon_name, "#ffffff", 24)
            self.assertFalse(icon.isNull())
            pixmap = icon.pixmap(24, 24)
            self.assertEqual(pixmap.width(), 24)
            self.assertEqual(pixmap.height(), 24)

    def test_sidebar_font_size_scaling(self):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        app = QApplication.instance() or QApplication([])

        widget = GroupChatMonitorWidget()
        init_size = widget._font_size
        self.assertEqual(init_size, 12)

        # 放大字体
        widget._increase_font_size()
        self.assertEqual(widget._font_size, 13)

        # 缩小字体
        widget._decrease_font_size()
        self.assertEqual(widget._font_size, 12)

    def test_media_router_standing_art_and_phrases(self):
        # 1. 明确立绘需求识别
        mi1 = parse_media_intent("帮我网络搜索初雪樱女主的立绘发到群里")
        self.assertIsNotNone(mi1)
        self.assertEqual(mi1.kind, "image")
        self.assertEqual(mi1.operation, "search_send")
        self.assertEqual(mi1.orientation, "")
        self.assertIn("初雪樱", mi1.query)

        # 2. 角色立绘
        mi2 = parse_media_intent("发两张芙莉莲角色立绘给我")
        self.assertIsNotNone(mi2)
        self.assertEqual(mi2.kind, "image")
        self.assertEqual(mi2.count, 2)
        self.assertEqual(mi2.orientation, "")
        self.assertIn("芙莉莲", mi2.query)

        # 3. CG 与插画
        mi3 = parse_media_intent("搜几张千之刃涛的CG壁纸")
        self.assertIsNotNone(mi3)
        self.assertEqual(mi3.kind, "image")
        self.assertIn("千之刃涛", mi3.query)

        # 4. 文件导出与直传
        mi4 = parse_media_intent("把刚才的讨论整理成 markdown 文件发我")
        self.assertIsNotNone(mi4)
        self.assertEqual(mi4.operation, "export")
        self.assertEqual(mi4.extension, "md")


if __name__ == "__main__":
    unittest.main()
