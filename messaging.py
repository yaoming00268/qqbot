"""Typed outbound text and receipts shared by the two QQ transports."""
from __future__ import annotations

from dataclasses import dataclass
import re


class SendError(RuntimeError):
    pass


class SendUnknown(SendError):
    """The server may have accepted the operation; do not blindly resend."""


class SendUnavailable(SendError):
    """Disconnected before submission; the operation is safe to queue."""


MESSAGE_ACTIONS = frozenset(("send_msg", "send_group_msg", "send_private_msg"))
SUBMIT_ACTIONS = MESSAGE_ACTIONS | {"upload_group_file", "upload_private_file"}


def check_onebot_response(action, response):
    """Gateway listener timeouts cannot prove a submitted message failed."""
    if not isinstance(response, dict):
        raise (SendUnknown if action in SUBMIT_ACTIONS else SendError)("OneBot 未返回有效回执。")
    if response.get("status") == "ok" and response.get("retcode", 0) == 0:
        return response
    details = " ".join(str(response.get(key) or "") for key in ("message", "wording", "error", "data"))
    if action in SUBMIT_ACTIONS and re.search(r"\btimeout\b|timed?\s*out|超时|socket hang up|connection closed", details, re.I):
        raise SendUnknown("QQ 网关未确认发送结果，已标记为 unknown；请核对聊天记录，系统不会自动重发。")
    raise SendError(f"OneBot 拒绝操作 {action}（错误码 {response.get('retcode', 'unknown')}）：{details.strip()[:500]}")


def is_master_user(master_config: str, user_id: str) -> bool:
    """Shared identity check for privileged global commands in both transports."""
    return bool(user_id) and str(user_id) in {
        item.strip() for item in re.split(r"[,，;；]", str(master_config or "")) if item.strip()
    }


@dataclass
class SendReceipt:
    status: str = "accepted"
    message_id: str = ""
    file_id: str = ""


class OutboundText(str):
    """A string for logs/legacy callers with explicitly constructed CQ metadata.

    Never parse the string representation to generate outbound actions. Only the
    separate, validated fields are used by the transport.
    """
    def __new__(cls, body: str, mention: str = "", quote: str = "", *, reply_to: str = "", reply_sender: str = "",
                reply_supersedable: bool = True, reply_quote: bool | None = None):
        mention = str(mention or "")
        quote = str(quote or "")
        valid_id = r"[A-Za-z0-9_-]{1,100}"
        mention = mention if re.fullmatch(valid_id, mention) and mention != "all" else ""
        quote = quote if re.fullmatch(valid_id, quote) else ""
        prefix = (f"[CQ:reply,id={quote}]" if quote else "") + (f"[CQ:at,qq={mention}]" if mention else "")
        value = super().__new__(cls, (prefix + " " if prefix else "") + body)
        value.body, value.mention, value.quote = body, mention, quote
        value.reply_to, value.reply_sender = str(reply_to or ""), str(reply_sender or "")
        value.reply_supersedable = reply_supersedable
        value.reply_quote = reply_quote
        return value

    def segments(self, body: str | None = None, include_prefix: bool = True) -> list[dict]:
        segments = []
        if include_prefix and self.quote:
            segments.append({"type": "reply", "data": {"id": self.quote}})
        if include_prefix and self.mention:
            segments.append({"type": "at", "data": {"qq": self.mention}})
        segments.append({"type": "text", "data": {"text": self.body if body is None else body}})
        return segments


def sentence_ends(text: str) -> list[int]:
    """Find prose stops without treating URLs, inline code or decimals as sentences."""
    protected = iter(re.finditer(r'https?://[^\s<>]+|`+[^`\n]+`+|“[^”]*”|「[^」]*」|『[^』]*』', text))
    span = next(protected, None)
    ends = []
    for match in re.finditer(r'[。！？!?]+[”’"」』）)]*|(?<!\.)\.(?!\.)[”’"]*(?=\s|$)', text):
        while span is not None and span.end() <= match.start():
            span = next(protected, None)
        if span is not None and span.start() <= match.start() < span.end():
            continue
        ends.append(match.end())
    return ends


def split_text(text: str, limit: int, max_chunks: int | None = None) -> list[str]:
    """Split at a preferred length while keeping URLs intact and code indented."""
    limit = max(1, int(limit or 1200))
    body = str(text or "").strip()
    start = 0
    urls = iter(re.finditer(r"https?://[^\s<>]+", body))
    url = next(urls, None)
    stops = sentence_ends(body)
    stop_index = 0
    chunks = []
    while start < len(body) and (max_chunks is None or len(chunks) < max_chunks):
        end = min(len(body), start + limit)
        if end < len(body):
            newline = body.rfind("\n", start, end)
            if newline - start >= (end - start) // 2:
                end = newline + 1
            else:
                # A message boundary should finish a sentence where possible.
                while stop_index < len(stops) and stops[stop_index] <= end:
                    stop_index += 1
                sentence_end = stops[stop_index - 1] if stop_index else start
                if sentence_end - start >= max(1, (end - start) // 2):
                    end = sentence_end
            # The chat length preference must not turn a usable URL into
            # several unrelated messages. A URL may occupy its own longer chunk.
            while url is not None and url.end() <= end:
                url = next(urls, None)
            if url is not None and url.start() < end < url.end():
                end = url.start() if url.start() > start else url.end()
        chunks.append(body[start:end])
        start = end
    return chunks
