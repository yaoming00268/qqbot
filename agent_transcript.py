"""Read changing agent logs without blocking the bot or mixing request replies."""
from __future__ import annotations

import json
from pathlib import Path
import re


class TranscriptReader:
    """Parse appended rows once; also handle rewritten logs and partial writes."""

    def __init__(self, path, *, minimum=0):
        self.path = Path(path)
        self.minimum = minimum
        self.signature = None
        self.data = b""
        self.pending = b""
        self.rows = []
        self.revision = 0

    @staticmethod
    def _row(raw):
        if not raw.strip():
            return None
        try:
            row = json.loads(raw)
            return row if isinstance(row, dict) else None
        except (ValueError, UnicodeError):
            return None

    def read(self):
        # Called in a worker thread. An unchanged file needs no read or JSON
        # decoding. A rewrite is detected by comparing bytes, even at equal size.
        try:
            stat = self.path.stat()
            signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if signature != self.signature:
                data = self.path.read_bytes()
                if data != self.data:
                    if data.startswith(self.data):
                        extra = self.pending + data[len(self.data):]
                    else:
                        self.rows = []
                        extra = data
                    lines = extra.split(b"\n")
                    self.pending = lines.pop()
                    self.rows.extend(row for raw in lines if (row := self._row(raw)) is not None
                                     and step_index(row) >= self.minimum)
                    self.data = data
                    self.revision += 1
                self.signature = signature
        except FileNotFoundError:
            pass  # The agent may create the log after accepting the request.
        # Some backend versions omit the final newline. Never discard that row,
        # and never commit a half-written JSON object to the durable snapshot.
        last = self._row(self.pending)
        return [*self.rows, last] if last is not None and step_index(last) >= self.minimum else list(self.rows)


def step_index(row):
    try:
        # Older service errors have no step number. They still belong to the
        # initial invocation and must fail promptly instead of timing out.
        return int(row.get("step_index", 0))
    except (TypeError, ValueError):
        return -1


def native_diagnostic_rows(trajectory):
    """Read only structured inputs/errors; never expose binary payloads or thinking."""
    rows = []
    steps = trajectory.get("steps", [])
    last_model = max((index for index, step in enumerate(steps) if isinstance(step, dict)
                      and step.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE"), default=-1)
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        kind = str(step.get("type", "")).removeprefix("CORTEX_STEP_TYPE_")
        if kind not in ("ERROR_MESSAGE", "SYSTEM_ERROR", "USER_INPUT", "SYSTEM_MESSAGE"):
            continue
        if kind in ("ERROR_MESSAGE", "SYSTEM_ERROR") and index < last_model:
            continue  # A later model continuation supersedes this old error.
        meta = step.get("metadata") or {}
        row = {"type": kind, "step_index": (meta.get("sourceTrajectoryStepInfo") or {}).get("stepIndex", index),
            "status": str(step.get("status", "")).removeprefix("CORTEX_STEP_STATUS_"),
            "source": str(meta.get("source", "")).removeprefix("CORTEX_STEP_SOURCE_")}
        if kind in ("USER_INPUT", "SYSTEM_MESSAGE"):
            body = step.get("userInput") or step.get("systemMessage") or {}
            text = body.get("userResponse") or body.get("text") or body.get("content")
            row["content"] = str(text or "\n".join(str(item.get("text") or "")
                for item in body.get("items", []) if isinstance(item, dict)))
        else:
            detail = (step.get("errorMessage") or {}).get("error") or step.get("error") or {}
            # Use metadata with the provider reason, not just the generic 400.
            if isinstance(detail, dict):
                detail = {key: detail[key] for key in ("shortError", "modelErrorMessage", "details",
                    "rpcErrorDetails", "errorCode", "userErrorNumber") if key in detail}
                summary = str(detail.get("shortError") or detail.get("modelErrorMessage") or "")
                row["error"] = summary + "\n" + json.dumps(detail, ensure_ascii=False)
            else:
                row["error"] = json.dumps(detail, ensure_ascii=False) if isinstance(detail, list) else str(detail)
        rows.append(row)
    return rows


def latest_model(rows):
    for row in reversed(rows):
        # A later service failure means an earlier progress final cannot make
        # this invocation complete when another input arrives during retry.
        if row.get("type") in ("SYSTEM_ERROR", "ERROR_MESSAGE") and row.get("source") != "TOOL":
            return None
        if row.get("source") == "MODEL" and row.get("type") == "PLANNER_RESPONSE":
            return row
    return None


def select_request_rows(rows, minimum=0, marker=""):
    """Bind replies to an actual logged input, never just the newest step.

    A background task can finish between our pre-send snapshot and the backend
    recording our input. Its acknowledgement must not become a QQ response.
    """
    rows = [row for row in rows if step_index(row) >= minimum]
    if not marker:
        return rows, True, False
    selected, found, interrupted = [], False, False
    for row in rows:
        inbound = row.get("type") in ("USER_INPUT", "SYSTEM_MESSAGE")
        content = str(row.get("content") or "")
        if not found:
            if inbound and marker in content:
                found = True
                selected.append(row)
            continue
        if inbound:
            latest = latest_model(selected)
            completed = (latest and latest.get("status") == "DONE" and not latest.get("tool_calls"))
            # Once our response is complete, subsequent system notifications
            # and their model acknowledgements belong to a separate invocation.
            background = bool(re.search(r"\bsender=[^\s]+/task-", content))
            if completed or not background:
                interrupted = not completed
                break
        selected.append(row)
    return selected, found, interrupted


def final_text(rows, *, combine=False):
    """Keep all final bubbles after the last tool call, excluding progress prose."""
    latest = latest_model(rows)
    if latest is None or latest.get("status") != "DONE" or latest.get("tool_calls"):
        return None
    if not combine:
        return str(latest.get("content") or "").strip()
    finals = []
    for row in rows:
        if row.get("source") != "MODEL" or row.get("type") != "PLANNER_RESPONSE":
            continue
        if row.get("tool_calls") or row.get("status") != "DONE":
            finals.clear()
        elif str(row.get("content") or "").strip():
            # A step can be logged again as its status changes.
            finals = [(step, text) for step, text in finals if step != step_index(row)]
            finals.append((step_index(row), str(row["content"]).strip()))
    return "\n\n".join(text for _, text in finals)
