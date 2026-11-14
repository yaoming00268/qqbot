"""Native Antigravity retrieval and explicitly selected public media providers."""
from __future__ import annotations

import asyncio
import html
from html.parser import HTMLParser
import json
import os
import re
import time
from pathlib import Path
import urllib.parse

from media_downloader import get_json, http_request, MediaError, public_url
from media_crawler import MediaCrawler, resource_kind
from media_models import MediaAsset, MediaIntent


COMMON_TERMS = {"橘猫": "orange cat", "猫咪": "cat", "猫猫": "cat", "猫": "cat", "狗狗": "dog",
                "柴犬": "shiba inu", "星空": "starry sky", "风景": "landscape", "海边": "beach", "大海": "sea",
                "日落": "sunset", "日出": "sunrise", "可爱": "cute", "生气": "angry", "二次元": "anime",
                "手机": "", "壁纸": "wallpaper", "表情包": "meme", "头像": "avatar", "熊猫": "panda", "樱花": "cherry blossom"}


def plain(value):
    return html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()[:400]


class _SearchCards(HTMLParser):
    """Only parse structured search-result cards, never arbitrary page images."""
    def __init__(self, kind):
        super().__init__(convert_charrefs=True)
        self.kind, self.cards = kind, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        field = "m" if self.kind == "image" and tag == "a" else "vrhm" if self.kind == "video" and tag == "div" else ""
        if field and attrs.get(field) and len(self.cards) < 100:
            try:
                data = json.loads(attrs[field])
                if isinstance(data, dict) and data.get("murl"):
                    murl = str(data.get("murl", "")).strip()
                    if self.kind == "image":
                        lower_path = urllib.parse.urlsplit(murl).path.lower()
                        if lower_path.endswith((".html", ".htm", ".php", ".jsp", ".asp", ".aspx")):
                            return
                    self.cards.append(data)
            except (ValueError, TypeError):
                pass


