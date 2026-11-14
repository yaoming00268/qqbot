"""Discover downloadable resources in public source pages without executing page code."""
from __future__ import annotations

import asyncio
from collections import deque
import html
from html.parser import HTMLParser
import json
import logging
from pathlib import PurePosixPath
import re
from urllib.parse import unquote, urljoin, urlsplit

from media_downloader import MediaError, http_request, public_url
from media_models import MediaAsset, MediaIntent


logger = logging.getLogger("MediaCrawler")
EXTENSIONS = {
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff"},
    "video": {".mp4", ".webm", ".ogv", ".ogg", ".mov"},
    "file": {".pdf", ".zip", ".txt", ".csv", ".md", ".json", ".log", ".docx", ".xlsx", ".pptx"},
}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_NAVIGATION = re.compile(r"(?:^|[\s_\-])(?:nav|navi|navigation|header|footer|menu|sns|share|sidebar|advert|banner|pageup)(?:[\s_\-]|$)", re.I)


def resource_kind(url, mime=""):
    path = unquote(urlsplit(url).path).lower()
    # File-description pages often end in .jpg but contain HTML.
    if "/wiki/" not in path and ":" not in path.rsplit("/", 1)[-1]:
        suffix = PurePosixPath(path).suffix
        for kind, extensions in EXTENSIONS.items():
            if suffix in extensions:
                return kind
    if mime.startswith("image/") and mime != "image/svg+xml":
        return "image"
    if mime.startswith("video/") or mime == "application/ogg":
        return "video"
    if mime in ("application/pdf", "application/zip", "application/octet-stream") or mime.startswith(
            "application/vnd.openxmlformats-officedocument."):
        return "file"
    return ""


def needs_resolution(asset):
    return not asset.local_path and not asset.metadata.get("crawled") and (
        asset.metadata.get("page_candidate") or not asset.url or PurePosixPath(urlsplit(asset.url).path).suffix.lower() in (
            ".html", ".htm", ".aspx", ".jsp") or (
            not resource_kind(asset.url) and resource_kind(asset.title) != asset.kind))


def _number(value):
    match = re.match(r"\d+", str(value or ""))
    return min(int(match[0]), 100000) if match else 0


