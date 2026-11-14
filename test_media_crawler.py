"""Source-page acquisition through verified search, byte validation and send receipts.

Public HTTP and QQ transports are fixtures; these tests send no QQ messages.
"""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import zipfile

from PIL import Image

from config import MediaConfig
from media_crawler import MediaCrawler
from media_downloader import MediaError, http_request
from media_jobs import MediaService
from media_models import MediaAsset, MediaIntent, MessageEnvelope
from media_processor import MediaProcessor
from media_search import MediaSearcher
from native_retrieval import NativeRetrievalResult


PAGE = "https://example.com/character"
IMAGE = "https://example.com/original.png"


def search_result(payload, *urls):
    return NativeRetrievalResult.from_trace(json.dumps(payload), [
        {"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [{"name": "search_web"}]},
        {"type": "GENERIC", "status": "DONE", "content": "\n".join(urls or [PAGE])},
    ])


class MediaCrawlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        vision = patch("visual_verification.verify_media_image", new=AsyncMock(return_value={"matched": True}))
        vision.start()
        self.addCleanup(vision.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_crawler_test_")
        self.previous = os.getcwd()
        os.chdir(self.tmp.name)
        self.config = MediaConfig()
        self.crawler = MediaCrawler(self.config)
        self.intent = MediaIntent(operation="search_send", query="角色立绘", kind="image")
        stream = io.BytesIO()
        Image.new("RGB", (300, 600), "orange").save(stream, "PNG")
        self.png = stream.getvalue()

    def tearDown(self):
        os.chdir(self.previous)
        self.tmp.cleanup()

    def seed(self, url=PAGE, kind="image"):
        return MediaAsset(kind=kind, url=url, source_url=url, provider="fixture")

    async def discover(self, content, *, kind="image"):
        intent = self.intent.model_copy(update={"kind": kind})
        with patch("media_crawler.http_request", AsyncMock(return_value=(content.encode(), "text/html", PAGE))):
            return await self.crawler.discover([self.seed(kind=kind)], intent, limit=12)

    async def test_search_only_page_is_crawled_downloaded_and_sent_with_receipt(self):
        ai = MagicMock()
        # The invented direct hint has no evidence; only the actual page may introduce it.
        ai.native_retrieve = AsyncMock(return_value=search_result({"pages": [{"url": PAGE}], "assets": [{
            "url": IMAGE, "source_url": PAGE, "local_path": "G:/qqbot/unrelated.png"}]}))
        calls = []
        async def request(url, *, target=None, headers=None, **kwargs):
            calls.append((url, target, headers))
            if target:
                self.assertEqual(url, IMAGE)
                self.assertEqual(headers["Referer"], PAGE)
                target.write_bytes(self.png)
                return b"", "image/png", url
            self.assertEqual(url, PAGE)
            return b'<a href="/original.png"><img src="/preview.png" width="60"></a>', "text/html", PAGE
        service = MediaService(self.config)
        # Exercise the optional crawler pipeline explicitly, not the default
        # native agent workflow (which downloads its own artifacts).
        ctx = MessageEnvelope(transport="onebot", account_id="test", conversation_id="123", sender_id="456",
            message_id="source-page", text="搜一张角色立绘发我", send_text=AsyncMock(), ai_client=ai,
            ai_session_key="test", send_action=AsyncMock(return_value={"status": "ok", "data": {"message_id": 1}}))
        outcome = {}
        with patch("media_crawler.http_request", request), patch("media_downloader.http_request", request):
            discovered = await service.searcher.search(self.intent, ai_client=ai, session_key="test")
            service.config.search_provider = "auto"
            service.searcher.search = AsyncMock(return_value=discovered)
            await service.handle(ctx, self.intent, outcome=outcome)
        self.assertTrue(outcome["success"])
        self.assertEqual(outcome["accepted"], 1)
        self.assertFalse(ai.native_retrieve.await_args.kwargs["require_page"])
        ctx.send_action.assert_awaited_once()
        self.assertEqual([url for url, _, _ in calls], [PAGE, IMAGE])
        cached = list(Path(self.config.cache_dir).glob("*.png"))
        self.assertEqual(cached[0].read_bytes(), self.png)

    async def test_direct_hint_still_crawls_source_and_prefers_discovered_original(self):
        ai = MagicMock()
        preview = "https://example.com/preview.png"
        ai.native_retrieve = AsyncMock(return_value=search_result({"assets": [{"url": preview, "source_url": PAGE}]}, PAGE, preview))
        with patch("media_crawler.http_request", AsyncMock(return_value=(b'<img data-original="/original.png">', "text/html", PAGE))) as request:
            assets = await MediaSearcher(self.config).search(self.intent, ai_client=ai, session_key="test")
        request.assert_awaited_once()
        self.assertEqual(assets[0].url, IMAGE)
        self.assertEqual(assets[1].url, preview)

    async def test_original_tool_source_is_preferred_over_selected_citation_redirect(self):
        ai = MagicMock()
        redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/opaque"
        ai.native_retrieve = AsyncMock(return_value=search_result({"pages": [{"url": redirect}]}, redirect, PAGE))
        async def request(url, **kwargs):
            self.assertEqual(url, PAGE)
            return b'<div class="stand"><img src="/original.png"></div>', "text/html", PAGE
        with patch("media_crawler.http_request", AsyncMock(side_effect=request)) as request:
            assets = await MediaSearcher(self.config).search(self.intent, ai_client=ai, session_key="test")
        request.assert_awaited_once()
        self.assertEqual(assets[0].url, IMAGE)

    async def test_gallery_follows_detail_and_resolves_base_and_relative_original(self):
        detail = "https://example.com/gallery/detail/1"
        pages = {PAGE: b'<a href="/gallery/detail/1"><img src="/thumb.png" width="50"></a>',
                 detail: b'<base href="https://cdn.example.org/art/"><img data-original="../original.png" alt="character">'}
        async def request(url, **kwargs):
            return pages[url], "text/html", url
        with patch("media_crawler.http_request", request):
            assets = await self.crawler.discover([self.seed()], self.intent)
        self.assertEqual(assets[0].url, "https://cdn.example.org/original.png")
        self.assertEqual(assets[0].source_url, detail)
        self.assertEqual(assets[0].metadata["referer"], detail)
        self.assertEqual(assets[0].metadata["provenance"], [PAGE, detail, assets[0].url])

    async def test_character_art_beats_ui_images_and_navigation_cannot_change_works(self):
        page = "https://example.com/game/character.html"
        content = b'''<header><img src="/header.png"></header>
            <link rel="shortcut icon" href="/favicon.gif"><meta property="og:image" content="/card.jpg">
            <div class="chara_navi_area"><a href="#other"><img src="/icon.png"></a></div>
            <img src="/game/chara_speak_01.png"><div class="stand stand_01"><img src="/game/stand.png"></div>
            <footer><a href="/"><img src="/publisher.png"></a></footer>
            <a href="/other-game/character.html"><img src="/other-game/preview.png" width="20"></a>'''
        with patch("media_crawler.http_request", AsyncMock(return_value=(content, "text/html", page))) as request:
            assets = await self.crawler.discover([self.seed(page)], self.intent)
        request.assert_awaited_once()
        self.assertEqual(assets[0].url, "https://example.com/game/stand.png")
        self.assertEqual(len(assets), 1)
        self.assertFalse(any(any(word in a.url for word in ("header", "publisher", "icon", "other-game")) for a in assets))

    async def test_relevant_navigation_leads_to_character_page_without_returning_button_art(self):
        index = "https://example.com/game/index.html"
        detail = "https://example.com/game/character.html"
        async def request(url, **kwargs):
            body = b'<header><a href="character.html"><img src="/button.png"></a></header>' if url == index else b'<img src="/stand.png">'
            return body, "text/html", url
        with patch("media_crawler.http_request", request):
            assets = await self.crawler.discover([self.seed(index)], self.intent)
        self.assertEqual([asset.url for asset in assets], ["https://example.com/stand.png"])

    async def test_original_name_hints_follow_character_detail_and_reject_cover_and_screenshots(self):
        index = "https://example.com/v123"
        detail = "https://example.com/c456"
        seed = self.seed(index)
        seed.metadata["keywords"] = ["玉樹桜", "Tamaki Sakura"]
        pages = {index: '''<meta property="og:image" content="/cv/cover.jpg">
            <div class="vnimg"><a href="/cover.jpg"><img src="/cover-small.jpg"></a></div>
            <a href="/screen.jpg"><img src="/screen-small.jpg" alt="Screenshot"></a>
            <a href="/c456" title="玉樹 桜">Tamaki Sakura</a><a href="/c789">Other Person</a>''',
            detail: '<img src="/character.png" alt="Tamaki Sakura">'}
        async def request(url, **kwargs):
            return pages[url].encode(), "text/html", url
        with patch("media_crawler.http_request", AsyncMock(side_effect=request)) as request:
            assets = await self.crawler.discover([seed], self.intent)
        self.assertEqual([call.args[0] for call in request.await_args_list], [index, detail])
        self.assertEqual([asset.url for asset in assets], ["https://example.com/character.png"])

    async def test_srcset_lazy_original_and_metadata_are_discoverable_without_icons(self):
        assets = await self.discover('''
            <img src="/logo.png"><img src="/tiny.png" width="24" height="24">
            <img src="/placeholder.gif" data-src="/lazy.png">
            <img src="/small.png" srcset="/medium.png 400w, /large.png 1600w" width="40">
            <img src="/preview.png" data-original="//cdn.example.org/full.png">
            <meta property="og:image" content="/social.png">
        ''')
        urls = {asset.url for asset in assets}
        self.assertEqual(urls, {"https://example.com/lazy.png", "https://example.com/large.png",
                                "https://cdn.example.org/full.png", "https://example.com/social.png"})

    async def test_json_ld_and_embedded_player_urls(self):
        images = await self.discover('''<script type="application/ld+json">
            {"@type":"ImageObject","contentUrl":"/render?id=1","name":"角色立绘","width":1000,"height":2000}
            </script>''')
        self.assertEqual(images[0].url, "https://example.com/render?id=1")
        self.assertEqual((images[0].width, images[0].height), (1000, 2000))
        videos = await self.discover('''<script>window.player = {"video_url":"https:\\/\\/cdn.example.org\\/clip.mp4?x=1\\u0026y=2"};</script>''', kind="video")
        self.assertEqual(videos[0].url, "https://cdn.example.org/clip.mp4?x=1&y=2")

    async def test_html5_video_and_document_download_links(self):
        videos = await self.discover('<video poster="/poster.png"><source src="/movie.webm" type="video/webm"></video>', kind="video")
        self.assertEqual([asset.url for asset in videos], ["https://example.com/movie.webm"])
        files = await self.discover('<a href="/manual.pdf">手册</a><a download href="/download?id=2">下载 ZIP</a>', kind="file")
        self.assertEqual({asset.url for asset in files}, {"https://example.com/manual.pdf", "https://example.com/download?id=2"})

    async def test_json_gallery_state_and_public_api_response(self):
        state = {"props": {"images": [{"original": {"url": "https://cdn.example.org/art.png"}, "width": 900, "height": 1200}]}}
        with patch("media_crawler.http_request", AsyncMock(return_value=(json.dumps(state).encode(), "application/json", PAGE))):
            assets = await self.crawler.discover([self.seed()], self.intent)
        self.assertEqual(assets[0].url, "https://cdn.example.org/art.png")

    async def test_rejected_source_tries_next_and_private_resource_urls_are_ignored(self):
        async def request(url, **kwargs):
            if url == PAGE:
                raise MediaError("HTTP 403")
            return b'<img src="http://127.0.0.1/private.png"><img src="file:///secret.png"><img src="/original.png">', "text/html", url
        with patch("media_crawler.http_request", request):
            assets = await self.crawler.discover([self.seed(), self.seed("https://example.com/next")], self.intent)
        self.assertEqual([asset.url for asset in assets], [IMAGE])
        self.assertEqual(assets[0].source_url, "https://example.com/next")

    async def test_search_without_structured_pages_uses_actual_search_evidence(self):
        ai = MagicMock()
        ai.native_retrieve = AsyncMock(return_value=search_result({"assets": [], "reason": "没有直链"}))
        with patch("media_crawler.http_request", AsyncMock(return_value=(b'<img src="/original.png">', "text/html", PAGE))):
            assets = await MediaSearcher(self.config).search(self.intent, ai_client=ai, session_key="test")
        self.assertEqual(assets[0].url, IMAGE)

    async def test_invented_source_never_causes_a_request(self):
        ai = MagicMock()
        ai.native_retrieve = AsyncMock(return_value=search_result({"pages": [{"url": "https://invented.example/guess"}]}))
        with patch("media_crawler.http_request", AsyncMock(return_value=(b'<img src="/original.png">', "text/html", PAGE))) as request:
            assets = await MediaSearcher(self.config).search(self.intent, ai_client=ai, session_key="test")
        self.assertEqual(request.await_args.args[0], PAGE)
        self.assertEqual(assets[0].url, IMAGE)

    async def test_failed_sources_trigger_another_real_search_for_alternative_pages(self):
        ai = MagicMock()
        other = "https://example.org/character"
        ai.native_retrieve = AsyncMock(side_effect=[search_result({"pages": [{"url": PAGE}]}),
            search_result({"pages": [{"url": other}]}, other)])
        async def request(url, **kwargs):
            if url == PAGE:
                raise MediaError("HTTP 403")
            return b'<img src="/original.png">', "text/html", other
        with patch("media_crawler.http_request", request):
            assets = await MediaSearcher(self.config).search(self.intent, ai_client=ai, session_key="test")
        self.assertEqual(ai.native_retrieve.await_count, 2)
        self.assertIn("更换检索词", ai.native_retrieve.await_args.args[1])
        self.assertEqual(assets[0].url, "https://example.org/original.png")

    async def test_explicit_page_download_retries_invalid_media_then_validates_actual_bytes(self):
        processor = MediaProcessor(self.config)
        async def request(url, *, target=None, **kwargs):
            if target:
                target.write_bytes(b"<html>not an image</html>" if "broken" in url else self.png)
                return b"", "image/png", url
            return b'<img data-original="/broken.png"><img data-original="/original.png">', "text/html", PAGE
        with patch("media_crawler.http_request", request), patch("media_downloader.http_request", request):
            asset = await processor.prepare(self.seed())
        self.assertEqual(asset.url, IMAGE)
        self.assertEqual(asset.mime, "image/png")
        self.assertEqual((asset.width, asset.height), (300, 600))
        self.assertEqual(len(list(processor.cache.iterdir())), 1)
        self.assertFalse(processor.active_paths)

    async def test_explicit_video_page_downloads_resource_instead_of_returning_page(self):
        processor = MediaProcessor(self.config)
        async def request(url, *, target=None, **kwargs):
            if target:
                target.write_bytes(b"\0\0\0\x18ftypisom" + b"\0" * 30)
                return b"", "video/mp4", url
            return b'<video src="/clip.mp4"></video>', "text/html", PAGE
        with patch("media_crawler.http_request", request), patch("media_downloader.http_request", request):
            # As-file avoids requiring ffmpeg for a synthetic fixture container.
            asset = await processor.prepare(self.seed(kind="video"), force_file=True)
        self.assertEqual(asset.url, "https://example.com/clip.mp4")
        self.assertTrue(Path(asset.local_path).is_file())

    async def test_stale_direct_link_recovers_by_crawling_its_source_page(self):
        processor = MediaProcessor(self.config)
        asset = MediaAsset(kind="image", url="https://example.com/stale.png", source_url=PAGE)
        async def request(url, *, target=None, **kwargs):
            if "stale" in url:
                raise MediaError("HTTP 404")
            if target:
                target.write_bytes(self.png)
                return b"", "image/png", url
            self.assertEqual(url, PAGE)
            return b'<img data-src="/original.png">', "text/html", PAGE
        with patch("media_crawler.http_request", request), patch("media_downloader.http_request", request):
            ready = await processor.prepare(asset)
        self.assertEqual(ready.url, IMAGE)
        self.assertEqual(Path(ready.local_path).read_bytes(), self.png)

    async def test_document_page_downloads_extensionless_office_file_with_correct_extension(self):
        processor = MediaProcessor(self.config)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", "<document/>")
        body = buffer.getvalue()
        async def request(url, *, target=None, **kwargs):
            if target:
                target.write_bytes(body)
                return b"", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", url
            return b'<a download href="/download?id=1">Manual</a>', "text/html", PAGE
        with patch("media_crawler.http_request", request), patch("media_downloader.http_request", request):
            asset = await processor.prepare(self.seed(kind="file"))
        self.assertEqual(asset.url, "https://example.com/download?id=1")
        self.assertEqual(Path(asset.local_path).suffix, ".docx")
        self.assertEqual(Path(asset.local_path).read_bytes(), body)

    async def test_cancellation_propagates_and_crawl_respects_page_limit(self):
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch("media_crawler.http_request", blocked):
            task = asyncio.create_task(self.crawler.discover([self.seed()], self.intent))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        async def page(url, **kwargs):
            body = "".join(f'<a href="/gallery/{i}"><img src="/thumb.png" width="20"></a>' for i in range(100))
            return body.encode(), "text/html", url
        with patch("media_crawler.http_request", AsyncMock(side_effect=page)) as request:
            await self.crawler.discover([self.seed()], self.intent)
        self.assertLessEqual(request.await_count, self.crawler.MAX_PAGES)

    async def test_source_redirect_can_end_at_large_original_without_reading_it_as_html(self):
        with patch("media_crawler.http_request", AsyncMock(return_value=(b"", "image/png", IMAGE))):
            assets = await self.crawler.discover([self.seed()], self.intent)
        self.assertEqual(assets[0].url, IMAGE)
        self.assertEqual(assets[0].source_url, PAGE)
        self.assertEqual(assets[0].metadata["provenance"], [PAGE, IMAGE])

    async def test_http_probe_closes_large_media_before_size_limit_but_download_still_enforces_it(self):
        class Response:
            status = 200
            content_length = 100000000
            headers = {"Content-Type": "video/mp4"}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
        class Session:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            def request(self, *args, **kwargs):
                return Response()
        with patch("media_downloader.aiohttp.TCPConnector"), patch("media_downloader.aiohttp.ClientSession", Session):
            self.assertEqual(await http_request(IMAGE, max_bytes=1024, stop_at_media=True), (b"", "video/mp4", IMAGE))
            with self.assertRaises(MediaError):
                await http_request(IMAGE, max_bytes=1024, target=Path("clip.mp4"))


if __name__ == "__main__":
    unittest.main()
