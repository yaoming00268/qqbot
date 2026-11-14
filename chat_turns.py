"""Console-owned chat formatting and receipt-order tracking."""
from __future__ import annotations

from collections import OrderedDict
import json
import re
import time

from messaging import sentence_ends, split_text
from prompt_builder import AIDecisionResult, _extract_json_candidate, parse_ai_decision_and_reply


_SILENCE_MARKER = re.compile(
    r"^\s*[\[【（(]\s*(?:PASS|WAIT|IGNORE|SILENT|保持安静|保持沉默|不回复|不发言|不插话|等待下一条消息)\s*[\]】）)]\s*$", re.I)
_IDLE_STATUS = re.compile(
    r"^\s*(?:[\[【（(]\s*)?(?:(?:保持在线(?:并|，|,)?|继续在线(?:并|，|,)?|继续)?等待下一条(?:群)?消息|"
    r"(?:本轮|本次)(?:不回复|不发言|不插话)|继续待机)[。.!！…\s]*(?:[\]】）)])?\s*$")


def clean_chat_text(value):
    """Remove standalone controller placeholders, retaining ordinary quoted speech."""
    lines, fence = [], ""
    for line in str(value or "").splitlines():
        marker = re.match(r"\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            lines.append(line)
        elif fence or not (_SILENCE_MARKER.fullmatch(line) or _IDLE_STATUS.fullmatch(line)):
            lines.append(line)
    return "\n".join(lines).strip()


class ChatTurns:
    def __init__(self):
        self.groups = {}

    def observe(self, scope, message_id, sender_id, text="", received_at=None):
        if not message_id:
            return
        rows = self.groups.setdefault(str(scope), OrderedDict())
        if str(message_id) in rows:
            return
        rows[str(message_id)] = {"id": str(message_id), "sender_id": str(sender_id),
            "text": str(text or ""), "received_at": time.time() if received_at is None else received_at}
        while len(rows) > 256:
            rows.popitem(last=False)

    def newer(self, scope, message_id, sender_id=None):
        rows = self.groups.get(str(scope), {})
        found = str(message_id) not in rows
        for key, row in rows.items():
            if key == str(message_id):
                found = True
            elif found and (sender_id is None or row["sender_id"] == str(sender_id)):
                return True
        return False

    def source(self, scope, message_id):
        return self.groups.get(str(scope), {}).get(str(message_id), {})

    def superseded(self, scope, message_id, sender_id, *, max_gap=6):
        """Only a nearby continuation can replace a reply that has not started."""
        rows = self.groups.get(str(scope), {})
        source = rows.get(str(message_id))
        if not source or source.get("reply_started"):
            return False
        found = False
        for key, row in rows.items():
            if key == str(message_id):
                found = True
            elif found and row["sender_id"] == str(sender_id):
                gap = row["received_at"] - source["received_at"]
                return 0 <= gap <= max_gap
        return False

    def mark_replied(self, scope, message_id):
        source = self.source(scope, message_id)
        if source:
            source["reply_started"] = True


def _reply_blocks(text):
    """Keep structured text together; plain chat lines are separate bubbles."""
    paragraphs, lines, fence = [], [], ""
    for line in text.splitlines():
        marker = re.match(r"\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
        if not line.strip() and not fence:
            if lines:
                paragraphs.append("\n".join(lines).strip("\n"))
                lines = []
        else:
            lines.append(line)
    if lines:
        paragraphs.append("\n".join(lines).strip("\n"))
    structured = re.compile(r"(?m)^(?:\s*(?:`{3,}|~{3,}|#{1,6}\s|[-*+]\s|\d+[.)、]\s*|>\s?|\|)| {4}|\t)")
    for paragraph in paragraphs:
        if structured.search(paragraph):
            yield paragraph, True
        else:
            for line in paragraph.splitlines():
                if line.strip():
                    yield line.strip(), False


def _chat_sentences(text, target):
    """Group short prose at sentence boundaries, retaining every word."""
    start, end = 0, 0
    for stop in sentence_ends(text):
        if end > start and stop - start > target:
            yield text[start:end].strip()
            start = end
        end = stop
    if len(text) - start > target and end > start:
        yield text[start:end].strip()
        start = end
    if text[start:].strip():
        yield text[start:].strip()


def plain_reply(response, limit, *, sender_id="", message_id=""):
    """Accept plain dialogue; legacy protocol is unwrapped or rejected, never sent."""
    text = clean_chat_text(response)
    decision = AIDecisionResult(target_user_id=sender_id, target_msg_id=message_id)
    if not text or text in ("[PASS]", "[WAIT]", "[IGNORE]", "[不回复]"):
        decision.reason = "等待下一条消息或本轮不发言"
        return decision
    legacy = _extract_json_candidate(text)
    if legacy is not None and any(key in legacy for key in ("should_reply", "messages", "reply", "tasks", "skill")):
        decision = parse_ai_decision_and_reply(text, default_user_id=sender_id, default_msg_id=message_id)
        if legacy.get("should_reply") is False:
            decision.should_reply = False
            decision.reply_text = ""
            decision.messages = []
            return decision
        text = "\n\n".join(str(item.get("text") or "") for item in decision.messages) if decision.messages else str(
            legacy.get("reply") or legacy.get("content") or "").strip()
        text = clean_chat_text(text)
    elif re.search(r'\{\s*"(?:should_reply|messages|reply|tasks|action)"\s*:', text, re.I):
        decision.reason = "旧格式响应不完整，已阻止发送协议文本"
        return decision
    # Short unformatted chat gets a soft bubble target. Detailed answers keep
    # their paragraphs, lists and code, and can use as many messages as needed.
    target = min(60, max(1, int(limit or 1200)))
    messages = []
    for block, structured in _reply_blocks(text):
        groups = _chat_sentences(block, target) if not structured and len(text) <= 360 and len(block) > target else [block]
        for group in groups:
            for chunk in split_text(group, limit):
                if chunk.strip():
                    messages.append({"text": chunk.strip("\r\n"), "action": "none",
                        "target_user_id": str(sender_id), "target_msg_id": str(message_id)})
    decision.messages = messages
    decision.reply_text = "\n\n".join(item["text"] for item in messages)
    decision.should_reply = bool(messages)
    decision.action = "none"
    return decision


def reply_json(decision):
    return json.dumps({"should_reply": decision.should_reply, "reply": decision.reply_text,
        "messages": decision.messages, "action": "none", "reason": decision.reason}, ensure_ascii=False)


def fit_reply_slots(messages, slots):
    """Official passive replies have five slots; merge adjacent text, never drop it."""
    items = [dict(item) for item in messages]
    while len(items) > max(1, slots):
        index = min(range(len(items) - 1), key=lambda i: len(items[i]["text"]) + len(items[i + 1]["text"]))
        items[index]["text"] += "\n\n" + items[index + 1]["text"]
        del items[index + 1]
    return items


def emit_reply(*, group_id, sender_id, source_message_id, source_text, reply, quoted=False,
               segment_index=1, segment_count=1):
    print("[FEED_ACTION] " + json.dumps({"time": time.strftime("%H:%M:%S"), "group_id": str(group_id),
        "action": "quote" if quoted else "none", "reply": reply, "target": str(sender_id),
        "source_message_id": str(source_message_id), "source_text": str(source_text)[:200],
        "quoted": bool(quoted), "segment_index": segment_index, "segment_count": segment_count,
        "reason": "控制台按来源消息发送"}, ensure_ascii=False), flush=True)
