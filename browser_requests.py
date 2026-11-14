"""Recognize direct owner browsing requests, keeping URLs separate from prose."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from browser_control import checked_url


_URL = re.compile(r"https?://[^\s<>\"'`【】（）\[\]{}，。！？；、]+", re.I)
_TRAILING_REQUEST = re.compile(
    r"(?:刷(?:帖子?|贴子|视频)|看(?:帖子?|视频)|看看|逛逛|浏览一下|阅读一下|的(?:帖子|视频|内容))"
    r"(?:吧|呀|啊|哦|呗|就行|就好|一下|一会儿|一会|了)*$")
_PREFIX = re.compile(
    r"^(?:(?:请|麻烦|劳驾|帮我|替我|给我|你|自己|自行|能不能|能否|可以|先|现在|以后|有空|就|再)\s*)*"
    r"(?:去|打开|浏览|访问|逛|刷|看|阅读|用浏览器|用你自己的浏览器)")
_READ = re.compile(r"刷帖|刷视频|看帖|看视频|看看|逛|浏览|访问|打开|阅读|去")


def browser_urls(text):
    result = []
    for match in _URL.finditer(str(text or "")):
        candidate = match[0].rstrip(").,:;!?，。")
        suffix = _TRAILING_REQUEST.search(candidate)
        try:
            parts = urlsplit(candidate)
            if suffix and not parts.query and not parts.fragment:
                candidate = candidate[:suffix.start()]
            url = checked_url(candidate)
        except ValueError:
            continue
        if url not in result:
            result.append(url)
    return result


def direct_browser_request(text, *, is_master, directed):
    """Only bootstrap unambiguous instructions; discussion stays with the agent."""
    if not is_master or not directed:
        return None
    text = re.sub(r"\[CQ:at,[^\]]+\]", "", str(text or "")).strip()
    if (not text or any(marker in text for marker in ("```", "`", "“", "”", '"', "「", "」", "『", "』"))
            or re.search(r"别去|不要|不许|不用|停止|取消|下载|保存文件|搜索|怎么|如何|假如|如果|例如|示例|他说|她说|有人说", text)):
        return None
    urls = browser_urls(text)
    if len(urls) != 1 or not _READ.search(text):
        return None
    before_url = text[:_URL.search(text).start()].strip()
    after_url = text[_URL.search(text).end():].strip()
    prose = _URL.sub(" ", text)
    if re.search(r"(?:去|打开|浏览|访问|看|刷)(?:过|了)|是不是|是否|有没有", prose):
        return None
    if not _PREFIX.match(text) and not (not before_url and _PREFIX.match(after_url)):
        return None
    return {"url": urls[0], "text": text}
