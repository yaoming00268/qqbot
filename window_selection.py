"""Choose an application's content/modal window without guessing by title or area."""
from __future__ import annotations


NON_CONTENT_CLASSES = {"UAC_InputIndicatorOverlayWnd", "UAC Input Indicator", "IME", "MSCTFIME UI",
                       "CiceroUIWndFrame", "Touch Tooltip Window", "tooltips_class32"}


def valid_client(window):
    size = window.get("size", [])
    return len(size) == 2 and all(type(value) is int and 0 < value <= 8192 for value in size)


def content_window(window):
    return (valid_client(window) and window.get("class_name", "") not in NON_CONTENT_CLASSES
            and not window.get("ex_style", 0) & 0x08000000)  # WS_EX_NOACTIVATE


def hidden_content(window):
    # Hidden framework helpers often have plausible sizes. Only restore a
    # substantial, enabled top-level caption/frame/popup, never IME/tool windows.
    return (content_window(window) and window.get("enabled", True)
            and min(window["size"]) >= 120 and max(window["size"]) >= 240
            and bool(window.get("style", 0) & (0x00C00000 | 0x00040000 | 0x80000000))
            and not window.get("style", 0) & 0x40000000
            and not window.get("ex_style", 0) & 0x80)  # WS_CHILD / WS_EX_TOOLWINDOW


def minimized_content(window):
    # An iconic window may report a zero client. GetWindowPlacement's normal
    # bounds let observation restore a real framed window without treating an
    # arbitrary zero-size framework helper as content.
    rect = window.get("normal_rect", [])
    if (not window.get("minimized") or len(rect) != 4
            or not all(type(value) is int for value in rect)):
        return False
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    return hidden_content({**window, "size": [width, height]})


def choose_window(windows, *, recover=False):
    """Return (candidate, reason); unrelated usable windows stay ambiguous."""
    visible = [window for window in windows if window.get("visible", True)
               and (content_window(window) or minimized_content(window))]
    if visible:
        candidates = visible
    elif recover:
        candidates = [window for window in windows if hidden_content(window) or minimized_content(window)]
    else:
        return None, "not_ready"
    if len(candidates) == 1:
        window = candidates[0]
        if not window.get("enabled", True):
            return None, "blocked"
        return window, "content" if visible else "hidden_content"
    # A modal is the enabled leaf of one owner chain with a disabled ancestor.
    # Never pick a random large window when independent documents/dialogs exist.
    by_id = {window["hwnd"]: window for window in candidates}
    modals = []
    for window in candidates:
        chain, owner = set(), window.get("owner_hwnd", 0)
        while owner in by_id and owner not in chain:
            chain.add(owner)
            owner = by_id[owner].get("owner_hwnd", 0)
        if (window.get("enabled", True) and len(chain) == len(candidates) - 1
                and any(not by_id[hwnd].get("enabled", True) for hwnd in chain)):
            modals.append(window)
    if len(modals) == 1:
        return modals[0], "modal"
    return None, "ambiguous" if candidates else "not_ready"


def window_details(windows):
    return [{**{key: window[key] for key in ("hwnd", "pid", "size", "class_name", "visible", "enabled",
                                           "minimized", "owner_hwnd", "normal_rect") if key in window},
             "title": str(window.get("title", ""))[:200],
             "content_candidate": content_window(window) or minimized_content(window)} for window in windows[:24]]
