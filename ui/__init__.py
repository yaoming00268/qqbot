"""pywebview Studio; legacy Qt components are available by explicit import."""


def __getattr__(name):
    if name == "StudioAPI":
        from ui.studio_api import StudioAPI
        return StudioAPI
    if name == "BotStudioGUI":
        from ui.main_window import BotStudioGUI
        return BotStudioGUI
    if name in ("GroupChatMonitorWidget", "FrostedChatBrowser"):
        from ui.components import monitor_sidebar
        return getattr(monitor_sidebar, name)
    if name in ("build_frosted_stylesheet", "create_vector_icon", "ACCENT_COLORS"):
        from ui import theme
        return getattr(theme, name)
    raise AttributeError(name)
