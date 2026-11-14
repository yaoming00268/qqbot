"""Semantic conversation records, separated from tool protocols and presets."""
from __future__ import annotations

import json


def dialogue_records(records):
    result, seen = [], {}
    for row in records:
        if not isinstance(row, dict):
            continue
        if isinstance(row.get("messages"), list):
            candidates = row["messages"]
        elif "event" in row:
            try:
                event = json.loads(str(row["event"]).splitlines()[0])
            except (ValueError, IndexError):
                if str(row["event"]).lstrip().startswith(("{", "[", "@rules:")):
                    continue
                # Legacy plain dialogue remains evidence with an unknown speaker.
                event = {"type": "message", "text": str(row["event"])}
            if not isinstance(event, dict) or event.get("type") != "message":
                continue
            candidates = [event]
        else:
            if row.get("type") not in (None, "message"):
                continue
            candidates = [row]
        for message in candidates:
            if not isinstance(message, dict) or message.get("type") not in (None, "message"):
                continue
            content = message.get("content", message.get("text", ""))
            if not isinstance(content, str) or not content.strip():
                continue
            bot = bool(message.get("is_bot") or message.get("role") == "assistant")
            if bot and message.get("delivery_state") not in (None, "", "accepted"):
                continue
            author = str(message.get("author_id") or message.get("author_openid") or message.get("sender_id") or message.get("user_id") or ("bot" if bot else ""))
            name = str(message.get("author_name") or message.get("sender_name") or message.get("sender") or message.get("nickname") or
                       message.get("name") or ("机器人" if bot else "未知发言者"))
            identity = str(message.get("message_id") or message.get("id") or message.get("event_id") or row.get("message_id") or "")
            timestamp = message.get("timestamp", message.get("received_at", row.get("time")))
            key = (identity, author, bot, content) if identity else (timestamp, author, bot, content)
            if key in seen:
                existing = seen[key]
                if existing["author_name"] in ("未知发言者", "机器人"):
                    existing["author_name"] = name
                for field in ("timestamp", "reply_to", "source_message_id", "delivery_state"):
                    if not existing.get(field) and message.get(field):
                        existing[field] = message[field]
                continue
            normalized = {"message_id": identity, "author_id": author, "author_name": name,
                "role": "assistant" if bot else "user" if author else "unknown", "is_bot": bot,
                "timestamp": timestamp, "content": content,
                **{key: message[key] for key in ("reply_to", "source_message_id", "delivery_state") if message.get(key)}}
            seen[key] = normalized
            result.append(normalized)
    return result
