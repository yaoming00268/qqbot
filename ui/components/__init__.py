# -*- coding: utf-8 -*-
"""
UI Components Package for QQBot Studio.
Modular, maintainable widgets for tabs, sidebars, and themes.
"""

from ui.components.monitor_sidebar import GroupChatMonitorWidget, FrostedChatBrowser, MessageBlockData
from ui.components.persona_tab import PersonaTabWidget
from ui.components.config_tab import ConfigTabWidget
from ui.components.console_tab import ConsoleTabWidget
from ui.components.media_tab import MediaTabWidget
from ui.components.slang_editor import SlangGlossaryEditor
from ui.components.emotion_tab import EmotionTabWidget

__all__ = [
    "GroupChatMonitorWidget",
    "FrostedChatBrowser",
    "MessageBlockData",
    "PersonaTabWidget",
    "ConfigTabWidget",
    "ConsoleTabWidget",
    "MediaTabWidget",
    "SlangGlossaryEditor",
    "EmotionTabWidget",
]
