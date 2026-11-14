"""QQBot Studio entry point, powered by pywebview and Edge WebView2."""
from __future__ import annotations


def main():
    import argparse
    from runtime_paths import configure_runtime
    from ui.webview_app import launch
    parser = argparse.ArgumentParser(description="QQBot Studio")
    parser.add_argument("--home")
    args = parser.parse_args()
    return launch(configure_runtime(args.home))


def __getattr__(name):
    # Legacy integrations can explicitly request their Qt widgets.
    if name == "BotStudioGUI":
        from ui.main_window import BotStudioGUI
        return BotStudioGUI
    if name in ("GroupChatMonitorWidget", "FrostedChatBrowser"):
        from ui.components import monitor_sidebar
        return getattr(monitor_sidebar, name)
    if name in ("create_vector_icon", "build_frosted_stylesheet", "ACCENT_COLORS"):
        from ui import theme
        return getattr(theme, name)
    if name == "MODERN_STYLE":
        from ui.theme import build_frosted_stylesheet
        return build_frosted_stylesheet()
    raise AttributeError(name)


if __name__ == "__main__":
    raise SystemExit(main())
