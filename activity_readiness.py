"""Bounded, cancellable observation waits; only observation is retried."""
from __future__ import annotations

import asyncio
import math
import time

from runtime_control import finish_thread


TRANSIENT_OBSERVATIONS = {
    "BACKGROUND_WINDOW_NOT_READY", "BACKGROUND_WINDOW_LOADING", "BACKGROUND_WINDOW_MINIMIZED",
    "BACKGROUND_WINDOW_CHANGED", "BACKGROUND_WINDOW_HUNG", "BACKGROUND_CAPTURE_BLANK",
    "BROWSER_NOT_READY", "BROWSER_PAGE_CHANGED", "RENPY_BRIDGE_NOT_READY",
}


async def wait_for_observation(runtime, context, sid, *, check_current, timeout=60, progress=None):
    """Wait for a real frame without relaunching or repeating any input."""
    timeout = max(.2, min(float(timeout), 300))
    started = time.monotonic()
    deadline = started + timeout
    last_error = None
    # The count is also bounded when the scheduler's sleep is mocked in tests.
    for attempt in range(1, math.ceil(timeout / .2) + 1):
        check_current()
        try:
            frame = await finish_thread(runtime.observe, context, sid)
            check_current()
            if progress:
                progress({"state": "ready", "window_ready": True, "attempts": attempt,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "observation_id": frame.get("id", ""), "pid": frame.get("pid"),
                    "capture_method": frame.get("capture_method", frame.get("capture", ""))})
            return frame
        except ValueError as error:
            if getattr(error, "code", "") not in TRANSIENT_OBSERVATIONS:
                raise
            last_error = error
            if progress:
                progress({"state": "waiting_window", "window_ready": False, "attempts": attempt,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "code": error.code, "error": str(error), "details": getattr(error, "details", {})})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(.2 * 1.5 ** min(attempt - 1, 4), 1, remaining))
    if last_error is not None:
        raise last_error
    raise ValueError("后台窗口尚未就绪，未发送任何输入。")
