"""Local, token-bound browser connector; only accepts Gemini's two login cookies."""
from __future__ import annotations

import atexit
import hmac
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import socket
import threading
from urllib.request import build_opener, ProxyHandler, Request

from gemini_browser_auth import auth_directory, read_private, store_browser_login, write_private
from media_downloader import MediaError


from runtime_paths import resource_path

EXTENSION_DIRECTORY = resource_path("browser_extensions/gemini_connector")
LINK_PATH = "/gemini-browser-link"
_runtime_links = {}
_runtime_lock = threading.Lock()


class LocalServer(ThreadingHTTPServer):
    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class GeminiBrowserLink:
    def __init__(self, config, on_connected=None):
        self.config, self.on_connected = config, on_connected
        self.server = None
        self.shared = False
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self._last_login = ""
        self._active_client = ""
        self._login_lock = threading.Lock()
        self.settings = auth_directory(config) / "browser-link.json"
        try:
            saved = read_private(self.settings)
            token, port = saved["token"], saved["port"]
            if isinstance(token, str) and len(token) >= 32 and type(port) is int and 1024 <= port <= 65535:
                self.token, self.port = token, port
                self._active_client = str(saved.get("active_client", ""))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def start(self):
        if self.server or self.shared:
            return self.url
        if self.port and self._existing_listener():
            self.shared = True
            return self.url
        link = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Requests carry local session tokens; never log headers/bodies.

            def handle(self):
                self.connection.settimeout(5)
                try:
                    super().handle()
                except (ConnectionError, TimeoutError):
                    pass  # Browser preconnects/tabs can disappear at any time.

            def reply(self, status, value, *, html=False):
                data = value.encode("utf-8") if html else json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(data)

            def valid_host(self):
                return self.headers.get("Host") == "127.0.0.1:" + str(link.port)

            def do_GET(self):
                if not self.valid_host() or self.path != LINK_PATH:
                    self.reply(404, {"ok": False})
                    return
                # This page never contains a token or a cookie. The token is
                # only in the URL fragment, read by the installed content script.
                self.reply(200, link.page(), html=True)

            def do_POST(self):
                self.connection.settimeout(4)
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    origin = self.headers.get("Origin", "")
                    if (not self.valid_host() or self.path not in ("/connect", "/health") or not 0 < size <= 16384
                            or self.headers.get_content_type() != "application/json"
                            or (origin and not origin.startswith(("chrome-extension://", "moz-extension://")))):
                        self.reply(403, {"ok": False, "message": "连接请求无效，请从 QQBot 重新打开连接页。"})
                        return
                    request = json.loads(self.rfile.read(size))
                    token = request.get("token", "")
                    if not isinstance(token, str) or not hmac.compare_digest(token, link.token):
                        self.reply(403, {"ok": False, "message": "连接已失效，请从 QQBot 重新打开连接页。"})
                        return
                    if self.path == "/health":
                        self.reply(200, {"ok": True, "service": "qqbot-gemini-link"})
                        return
                    source = "Edge / 当前配置" if request.get("browser") == "edge" else "Chrome / 当前配置"
                    rows = request.get("cookies", [])
                    if not isinstance(rows, list) or len(rows) > 2:
                        self.reply(400, {"ok": False, "message": "仅接受 Gemini 登录所需的两项 Cookie。"})
                        return
                    browser = "edge" if request.get("browser") == "edge" else "chrome"
                    client_id = request.get("client_id", "")
                    if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", client_id):
                        self.reply(400, {"ok": False, "message": "浏览器连接标识无效，请刷新连接页。"})
                        return
                    with link._login_lock:
                        if request.get("handshake") is not True and client_id != link._active_client:
                            self.reply(409, {"ok": False, "message": "已切换到其他浏览器配置；如需使用此配置，请重新打开连接页。"})
                            return
                        store_browser_login(link.config, rows, source, browser)
                        if request.get("handshake") is True:
                            link._active_client = client_id
                            link._save_settings()
                        fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
                        changed = fingerprint != link._last_login or request.get("handshake") is True
                        link._last_login = fingerprint
                    self.reply(200, {"ok": True, "message": "浏览器已连接，登录态会自动同步。可返回 QQBot 选择模型。"})
                    if changed and link.on_connected:
                        link.on_connected(source)
                except MediaError as error:
                    self.reply(400, {"ok": False, "message": str(error)})
                except (OSError, ValueError, TypeError, AttributeError):
                    self.reply(400, {"ok": False, "message": "连接未保存，请返回 QQBot 重新连接。"})

        try:
            self.server = LocalServer(("127.0.0.1", self.port), Handler)
        except OSError:
            if self.port and self._existing_listener():
                self.shared = True
                return self.url
            # A port can be taken by another application after reboot. A new
            # handshake updates the extension; never trust whoever holds it.
            self.server = LocalServer(("127.0.0.1", 0), Handler)
            self.token = secrets.token_urlsafe(32)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        try:
            self._save_settings()
        except BaseException:
            self.server.server_close()
            self.server = None
            raise
        threading.Thread(target=self.server.serve_forever, name="gemini-browser-link", daemon=True).start()
        return self.url

    def _save_settings(self):
        write_private(self.settings, {"port": self.port, "token": self.token, "active_client": self._active_client})

    def _existing_listener(self):
        try:
            request = Request(f"http://127.0.0.1:{self.port}/health",
                data=json.dumps({"token": self.token}).encode(), headers={"Content-Type": "application/json"})
            with build_opener(ProxyHandler({})).open(request, timeout=1) as response:
                data = json.loads(response.read(1024))
            return data.get("ok") is True and data.get("service") == "qqbot-gemini-link"
        except (OSError, ValueError, AttributeError):
            return False

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}{LINK_PATH}#{self.token}"

    def close(self):
        self.shared = False
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    @staticmethod
    def page():
        from html import escape
        folder = escape(str(EXTENSION_DIRECTORY))
        return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>连接 Gemini 与 QQBot</title>
