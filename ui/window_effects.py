"""Windows 11 system acrylic with a Windows 10 composition fallback."""
from __future__ import annotations

import ctypes
import os


def apply_effects(window, preferences):
    result = {"supported": False, "effect": "css", "window_opacity": preferences["window_opacity"]}
    if os.name != "nt" or getattr(window, "native", None) is None:
        return result
    native = window.native
    def apply():
        try:
            native.Opacity = preferences["window_opacity"] / 100
            result["applied_opacity"] = round(float(native.Opacity) * 100, 1)
            hwnd = int(native.Handle.ToInt64())
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
            def attribute(key, value):
                number = ctypes.c_int(value)
                return dwm.DwmSetWindowAttribute(hwnd, key, ctypes.byref(number), ctypes.sizeof(number)) == 0
            dark = preferences["theme"] == "dark"
            if preferences["theme"] == "system":
                try:
                    import winreg
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                        dark = not bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
                except OSError:
                    pass
            attribute(20, int(dark))
            attribute(33, 2)  # DWMWCP_ROUND
            enabled = preferences["native_glass"]
            if attribute(38, 3 if enabled else 1):
                result.update(supported=True, effect="acrylic" if enabled else "off")
                return
            class Accent(ctypes.Structure):
                _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int), ("color", ctypes.c_uint), ("animation", ctypes.c_int)]
            class Composition(ctypes.Structure):
                _fields_ = [("attribute", ctypes.c_int), ("data", ctypes.c_void_p), ("size", ctypes.c_size_t)]
            alpha = int(preferences["surface_opacity"] / 100 * 210)
            accent = Accent(4 if enabled else 0, 2, (alpha << 24) | (0x211E18 if dark else 0xF5F3F0), 0)
            composition = Composition(19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent))
            function = ctypes.WinDLL("user32").SetWindowCompositionAttribute
            function.argtypes = [ctypes.c_void_p, ctypes.POINTER(Composition)]
            if function(hwnd, ctypes.byref(composition)):
                result.update(supported=True, effect="acrylic" if enabled else "off")
        except (AttributeError, OSError, ValueError) as error:
            result["detail"] = str(error)
    try:
        if native.InvokeRequired:
            from System import Action
            native.Invoke(Action(apply))
        else:
            apply()
    except Exception as error:
        result["detail"] = str(error)
    return result
