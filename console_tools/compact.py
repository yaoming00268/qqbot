"""Small MCP receipts; full CLI/operator results and audit rows remain available."""
from __future__ import annotations


def compact_receipt(action, value, operation=""):
    if value.get("ok") is False or value.get("error"):
        return value
    if action == "context":
        keys = ("ok", "scope", "role", "binding", "capabilities", "output_dir", "reply_char_limit", "files", "memory_files", "speech")
        return {**{key: value[key] for key in keys if key in value and value[key] not in ("", [], {}, 0)},
                "help": "qqbot_console_help(command,operation)"}
    if action in ("reply", "commit", "finish", "wait", "message", "sticker", "deliver", "send", "submit", "search", "generate-image", "generate-video", "command", "receive"):
        keys = ("ok", "id", "state", "next_action", "delivery", "message_ids", "command", "request_id", "repeated")
        result = {key: value[key] for key in keys if key in value}
        # Delivery receipts may include a large plan already stored in status.
        if isinstance(result.get("delivery"), dict):
            result["delivery"] = {key: result["delivery"][key] for key in ("state", "approved", "count") if key in result["delivery"]}
        return result
    if action == "sandbox" and operation in ("steer", "skip", "restart", "play", "use", "browse", "pause", "finish"):
        return {key: item for key, item in value.items() if key not in ("next_commands", "examples", "task", "checkpoint")}
    return value