<style>body{{font:17px/1.8 system-ui,sans-serif;background:#f3f5f8;color:#1c2638;max-width:760px;margin:8vh auto;padding:24px}}
main{{background:white;border-radius:18px;padding:32px}}h1{{font-size:27px}}code{{display:block;overflow-wrap:anywhere;background:#eef1f6;padding:12px;border-radius:8px}}
#status{{padding:16px;background:#eaf1ff;border-radius:10px}}a{{color:#2856b6}}</style>
<main data-qqbot-gemini-link="1"><h1>连接当前浏览器的 Gemini</h1>
<p id="status" role="status">正在等待 QQBot Gemini 连接扩展…</p>
<p>首次连接只需安装本地扩展。之后登录态自动同步，不需要复制 Cookie。</p>
<ol><li>在已登录 Gemini 的同一个浏览器配置中打开 <strong>chrome://extensions</strong>（Edge 使用 edge://extensions）。</li>
<li>开启“开发者模式”，点“加载已解压的扩展程序”，选择这个文件夹：<code>{folder}</code></li>
<li>刷新本页。显示“浏览器已连接”后，返回 QQBot 选择模型。</li></ol>
<p>若未登录，先打开 <a href="https://gemini.google.com/" target="_blank" rel="noreferrer">Gemini 网页端</a> 登录，再刷新此页。</p>
<p>扩展只同步 Gemini 使用的两项登录 Cookie 到这台电脑上的 QQBot；不读取密码、聊天内容或其他网站数据。</p></main></html>"""


def resume_browser_link(config):
    """Keep automatic sync available for the console-only bot as well as the GUI."""
    root = auth_directory(config)
    if not config.auto_browser or not (root / "browser-link.json").is_file():
        return
    with _runtime_lock:
        if root not in _runtime_links:
            link = GeminiBrowserLink(config)
            link.start()
            _runtime_links[root] = link
            atexit.register(link.close)
