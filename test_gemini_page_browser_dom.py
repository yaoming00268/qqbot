"""Offline Chromium DOM regression coverage for the Gemini page transport."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit as real_urlsplit

from browser_control import BrowserBridge, installed_browser
from media_downloader import MediaError
from testing_sandbox import own_browser_service, playwright_fixture_driver
import gemini_page_browser as browser

PAGE = b'''<!doctype html><html><body>
<rich-textarea><div role="textbox" contenteditable="true"></div></rich-textarea>
<button aria-label="Add files" onclick="document.querySelector('#file').click()">Add files</button>
<input id="file" type="file" hidden onchange="upload(this)">
<div id="attachments"></div><button id="send" aria-label="Send" onclick="send()">Send</button>
<div id="messages"></div><div id="site"></div>
<script>
window.clicks=0; window.sentBeforeReady=false; window.ready=false;
function upload(input) { document.querySelector('#send').disabled=true;
  setTimeout(()=>{let chip=document.createElement('button');chip.textContent='Remove '+input.files[0].name;
    document.querySelector('#attachments').appendChild(chip);window.ready=true;document.querySelector('#send').disabled=false;}, 500); }
function send() { window.clicks++; if(document.querySelector('#file').files.length && !window.ready) window.sentBeforeReady=true;
 let response=document.createElement('model-response');response.innerHTML='<div class="markdown">Fixture answer</div>';
 document.querySelector('#messages').appendChild(response); }
</script></body></html>'''


class PageDOMTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="qqbot-gemini-dom-", ignore_cleanup_errors=True)
        cls.addClassCleanup(cls.temp.cleanup)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                if self.path == "/fixture.mp4":
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Disposition", "attachment; filename=fixture.mp4")
                    self.end_headers()
                    self.wfile.write(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00isommp42\x00\x00\x00\x08free")
                else:
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(PAGE.replace(b'<button id="send"', b'<button disabled id="send"') if self.path == "/hold" else PAGE)
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/app"
        profile = Path(cls.temp.name) / "profile"
        try:
            executable = installed_browser()
        except Exception as error:
            raise unittest.SkipTest(f"Chrome/Edge unavailable: {type(error).__name__}")
        cls.process = subprocess.Popen([str(executable), "--headless=new", "--disable-gpu",
            "--disable-background-networking", "--disable-component-update", "--disable-sync",
            "--no-pings", "--no-first-run", "--no-default-browser-check",
            "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
            "--user-data-dir=" + str(profile), cls.url], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        cls.addClassCleanup(cls.close_browser)
        own_browser_service(cls.process, profile)
        cls.bridge = BrowserBridge({"profile_path": str(profile)})
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                if cls.bridge.pages():
                    return
            except ValueError:
                pass
            time.sleep(.05)
        raise RuntimeError("Fixture browser did not start")

    @classmethod
    def close_browser(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)

    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.guard = playwright_fixture_driver()
        self.guard.__enter__()
        self.driver = await async_playwright().start()
        self.connection = await self.driver.chromium.connect_over_cdp(self.bridge.endpoint())
        self.page = await self.connection.contexts[0].new_page()
        self.page.set_default_timeout(4000)
        await self.page.goto(self.url)
        self.case = tempfile.TemporaryDirectory(prefix="qqbot-gemini-case-")
        self.config = SimpleNamespace(timeout_seconds=12)
        self.page_patch = patch.object(browser, "_page", self.fixture_page)
        self.page_patch.start()
        self.url_patch = patch.object(browser, "urlsplit", self.fixture_urlsplit)
        self.url_patch.start()

    async def asyncTearDown(self):
        self.url_patch.stop()
        self.page_patch.stop()
        await self.page.close()
        await self.connection.close()
        await self.driver.stop()
        self.guard.__exit__(None, None, None)
        self.case.cleanup()

    @asynccontextmanager
    async def fixture_page(self, config):
        yield self.page

    def fixture_urlsplit(self, value):
        if str(value).startswith(self.url.rsplit("/", 1)[0]):
            return real_urlsplit("https://gemini.google.com/app")
        return real_urlsplit(value)

    async def test_upload_waits_until_ready_then_sends_once(self):
        attachment = Path(self.case.name) / "reference.txt"
        attachment.write_text("fixture", encoding="utf-8")
        result = await browser.generate_page_content(self.config, "Describe attachment", files=[attachment])
        self.assertIn("Fixture answer", result.text)
        self.assertEqual(await self.page.evaluate("window.clicks"), 1)
        self.assertFalse(await self.page.evaluate("window.sentBeforeReady"))
        self.assertTrue(await self.page.evaluate("window.ready"))

    async def test_message_words_do_not_trigger_site_checks(self):
        await self.page.locator("#messages").evaluate("e => e.innerHTML = '<model-response>usage limit captcha 验证码</model-response>'")
        await browser._check_state(self.page)

    async def test_visible_limit_and_verification_stop(self):
        for text in ("Usage limit reached", "Verify you are human"):
            with self.subTest(text=text):
                await self.page.locator("#site").evaluate("(e,t) => e.innerHTML = '<div role=alert>'+t+'</div>'", text)
                with self.assertRaises(MediaError):
                    await browser._check_state(self.page)

    async def test_cancellation_stops_before_submission(self):
        attachment = Path(self.case.name) / "reference.txt"
        attachment.write_text("fixture", encoding="utf-8")
        await self.page.evaluate("document.querySelector('#send').disabled=true")
        task = asyncio.create_task(browser.generate_page_content(self.config, "fixture"))
        await asyncio.sleep(.25)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await self.page.evaluate("window.clicks"), 0)

    async def test_video_share_download_uses_response_menu(self):
        await self.page.locator("#messages").evaluate("""e => e.innerHTML = `
          <model-response><video></video><button aria-label="Share video" onclick="document.querySelector('#share-menu').hidden=false">Share video</button></model-response>
          <div id="share-menu" role="menu" hidden><button role="menuitem" onclick="window.publishClicks++">Publish</button>
          <a role="menuitem" href="/fixture.mp4" download="fixture.mp4">Download video</a></div>`""")
        await self.page.evaluate("window.publishClicks=0")
        response = self.page.locator("model-response")
        files = await browser._download(response, "video", Path(self.case.name))
        self.assertEqual(len(files), 1)
        self.assertEqual(Path(files[0]).resolve().parent, Path(self.case.name).resolve())
        self.assertTrue(Path(files[0]).is_file())
        self.assertEqual(await self.page.evaluate("window.publishClicks"), 0)

    async def test_dedicated_video_entry_with_reference(self):
        await self.page.locator("body").evaluate("""e => e.innerHTML = `
          <nav><button onclick="openVideo()">Create video</button></nav>
          <div id="workspace"><rich-textarea><div role="textbox" contenteditable="true"></div></rich-textarea>
          <button aria-label="Send">Send</button></div><div id="messages"></div>`""")
        await self.page.evaluate("""() => {
          window.clicks=0; window.publishClicks=0;
          window.openVideo=() => { document.querySelector('#workspace').innerHTML = `
            <form><textarea aria-label="Video prompt"></textarea><button type="button" aria-label="Add image" onclick="document.querySelector('#video-file').click()">Add image</button>
            <input id="video-file" type="file" hidden onchange="videoUpload(this)"><div id="attachments"></div>
            <button id="submit-video" type="button" onclick="videoSubmit()">Submit</button></form>`; };
          window.videoUpload=(input) => { const chip=document.createElement('button'); chip.type='button';
            chip.textContent='Remove '+input.files[0].name; document.querySelector('#attachments').appendChild(chip); };
          window.videoSubmit=() => {window.clicks++; document.querySelector('#messages').innerHTML = `
            <model-response><video-generation-result><video></video><button aria-label="Share video" onclick="document.querySelector('#share-menu').hidden=false">Share video</button></video-generation-result></model-response>
            <div id="share-menu" role="menu" hidden><button role="menuitem" onclick="window.publishClicks++">Publish</button>
            <a role="menuitem" href="/fixture.mp4" download="fixture.mp4">Download video</a></div>`; };
        }""")
        attachment = Path(self.case.name) / "reference.png"
        attachment.write_bytes(b"fixture")
        result = await browser.generate_page_content(self.config, "Animate reference", files=[attachment],
            media_kind="video", directory=Path(self.case.name))
        self.assertEqual(await self.page.evaluate("window.clicks"), 1)
        self.assertEqual(await self.page.evaluate("window.publishClicks"), 0)
        self.assertEqual(len(result.videos), 1)
        self.assertEqual(Path(result.videos[0]).resolve().parent, Path(self.case.name).resolve())

    async def test_cancellation_closes_owned_context(self):
        self.page_patch.stop()
        created = []
        connection = self.connection
        class Chromium:
            async def launch_persistent_context(self, *args, **kwargs):
                context = await connection.new_context(accept_downloads=True)
                created.append(context)
                return context
        @asynccontextmanager
        async def fixture_driver():
            yield SimpleNamespace(chromium=Chromium())
        config = SimpleNamespace(timeout_seconds=12, page_profile_dir=str(Path(self.case.name) / "profile"))
        with patch("playwright.async_api.async_playwright", fixture_driver), \
             patch.object(browser, "URL", self.url.rsplit("/", 1)[0] + "/hold"), \
             patch.object(browser, "_browser", return_value=Path("fixture")):
            task = asyncio.create_task(browser.generate_page_content(config, "fixture"))
            for _ in range(80):
                if created and created[0].pages:
                    page = created[0].pages[0]
                    if await page.locator("#send").count():
                        await page.evaluate("document.querySelector('#send').disabled=true")
                        break
                await asyncio.sleep(.05)
            self.assertTrue(created and created[0].pages)
            await asyncio.sleep(.2)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(created)
        self.assertTrue(page.is_closed())