class MediaSearcher:
    def __init__(self, config):
        self.config = config
        self.crawler = MediaCrawler(config)

    async def search(self, intent: MediaIntent, *, page=0, ai_client=None, session_key=""):
        if not intent.query.strip():
            raise MediaError("请提供搜索关键词，例如“搜三张橘猫图片发我”。")
        if self.config.search_provider == "native":
            return await self.native(intent, page=page, ai_client=ai_client, session_key=session_key)
        if (intent.kind == "file" and re.search(r"python", intent.query, re.I) and "pdf" in intent.query.lower()
                and ("官方" in intent.query or "official" in intent.query.lower())):
            return await self.python_docs()
        if self.config.search_api_url:
            try:
                assets = await self.custom(intent, page)
                if assets:
                    return self.filter(assets, intent)
            except MediaError:
                pass
        if self.config.search_provider == "auto" and intent.kind in ("image", "video"):
            try:
                assets = self.filter(await self.bing(intent, page), intent)
                if assets:
                    return assets
            except MediaError:
                pass
        return self.filter(await self.commons(intent, page), intent)

    async def native(self, intent, *, page, ai_client, session_key):
        if ai_client is None or not session_key:
            raise MediaError("原生检索缺少反重力会话连接，未使用工作区文件代替搜索结果。")
        request = {
            "query": intent.query, "kind": intent.kind, "count": intent.count,
            "page": page, "orientation": intent.orientation, "aspect_ratio": intent.aspect_ratio,
            "max_duration_seconds": intent.max_duration,
        }
        task = (
            "搜索下面请求的网络媒体，优先寻找匹配作品、人物或主题的来源页、图库、详情页、下载页。"
            "按需要搜索并核对来源，不要急于给出第一个候选。"
            "充分检索并返回相关来源网页，此接口用于兼容公共来源页解析，"
            "不要求搜索结果直接给出媒体直链。原生网页/浏览器可用时可以辅助核对；不可用时仍返回搜索找到的来源网页。"
            "不要因为缺少 read_url_content 或浏览器而返回空列表。优先官方、原始发布页，避免教程截图及不相关配图。"
            "若请求角色立绘，优先该角色的官方详情页，作品首页的封面、商店图和游戏截图不算立绘。"
            "中文检索不足时，实际改用作品及角色的日文/英文原名继续搜索；在 keywords 中提供这些名字以便页面匹配。"
            "搜索工具返回的引用重定向 URL 也可作为来源；不要为了获得网站原始地址而反复搜索。"
            '返回 {"keywords":["角色或主体的原文名/英文名"],"pages":[{"title":"页面名称","url":"来源网页 URL"}],'
            '"assets":[{"title":"资源名称","url":"可选已发现的媒体直链","source_url":"来源网页 URL"}],"reason":""}。'
            "此处返回的 URL 必须来自本轮真实网络工具结果，不要猜测地址，不要返回本地路径。\n请求数据："
            + json.dumps(request, ensure_ascii=False)
        )
        deadline = time.monotonic() + self.config.task_timeout_seconds * 0.85 if self.config.task_timeout_seconds else float("inf")
        feedback = ""
        seeds = []
        for _ in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                break
            result = await ai_client.native_retrieve(session_key, task + feedback,
                timeout=remaining * 0.65 if self.config.task_timeout_seconds else 0, require_page=False)
            assets, seeds = self.native_candidates(result, intent)
            if seeds:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    try:
                        async with asyncio.timeout(remaining):
                            assets = [*await self.crawler.discover(seeds, intent, limit=12), *assets]
                    except TimeoutError:
                        pass
            filtered = self.filter(assets, intent)
            if filtered:
                return filtered
            feedback = (
                "\n上一批来源页实际抓取后未获得可下载资源。请重新调用 search_web，更换检索词或网站，"
                "可用作品/人物的原文名称搜索官方角色详情页、图鉴和原始发布页；不要重复上一批链接或返回作品封面。"
                "此前页面：" + json.dumps([seed.title for seed in seeds], ensure_ascii=False)
            )
        if intent.kind == "video" and seeds:
            return self.filter([seed.model_copy(update={"url": "", "metadata": {"page_candidate": True}})
                                for seed in seeds], intent)
        raise MediaError("已搜索并抓取来源页，未能提取可下载的匹配资源，请换个关键词或来源页。")

    @staticmethod
    def native_candidates(result, intent):
        from native_retrieval import json_result
        result.verify(require_page=False)
        try:
            payload = json_result(result.text)
        except MediaError:
            payload = {}
        assets, seeds, seen = [], [], set()
        hints = payload.get("keywords", [])
        keywords = [value.strip()[:100] for value in hints[:8] if isinstance(value, str) and value.strip()] if isinstance(hints, list) else []

        def page(value, title=""):
            try:
                url = result.require_url(value)
            except MediaError:
                return
            if url not in seen:
                seeds.append(MediaAsset(kind=intent.kind, title=plain(title or intent.query),
                    url=url, source_url=url, provider="antigravity", metadata={"keywords": keywords}))
                seen.add(url)

        entries = payload.get("assets", [])
        for entry in entries[:20] if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            title = plain(entry.get("title") or intent.query)
            page(entry.get("source_url"), title)
            try:
                url = result.require_url(entry.get("url", ""))
                source = result.require_url(entry.get("source_url") or url)
                if resource_kind(url) == intent.kind:
                    assets.append(MediaAsset(kind=intent.kind, title=title,
                        url=url, source_url=source, provider="antigravity", metadata={"referer": source}))
                else:
                    page(url, title)
            except MediaError:
                continue
        for field in ("pages", "sources"):
            entries = payload.get(field, [])
            for entry in entries[:12] if isinstance(entries, list) else []:
                if isinstance(entry, dict):
                    page(entry.get("url") or entry.get("source_url"), entry.get("title"))
                elif isinstance(entry, str):
                    page(entry)
        # Search summaries sometimes contain the original page address while
        # the model selects only opaque citation redirects. Both are evidence;
        # try the original page before spending the crawl budget on redirects.
        for url in sorted(result.urls):
            if (not resource_kind(url) and "vertexaisearch.cloud.google.com/grounding-api-redirect/" not in url
                    and not re.search(r"(?:google\.[^/]+/search|bing\.com/(?:images/|videos/)?search)", url)):
                page(url)
        # Even a model that says "no direct URLs" may have found useful pages.
        if not seeds and not assets:
            for url in sorted(result.urls)[:8]:
                if not re.search(r"(?:google\.[^/]+/search|bing\.com/(?:images/|videos/)?search)", url):
                    page(url)
        seeds.sort(key=lambda seed: "vertexaisearch.cloud.google.com/grounding-api-redirect/" in seed.url)
        return assets, seeds

    @staticmethod
    def matches_dimensions(asset, intent):
        if not (intent.orientation or intent.aspect_ratio):
            return True
        if not asset.width or not asset.height:
            return True
        if intent.orientation == "portrait" and asset.width > asset.height * 1.25:
            return False
        if intent.orientation == "landscape" and asset.height > asset.width * 1.25:
            return False
        if intent.aspect_ratio:
            x, y = map(int, intent.aspect_ratio.split(":"))
            if abs(asset.width / asset.height - x / y) > 0.15:
                return False
        return True

    @staticmethod
    def filter(assets, intent):
        unique, seen = [], set()
        for asset in assets:
            key = asset.url or asset.source_url
            if not key or key in seen:
                continue
            try:
                if asset.url:
                    asset.url = public_url(asset.url)
                if asset.source_url:
                    asset.source_url = public_url(asset.source_url)
            except MediaError:
                continue
            if intent.max_duration and asset.duration and asset.duration > intent.max_duration:
                continue
            if asset.width and asset.height and not MediaSearcher.matches_dimensions(asset, intent):
                continue
            seen.add(key)
            unique.append(asset)
        return unique[:5]

    async def custom(self, intent, page):
        separator = "&" if "?" in self.config.search_api_url else "?"
        url = self.config.search_api_url + separator + urllib.parse.urlencode({
            "q": intent.query, "kind": intent.kind, "limit": 5, "offset": page * 5})
        headers = {}
        key = os.environ.get(self.config.search_api_key_env, "")
        if key:
            headers["Authorization"] = "Bearer " + key
        payload = await get_json(url, headers=headers)
        if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
            raise MediaError("搜索服务返回了无效的结果列表。")
        entries = payload.get("results", [])
        assets = []
        for entry in entries[:50]:
            if not isinstance(entry, dict):
                continue
            # Never trust a provider's local path or arbitrary RPC metadata.
            try:
                assets.append(MediaAsset(kind=intent.kind, title=plain(entry.get("title")), url=str(entry.get("url", "")),
                    source_url=str(entry.get("source_url", "")), provider="search_api", mime=str(entry.get("mime", "")),
                    width=max(0, int(entry.get("width", 0) or 0)), height=max(0, int(entry.get("height", 0) or 0)),
                    duration=max(0, float(entry.get("duration", 0) or 0)), author=plain(entry.get("author")),
                    license=plain(entry.get("license"))))
            except (ValueError, TypeError):
                continue
        return assets

    async def bing(self, intent, page):
        category = "images" if intent.kind == "image" else "videos"
        params = {"q": intent.query, "first": page * 20 + 1, "count": 20, "adlt": "strict"}
        if intent.kind == "video" and intent.max_duration:
            params["qft"] = "+filterui:duration-short"
        if intent.kind == "image" and intent.orientation:
            params["qft"] = "+filterui:aspect-" + ("tall" if intent.orientation == "portrait" else "wide")
        body, _, _ = await http_request("https://cn.bing.com/" + category + "/search?" + urllib.parse.urlencode(params),
                                       max_bytes=5 * 1024 * 1024, timeout=20)
        parser = _SearchCards(intent.kind)
        parser.feed(body.decode("utf-8", errors="replace"))
        assets = []
        for card in parser.cards:
            media = str(card.get("murl", ""))
            source = str(card.get("purl") or card.get("pgurl") or media)
            duration = 0
            if intent.kind == "video":
                try:
                    for part in str(card.get("du", "0")).split(":"):
                        duration = duration * 60 + int(part)
                except ValueError:
                    duration = 0
                # Watch-page URLs are candidate links, never downloadable MP4s.
                if not re.search(r"\.(?:mp4|webm|ogv|mov)(?:\?|$)", media, re.I):
                    media = ""
            assets.append(MediaAsset(kind=intent.kind, title=plain(card.get("t") or card.get("vt") or intent.query),
                url=media, source_url=source, provider="bing", thumbnail=str(card.get("turl", "")), duration=duration))
        return assets

    async def commons(self, intent, page):
        query = intent.query
        for chinese, english in COMMON_TERMS.items():
            query = query.replace(chinese, " " + english + " ")
        filetype = {"image": "bitmap", "video": "video", "file": "office"}[intent.kind]
        if intent.kind == "file" and "pdf" in query.lower():
            query = re.sub(r"pdf", "", query, flags=re.I) + " filemime:application/pdf"
            filter_term = ""
        else:
            filter_term = " filetype:" + filetype
        params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                  "gsrsearch": re.sub(r"\s+", " ", query).strip() + filter_term, "gsrlimit": 20,
                  "gsroffset": page * 20, "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata|metadata",
                  "iiurlwidth": 960, "uselang": "zh"}
        payload = await get_json(self.config.commons_api_url + "?" + urllib.parse.urlencode(params))
        assets = []
        for entry in sorted(payload.get("query", {}).get("pages", {}).values(), key=lambda e: e.get("index", 0)):
            info = (entry.get("imageinfo") or [{}])[0]
            mime = info.get("mime", "")
            if intent.kind == "image" and mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                continue
            if intent.kind == "video" and not (mime.startswith("video/") or mime in ("application/ogg",)):
                continue
            if intent.kind == "file" and mime not in ("application/pdf", "application/zip", "text/plain"):
                continue
            ext = info.get("extmetadata", {})
            metadata = {item["name"]: item.get("value") for item in info.get("metadata", []) if isinstance(item, dict) and "name" in item}
            try:
                duration = float(metadata.get("length", metadata.get("playtime_seconds", 0)) or 0)
            except (ValueError, TypeError):
                duration = 0
            assets.append(MediaAsset(kind=intent.kind, title=plain(entry.get("title", "")).removeprefix("File:"),
                url=info.get("url", ""), source_url=info.get("descriptionurl", ""), provider="wikimedia",
                thumbnail=info.get("thumburl", ""), mime=mime, size=info.get("size", 0),
                width=info.get("width", 0), height=info.get("height", 0), duration=duration,
                author=plain(ext.get("Artist", {}).get("value")), license=plain(ext.get("LicenseShortName", {}).get("value"))))
        return assets

    async def python_docs(self):
        for version in ("3", "3.13", "3.12"):
            source = f"https://docs.python.org/{version}/download.html"
            payload, _, _ = await http_request(source, max_bytes=1024 * 1024, timeout=20)
            links = re.findall(r'href=["\']([^"\']+-docs-pdf-a4\.zip)["\']', payload.decode("utf-8", errors="replace"))
            if links:
                return [MediaAsset(kind="file", title=f"Python {version} 官方教程.pdf", url=urllib.parse.urljoin(source, html.unescape(links[0])),
                    source_url=source, provider="python_docs", metadata={"extract_pdf": "tutorial.pdf", "version": version})]
        raise MediaError("Python 官网暂未提供可获取的 PDF 文档包，请查看 https://docs.python.org/3/download.html 。")

    def library(self, intent, *, page=0):
        assets = []
        def normalized(value):
            return value.lower().replace("猫猫", "猫").replace("猫咪", "猫").replace("狗狗", "狗")
        words = [normalized(w) for w in intent.query.split() if w]
        inspected = 0
        for root in self.config.local_library_roots:
            directory = Path(root).expanduser().resolve()
            if not directory.is_dir():
                continue
            for path in directory.rglob("*"):
                inspected += 1
                if inspected > 10000 or len(assets) >= 1000:
                    break
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(directory):
                    continue
                if words and not all(w in normalized(path.stem) for w in words):
                    continue
                suffix = path.suffix.lower()
                kind = "image" if suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp") else "video" if suffix in (".mp4", ".webm", ".ogv", ".mov") else "file"
                if intent.kind != "file" and kind != intent.kind:
                    continue
                assets.append(MediaAsset(kind=kind, title=path.name, provider="library", local_path=str(path),
                                         size=path.stat().st_size, metadata={"mtime": path.stat().st_mtime}))
        assets.sort(key=lambda a: a.metadata.get("mtime", 0), reverse=True)
        return assets[page * 5:(page + 1) * 5]
