"""QQBot Studio desktop host, rendered by pywebview and Edge WebView2."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading

from runtime_paths import application_home, resource_path
from ui.studio_api import StudioAPI


def launch(home=None, *, smoke=False, report=None):
    import webview
    home = Path(home or application_home()).resolve()
    api = StudioAPI(home)
    window = webview.create_window("QQBot Studio", url=str(resource_path("ui/web/index.html")),
        js_api=api, width=1320, height=860, min_size=(940, 640), frameless=True, easy_drag=False,
        shadow=True, transparent=True, background_color="#eef1f4", text_select=True, hidden=smoke)
    api._window = window
    def loaded():
        api.preferences(api._preferences, persist=False)
        if not smoke:
            return
        import time
        try:
            for _ in range(80):
                if window.evaluate_js("Boolean(window.studio && window.studio.ready)"):
                    break
                time.sleep(.1)
            else:
                raise RuntimeError("Studio frontend did not initialize")
            result = window.evaluate_js("window.studio.smoke()")
            result.update(engine="pywebview", native=api._native)
            preview = api.preferences({"window_opacity": 86}, persist=False)
            applied = preview.get("data", {}).get("native", {}).get("applied_opacity")
            result["opacity_check"] = {"requested": 86, "applied": applied, "ok": applied == 86}
            result["ok"] = result["ok"] and (os.name != "nt" or applied == 86)
            api.preferences(api._preferences, persist=False)
            if report:
                Path(report).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            if not result.get("ok"):
                raise RuntimeError(json.dumps(result, ensure_ascii=False))
        except Exception as error:
            if report:
                Path(report).write_text(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), encoding="utf-8")
        finally:
            api._allow_close = True
            window.destroy()
    def closing():
        if api._allow_close:
            return True
        try:
            window.run_js("window.studio && window.studio.requestClose()")
        except Exception:
            return True
        return False
    window.events.loaded += loaded
    window.events.closing += closing
    window.events.maximized += lambda: setattr(api, "_maximized", True)
    window.events.restored += lambda: setattr(api, "_maximized", False)
    webview.start(gui="edgechromium" if os.name == "nt" else None, debug=False,
        private_mode=False, storage_path=str(home / "data/studio/webview"))
    if smoke and report:
        result = json.loads(Path(report).read_text(encoding="utf-8"))
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Studio smoke check failed"))
        return result
    return 0
