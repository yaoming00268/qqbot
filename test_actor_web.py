"""Real local website read/draft/post/comment and semantic browser control."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from agent_core.configuration import AgentConfig
from agent_core.runtime import ActorRuntime
from agent_core.web import WebAccountAdapter, allowed_url
from browser_control import BrowserBridge, installed_browser
from computer_control import ControlError, PlaywrightBridge
from testing_sandbox import own_browser_service, playwright_fixture_driver


PAGE = """<!doctype html><meta charset="utf-8"><title>Local account</title>
<h1>实际阅读的本地文章</h1><p>文章讨论现场录音与声音设计，这是作者的观点。</p>
<a href="/article">阅读文章</a><a href="https://example.invalid/">外部链接</a>
<span data-testid="identity">fixture-account</span>
<form><label for="editor">正文</label><textarea id="editor"></textarea>
<button type="submit">发布</button></form><output role="status" hidden></output>
<a id="receipt" hidden>查看发布结果</a>
<label for="category">分类</label><select id="category"><option value="music">音乐</option><option value="games">游戏</option></select>
<div contenteditable="true" aria-label="简介"></div>
<input type="password" value="PRIVATE_PASSWORD_NOT_IN_SNAPSHOT">
<script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();
const response=await fetch('/publish',{method:'POST',body:document.querySelector('textarea').value});
if(location.pathname==='/no-confirm') return;
if(response.ok){const result=await response.json();const out=document.querySelector('output');
out.hidden=false;out.textContent='已发布 '+result.id;
const a=document.querySelector('#receipt');a.hidden=false;a.href='/post/'+result.id;}
};</script>""".encode("utf-8")


class WebsiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="qqbot_actor_web_", ignore_cleanup_errors=True)
        cls.addClassCleanup(cls.temp.cleanup)
        cls.received = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(PAGE)))
                self.end_headers()
                self.wfile.write(PAGE)

            def do_POST(self):
                cls.received.append(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
                data = json.dumps({"id": len(cls.received)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.close_server)
        cls.url = "http://127.0.0.1:" + str(cls.server.server_port)
        profile = Path(cls.temp.name) / "profile"
        cls.process = subprocess.Popen([str(installed_browser()), "--headless=new", "--disable-gpu",
            "--disable-background-networking", "--disable-component-update", "--disable-sync", "--no-pings",
            "--no-first-run", "--no-default-browser-check", "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0", "--user-data-dir=" + str(profile),
            "--window-size=1000,700", "--force-device-scale-factor=1", cls.url + "/"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        cls.addClassCleanup(cls.close_browser)
        own_browser_service(cls.process, profile)
        cls.target = {"profile_path": str(profile)}
        cls.bridge = BrowserBridge(cls.target)
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
    def close_server(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    @classmethod
    def close_browser(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)

    @contextmanager
    def browser_factory(self, config, profile, timeout_ms):
        from playwright.sync_api import sync_playwright
        with playwright_fixture_driver(), sync_playwright() as driver:
            browser = driver.chromium.connect_over_cdp(self.bridge.endpoint())
            context = browser.contexts[0]
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            def restrict(route):
                if route.request.is_navigation_request():
                    try:
                        allowed_url(config, route.request.url)
                    except (ValueError, PermissionError):
                        route.abort("blockedbyclient")
                        return
                route.continue_()
            page.route("**/*", restrict)
            try:
                yield SimpleNamespace(pages=[page])
            finally:
                page.close()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(prefix="qqbot_actor_web_case_")
        self.addCleanup(self.case.cleanup)
        recipe = {"editor": {"label": "正文"}, "submit": {"role": "button", "name": "发布"},
                  "confirmation": {"role": "status"}, "result_link": {"css": "#receipt"}}
        self.config = AgentConfig.model_validate({"autonomous_model_enabled": False,
            "actors": {"alice": {"name": "Alice"}}, "accounts": {"local": {
                "actor_id": "alice", "home_url": self.url + "/", "identity": {"test_id": "identity"},
                "expected_identity": "fixture-account", "publish": recipe, "comment": recipe,
                "actions": ["read_content", "create_draft", "publish_post", "reply_comment"]}}})
        self.runtime = ActorRuntime(self.config, database_path=Path(self.case.name) / "actors.sqlite3", browser_factory=self.browser_factory)
        self.gateway = self.runtime.gateway
        self.gateway.adapters["web"].timeout_ms = 2000
        self.count_before = len(self.received)

    def execute(self, kind, payload, key):
        return self.gateway.execute("alice", "web:local", kind, payload, key)

    def draft(self, text, url="", kind="publish_post"):
        result = self.execute("create_draft", {"text": text, "target_url": url, "draft_kind": kind}, "draft:" + text)
        self.assertEqual(result["state"], "confirmed", result)
        return result["result"]["draft"]["id"]

    def test_read_draft_publish_and_comment_have_real_server_receipts(self):
        read = self.execute("read_content", {"url": self.url + "/article"}, "read")
        self.assertEqual(read["state"], "confirmed", read)
        self.assertIn("声音设计", read["result"]["text"])
        self.assertNotIn("PRIVATE_PASSWORD", json.dumps(read))
        self.assertTrue(all(link["url"].startswith(self.url) for link in read["result"]["links"]))
        self.assertTrue(all(Path(path).is_file() for path in read["result"]["evidence"]))
        draft_id = self.draft("我读到作者谈论现场录音，想进一步了解声音设计。")
        payload = {"draft_id": draft_id}
        published = self.execute("publish_post", payload, "publish")
        self.assertEqual(published["state"], "confirmed", published)
        self.assertIn("/post/", published["result"]["remote_url"])
        repeated = self.execute("publish_post", payload, "publish")
        self.assertEqual(repeated["id"], published["id"])
        blocked = self.execute("publish_post", payload, "different-key")
        self.assertEqual(blocked["state"], "failed")
        comment_id = self.draft("这个录音方法在室内也适用吗？", self.url + "/article", "reply_comment")
        comment = self.execute("reply_comment", {"draft_id": comment_id}, "comment")
        self.assertEqual(comment["state"], "confirmed", comment)
        self.assertEqual(len(self.received) - self.count_before, 2)

    def test_identity_mismatch_and_unconfigured_origin_cannot_submit(self):
        self.config.accounts["local"].expected_identity = "other-account"
        self.runtime.configure(self.config)
        draft_id = self.draft("没有登录为正确身份时不能发出")
        result = self.execute("publish_post", {"draft_id": draft_id}, "publish")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["result"]["code"], "ACCOUNT_IDENTITY_MISMATCH")
        with self.assertRaises(PermissionError):
            allowed_url(self.config.accounts["local"], "https://example.invalid/")
        result = self.execute("read_content", {"url": "https://example.invalid/"}, "outside")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(len(self.received), self.count_before)

    def test_missing_confirmation_after_accepted_post_never_replays_even_with_new_key(self):
        draft_id = self.draft("服务端收到了，但前端确认丢失", self.url + "/no-confirm")
        result = self.execute("publish_post", {"draft_id": draft_id}, "uncertain")
        self.assertEqual(result["state"], "unknown", result)
        self.assertIsNone(result["result"]["input_posted"])
        self.execute("publish_post", {"draft_id": draft_id}, "uncertain")
        another = self.execute("publish_post", {"draft_id": draft_id}, "do-not-retry")
        self.assertEqual(another["result"]["code"], "DRAFT_NOT_READY")
        copied = self.runtime.store.create_draft("alice", "web:local", "服务端收到了，但前端确认丢失", target_url=self.url + "/no-confirm")
        copy_result = self.execute("publish_post", {"draft_id": copied["id"]}, "copied-draft")
        self.assertEqual(copy_result["result"]["code"], "DUPLICATE_CONTENT")
        self.assertEqual(len(self.received) - self.count_before, 1)

    def test_semantic_fill_select_and_invoke_bind_to_actual_observation(self):
        from playwright.sync_api import sync_playwright
        with playwright_fixture_driver(), sync_playwright() as driver:
            context = driver.chromium.connect_over_cdp(self.bridge.endpoint()).contexts[0]
            page = context.pages[0]
            page.goto(self.url + "/")
        tab = next(row for row in self.bridge.pages() if row["url"] == self.url + "/")
        observed = {"browser_page": tab["id"], "page_url": tab["url"]}
        controller = PlaywrightBridge(self.target, timeout_ms=2000)
        with playwright_fixture_driver():
            observed["semantic"] = controller.inspect(observed)
            self.assertNotIn("PRIVATE_PASSWORD", json.dumps(observed))
            element = next(row for row in observed["semantic"]["elements"] if row["tag"] == "textarea")
            result = controller.act(observed, "fill", element["id"], text="真实语义输入")
            self.assertTrue(result["verified"])
            observed["semantic"] = controller.inspect(observed)
            editable = next(row for row in observed["semantic"]["elements"] if row["label"] == "简介")
            self.assertTrue(controller.act(observed, "fill", editable["id"], text="富文本简介")["verified"])
            observed["semantic"] = controller.inspect(observed)
            select = next(row for row in observed["semantic"]["elements"] if row["tag"] == "select")
            controller.act(observed, "select", select["id"], text="games")
            observed["semantic"] = controller.inspect(observed)
            button = next(row for row in observed["semantic"]["elements"] if row["tag"] == "button")
            controller.act(observed, "invoke", button["id"])
        with playwright_fixture_driver(), sync_playwright() as driver:
            page = driver.chromium.connect_over_cdp(self.bridge.endpoint()).contexts[0].pages[0]
            page.get_by_role("status").wait_for(state="visible")
            self.assertEqual(page.locator("#category").input_value(), "games")
            page.locator("h1").evaluate("el => el.textContent='页面已改变'")
        with playwright_fixture_driver(), self.assertRaises(ControlError) as caught:
            controller.act(observed, "invoke", button["id"])
        self.assertEqual(caught.exception.code, "CONTROL_CONTENT_CHANGED")
        self.assertEqual(len(self.received) - self.count_before, 1)