def _label(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()[:400]


def _srcset(value):
    candidates = []
    for item in str(value or "").split(",")[:40]:
        parts = item.split()
        if parts:
            try:
                weight = float(parts[1].rstrip("wx")) if len(parts) > 1 else 1
            except ValueError:
                weight = 1
            candidates.append((weight, parts[0]))
    return max(candidates, default=(0, ""))[1]


class _Page(HTMLParser):
    def __init__(self, url, intent, keywords=()):
        super().__init__(convert_charrefs=True)
        self.url = self.base = url
        self.intent = intent
        self.keywords = keywords
        self.resources, self.links = [], []
        self.title, self._title, self._script = "", False, None
        self._title_parts = []
        self._anchor = None
        self._video = self._picture = False
        self._contexts = []

    def absolute(self, value):
        try:
            return public_url(urljoin(self.base, html.unescape(str(value or "").strip()))) if value else ""
        except MediaError:
            return ""

    def relevance(self, text):
        words = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]+", self.intent.query.lower())
        words.extend(self.keywords)
        text = unquote(str(text)).lower()
        compact = re.sub(r"\W+", "", text)
        return min(45, sum(15 for word in words if word.lower() in text or (
            re.sub(r"\W+", "", word.lower()) and re.sub(r"\W+", "", word.lower()) in compact)))

    def add(self, value, *, expected="", title="", width=0, height=0, score=60):
        url = self.absolute(value)
        if not url or len(self.resources) >= 200:
            return
        kind = resource_kind(url)
        if kind and kind != self.intent.kind or not kind and expected != self.intent.kind:
            return
        if re.search(r"\.(?:svg|m3u8|mpd)(?:[?#]|$)", url, re.I):
            return
        width, height = _number(width), _number(height)
        text = url + " " + title
        if self.intent.kind == "image":
            if width and width < 96 or height and height < 96:
                return
            if re.search(r"(?:logo|favicon|sprite|spacer|placeholder|loading|advert|tracking)(?:[_.\s/\-]|$)", text, re.I):
                return
            if not re.search(r"头像|图标|avatar|icon", self.intent.query, re.I) and re.search(
                    r"(?:^|[/_\-])(?:icon|icons|button|btn|navi|sns)(?:[_.\-/]|$)", url, re.I):
                return
            if re.search(r"立绘|立ち絵|standing|tachie", self.intent.query, re.I) and re.search(
                    r"(?:^|[/_\-])(?:speak|text|head|card|header|footer|cover|covers|cv|cg)(?:[_.\-/]|$)", url, re.I):
                return
            if re.search(r"立绘|立ち絵|standing|tachie", self.intent.query, re.I) and re.search(
                    r"screenshot|screen.?shot|スクリーンショット|封面|截图|\bvnimg\b|\bcover\b",
                    text + " " + " ".join(c[1] for c in self._contexts), re.I):
                return
            if re.search(r"立绘|立ち絵|standing|tachie", self.intent.query, re.I) and re.search(
                    r"(?:stand|standing|tachie|立绘|立ち絵)", text + " " + " ".join(c[1] for c in self._contexts), re.I):
                score += 90
        if width and height:
            score += min(15, width * height / 100000)
        self.resources.append({"url": url, "title": _label(title), "width": width, "height": height,
                               "score": score + self.relevance(text)})

    def link(self, value, title="", *, related=False):
        url = self.absolute(value)
        if not url or len(self.links) >= 80 or url == self.url:
            return
        if resource_kind(url) == self.intent.kind:
            self.add(url, title=title, score=110)
        elif urlsplit(url).netloc == urlsplit(self.url).netloc and (
                related or self.relevance(title + " " + url) or re.search(
                    r"(?:gallery|detail|picture|image|illustration|character|/art/|/file/|download|原图|立绘)",
                    title + " " + url, re.I)):
            # Keep a game's/gallery's detail traversal in that section. Footer
            # links to the publisher homepage must not lead to unrelated works.
            scope = urlsplit(self.url).path.rsplit("/", 1)[0] + "/"
            if urlsplit(url).path.startswith(scope) and urlsplit(url).path not in ("", "/"):
                self.links.append((url, self.relevance(title + " " + url) + (30 if related else 0)))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in _VOID_TAGS:
            label = (a.get("class") or "") + " " + (a.get("id") or "")
            blocked = tag in ("nav", "header", "footer") or a.get("role") == "navigation" or bool(_NAVIGATION.search(label))
            self._contexts.append((tag, label, blocked))
        if any(c[2] for c in self._contexts):
            # Relevant detail/download navigation can lead to the resource;
            # its button artwork must never become an image candidate.
            if tag == "a":
                self.link(a.get("href"), a.get("title", ""))
            return
        if tag == "base" and a.get("href"):
            self.base = self.absolute(a["href"]) or self.base
        elif tag == "title":
            self._title = True
            self._title_parts = []
        elif tag == "script":
            self._script = []
        elif tag == "a":
            self._anchor = {"href": a.get("href", ""), "text": [a.get("title", "")], "image": False,
                            "direct": False, "download": "download" in a}
        elif tag == "video":
            self._video = True
            self.add(a.get("src"), expected="video", score=105, title=a.get("title", ""))
        elif tag == "picture":
            self._picture = True
        elif tag == "source":
            if self._video or a.get("type", "").startswith("video/"):
                self.add(a.get("src"), expected="video", score=105)
            elif self._picture:
                self.add(_srcset(a.get("data-srcset") or a.get("srcset")), expected="image", score=95)
        elif tag == "img":
            title = a.get("alt") or a.get("title") or ""
            if self._anchor:
                self._anchor["image"] = True
                if resource_kind(self.absolute(self._anchor["href"])) == "image":
                    self._anchor["direct"] = True
                    self.add(self._anchor["href"], title=title, score=115)
                    return
            originals = [a.get(k) for k in ("data-original", "data-full", "data-full-src", "data-hires", "data-large")]
            srcset = _srcset(a.get("data-srcset") or a.get("srcset"))
            lazy = a.get("data-src") or a.get("data-lazy-src") or a.get("data-url")
            original = next((v for v in originals if v), "")
            value = original or srcset or lazy or a.get("src")
            # Thumbnail dimensions do not describe a linked original/srcset.
            width, height = (0, 0) if original or srcset else (a.get("width"), a.get("height"))
            score = 100 if original else 90 if srcset else 80 if lazy else 65
            if self._anchor and not original and not srcset:
                score -= 35
            self.add(value, expected="image", title=title, width=width, height=height, score=score)
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key in ("og:title", "twitter:title") and not self.title:
                self.title = _label(a.get("content"))
            expected = "image" if key in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image",
                                          "twitter:image:src", "image") else "video" if key in (
                "og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream") else ""
            if expected:
                self.add(a.get("content"), expected=expected, score=70)
            elif key in ("contenturl", "downloadurl"):
                self.add(a.get("content"), expected=self.intent.kind, score=100)
        elif tag == "link":
            if a.get("rel") == "image_src":
                self.add(a.get("href"), expected="image", score=85)
            elif a.get("rel") == "enclosure" or a.get("rel") == "preload" and a.get("as") == self.intent.kind:
                self.add(a.get("href"), title=a.get("title", ""), score=95)
        elif tag in ("iframe", "embed", "object"):
            self.link(a.get("src") or a.get("data"), related=True)

    def handle_endtag(self, tag):
        blocked = any(c[2] for c in self._contexts)
        for index in range(len(self._contexts) - 1, -1, -1):
            if self._contexts[index][0] == tag:
                del self._contexts[index:]
                break
        if blocked:
            return
        if tag == "title":
            self._title = False
            self.title = _label("".join(self._title_parts)) or self.title
        elif tag == "script" and self._script is not None:
            script = "".join(self._script)
            self._script = None
            try:
                self.structured(json.loads(script))
            except (ValueError, RecursionError):
                # Embedded player/gallery state is often JS rather than JSON.
                # Read URL string fields only; never evaluate scripts.
                pattern = r'''["'](original|originalImage|imageUrl|image_url|contentUrl|downloadUrl|playUrl|video_url|src)["']\s*:\s*("(?:\\.|[^"\\])*"|'[^']*')'''
                for match in list(re.finditer(pattern, script, re.I))[:150]:
                    try:
                        value = json.loads(match[2]) if match[2][0] == '"' else match[2][1:-1].replace("\\/", "/")
                        self.structured({match[1]: value})
                    except ValueError:
                        pass
        elif tag == "a" and self._anchor:
            a, self._anchor = self._anchor, None
            if a["direct"]:
                return
            label = _label("".join(a["text"]))
            if a["download"]:
                self.add(a["href"], expected=self.intent.kind, title=label, score=115)
            else:
                self.link(a["href"], label, related=a["image"])
        elif tag == "video":
            self._video = False
        elif tag == "picture":
            self._picture = False

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        elif self._title:
            self._title_parts.append(data)
        elif self._anchor:
            self._anchor["text"].append(data[:1000])

    def structured(self, payload):
        queue, visited = deque([(payload, "", 0)]), 0
        image_keys = {"image", "images", "imageurl", "image_url", "original", "originalimage", "original_url"}
        video_keys = {"video", "videos", "videourl", "video_url", "playurl", "play_url"}
        while queue and visited < 3000:
            value, context, depth = queue.popleft()
            visited += 1
            if depth > 18:
                continue
            if isinstance(value, dict):
                schema = str(value.get("@type", "")).lower()
                expected = "video" if "videoobject" in schema else "image" if "imageobject" in schema else context
                title = value.get("name") or value.get("title") or value.get("caption") or ""
                for key, child in list(value.items())[:300]:
                    key = str(key).lower()
                    field_kind = "image" if key in image_keys else "video" if key in video_keys else expected
                    if isinstance(child, str) and (key in image_keys | video_keys | {
                            "contenturl", "downloadurl", "download_url", "url", "src", "file", "fileurl"}):
                        self.add(child, expected=field_kind or (self.intent.kind if key in (
                            "downloadurl", "download_url", "contenturl") else ""), title=title,
                            width=value.get("width"), height=value.get("height"), score=100)
                    elif isinstance(child, (dict, list)):
                        queue.append((child, field_kind, depth + 1))
            elif isinstance(value, list):
                queue.extend((child, context, depth + 1) for child in value[:300])
            elif isinstance(value, str) and context:
                self.add(value, expected=context, score=95)


class MediaCrawler:
    MAX_PAGES = 8
    MAX_DEPTH = 2
    MAX_PAGE_BYTES = 3 * 1024 * 1024

    def __init__(self, config):
        self.config = config

    async def discover(self, seeds, intent: MediaIntent, *, limit=5, failures=None):
        queue, seen, found = deque(), set(), {}
        pages = 0
        for seed in seeds[:12]:
            url = seed.url or seed.source_url
            try:
                queue.append((public_url(url), seed, 0, "", []))
            except MediaError:
                continue
        try:
            async with asyncio.timeout(self.config.task_timeout_seconds / 2 if self.config.task_timeout_seconds else None):
                while queue and pages < self.MAX_PAGES:
                    url, seed, depth, referer, chain = queue.popleft()
                    if url in seen:
                        continue
                    seen.add(url)
                    pages += 1
                    try:
                        body, mime, final = await http_request(url, max_bytes=self.MAX_PAGE_BYTES,
                            timeout=min(12, self.config.download_timeout_seconds), stop_at_media=True,
                            headers={"Referer": referer} if referer else None)
                    except MediaError as error:
                        logger.info("来源页抓取失败 %s: %s", urlsplit(url).hostname, error)
                        if failures is not None:
                            failures.append({"url": url, "reason": str(error)})
                        continue
                    chain = list(dict.fromkeys([*chain, url, final]))
                    kind = resource_kind(final, mime)
                    if not body or mime.startswith(("image/", "video/")) or mime in (
                            "application/pdf", "application/zip", "application/octet-stream"):
                        if kind == intent.kind or mime == "application/octet-stream":
                            asset = MediaAsset(kind=intent.kind, title=seed.title or intent.query,
                                url=final, source_url=referer or seed.source_url or final,
                                provider=seed.provider or "page_crawl", mime=mime,
                                metadata={"crawled": True, "referer": referer or seed.source_url,
                                          "provenance": chain, "crawl_score": 120})
                            found.setdefault(final, asset)
                        continue
                    seen.add(final)
                    parser = _Page(final, intent, seed.metadata.get("keywords", []))
                    try:
                        charset = re.search(br'charset\s*=\s*["\x27]?([a-zA-Z0-9_-]+)', body[:4096])
                        encoding = charset[1].decode("ascii") if charset else "utf-8"
                        try:
                            content = body.decode(encoding)
                        except (UnicodeError, LookupError):
                            content = body.decode("gb18030", errors="replace")
                        if mime == "application/json" or content.lstrip().startswith(("{", "[")):
                            parser.structured(json.loads(content))
                        else:
                            parser.feed(content)
                    except (ValueError, RecursionError):
                        continue
                    for item in parser.resources:
                        asset = MediaAsset(kind=intent.kind, title=item["title"] or parser.title or seed.title or intent.query,
                            url=item["url"], source_url=final, provider=seed.provider or "page_crawl",
                            width=item["width"], height=item["height"],
                            metadata={"crawled": True, "referer": final,
                                      "provenance": [*chain, item["url"]], "crawl_score": item["score"] - depth * 20})
                        if item["url"] not in found or found[item["url"]].metadata["crawl_score"] < asset.metadata["crawl_score"]:
                            found[item["url"]] = asset
                    if sum(asset.metadata["crawl_score"] >= 150 for asset in found.values()) >= min(5, intent.count):
                        break
                    if depth < self.MAX_DEPTH:
                        for child, _ in sorted(parser.links, key=lambda item: item[1], reverse=True)[:8]:
                            if child not in seen:
                                queue.append((child, seed, depth + 1, final, chain))
        except TimeoutError:
            logger.info("来源页抓取达到时间限制，使用已提取的 %d 个资源", len(found))
        assets = sorted(found.values(), key=lambda asset: asset.metadata["crawl_score"], reverse=True)
        logger.info("抓取 %d 个来源页，提取 %d 个资源", pages, len(assets))
        return assets[:limit]
