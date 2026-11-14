"""Automatic Gemini configuration, profile boundaries and local browser handshake."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, Request

from config import AppConfig, GeminiWebConfig, MediaConfig
from gemini_browser_auth import (auth_directory, browser_login, browser_profiles, connected_login,
                                essential_cookies, read_private, store_browser_login)
from gemini_browser_link import GeminiBrowserLink, LINK_PATH
from gemini_web_client import cookie_values, proxy_settings
from media_downloader import MediaError


def cookies(psid="fixture-browser-login", ts="fixture-browser-ts"):
    return [{"name": name, "value": value, "domain": ".google.com", "path": "/",
             "expirationDate": time.time() + 3600} for name, value in
            (("__Secure-1PSID", psid), ("__Secure-1PSIDTS", ts))]


class BrowserSetupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="qqbot-browser-setup-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.browser_root = self.root / "Chrome"
        self.profile = self.browser_root / "Default"
        (self.profile / "Network").mkdir(parents=True)
        (self.browser_root / "Local State").write_text(json.dumps({"profile": {
            "last_used": "Default", "info_cache": {"Default": {}, "Profile 2": {}}}}))
        self.cfg = GeminiWebConfig(cookie_file=str(self.root / "auth/cookies.json"), browser="chrome",
                                   secure_1psid_env="", secure_1psidts_env="", proxy_env="")
        roots = patch("gemini_browser_auth.browser_roots", return_value={"chrome": self.browser_root,
                      "firefox": self.root / "Firefox"})
        roots.start()
        self.addCleanup(roots.stop)

    def database(self, profile=None, *, encrypted=b"", psid="profile-login", version=24, domain=".google.com"):
        profile = profile or self.profile
        (profile / "Network").mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(profile / "Network/Cookies")) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT, value TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS cookies(host_key TEXT,name TEXT,value TEXT,encrypted_value BLOB,expires_utc INTEGER,path TEXT)")
            db.execute("INSERT INTO meta VALUES('version',?)", (str(version),))
            for name, value in (("__Secure-1PSID", psid), ("__Secure-1PSIDTS", "profile-ts")):
                db.execute("INSERT INTO cookies VALUES(?,?,?,?,?,?)", (domain, name, value,
                    encrypted if name == "__Secure-1PSID" else b"", 0, "/"))

    def test_auto_profile_and_scoped_cookie_query_ignore_other_google_and_foreign_cookies(self):
        self.database()
        self.database(self.browser_root / "Profile 2", psid="other-profile")
        with closing(sqlite3.connect(self.profile / "Network/Cookies")) as db, db:
            db.execute("INSERT INTO cookies VALUES(?,?,?,?,?,?)", ("evilgoogle.com", "__Secure-1PSID", "foreign", b"v20", 0, "/"))
            db.execute("INSERT INTO cookies VALUES(?,?,?,?,?,?)", (".google.com", "OTHER_PRIVATE_COOKIE", "unrelated", b"v20", 0, "/"))
        self.assertEqual(browser_profiles("chrome")[0]["name"], "Default")
        self.assertEqual(cookie_values(self.cfg), ("profile-login", "profile-ts"))
        self.cfg.browser_profile = "Profile 2"
        self.assertEqual(cookie_values(self.cfg)[0], "other-profile")

    def test_browser_protection_error_points_to_connection_and_never_logs_encrypted_data(self):
        self.database(encrypted=b"v20-private-fixture", psid="")
        with self.assertRaisesRegex(MediaError, "连接当前浏览器") as caught:
            cookie_values(self.cfg)
        self.assertNotIn("private-fixture", str(caught.exception))

    @unittest.skipUnless(os.name == "nt", "DPAPI browser layout is Windows-specific")
    def test_old_chromium_decryption_checks_domain_hash_before_accepting_login(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key, nonce = b"a" * 32, b"b" * 12
        plain = hashlib.sha256(b".google.com").digest() + b"encrypted-profile-login"
        self.database(encrypted=b"v10" + nonce + AESGCM(key).encrypt(nonce, plain, None), psid="")
        data = json.loads((self.browser_root / "Local State").read_text())
        import base64
        data["os_crypt"] = {"encrypted_key": base64.b64encode(b"DPAPI" + b"fixture").decode()}
        (self.browser_root / "Local State").write_text(json.dumps(data))
        with patch("gemini_browser_auth._dpapi", return_value=key):
            self.assertEqual(cookie_values(self.cfg)[0], "encrypted-profile-login")
            with closing(sqlite3.connect(self.profile / "Network/Cookies")) as db, db:
                db.execute("UPDATE cookies SET host_key='gemini.google.com' WHERE name='__Secure-1PSID'")
            with self.assertRaisesRegex(MediaError, "连接当前浏览器"):
                cookie_values(self.cfg)

    def test_linked_login_survives_restarts_and_explicit_profile_selection_takes_precedence(self):
        store_browser_login(self.cfg, cookies(), "Chrome / 当前配置", "chrome")
        login = connected_login(self.cfg)
        self.assertEqual(login.psid, "fixture-browser-login")
        self.assertNotIn(login.psid, repr(login))
        self.assertEqual(cookie_values(self.cfg), ("fixture-browser-login", "fixture-browser-ts"))
        raw = (auth_directory(self.cfg) / "browser-session.json").read_text()
        if os.name == "nt":
            self.assertNotIn(login.psid, raw)
            self.assertIn("windows-current-user", raw)
        self.database(psid="selected-profile")
        self.cfg.browser_profile = str(self.profile)
        self.assertEqual(cookie_values(self.cfg)[0], "selected-profile")
        self.cfg.auto_browser = False
        with self.assertRaisesRegex(MediaError, "登录态"):
            cookie_values(self.cfg)

    def test_manual_cookie_and_environment_remain_explicit_overrides(self):
        store_browser_login(self.cfg, cookies())
        Path(self.cfg.cookie_file).write_text(json.dumps({"__Secure-1PSID": "manual-login"}))
        self.assertEqual(cookie_values(self.cfg), ("manual-login", ""))
        self.cfg.secure_1psid_env = "QQBOT_FIXTURE_GEMINI_LOGIN"
        with patch.dict(os.environ, {self.cfg.secure_1psid_env: "environment-login"}):
            self.assertEqual(cookie_values(self.cfg), ("environment-login", ""))

    def test_expired_foreign_partitioned_and_control_character_cookies_never_become_login(self):
        for changes in ({"domain": "evilgoogle.com"}, {"expirationDate": time.time() - 1},
                        {"partitionKey": {"topLevelSite": "https://other.example"}}, {"value": "x\r\ny"}):
            with self.subTest(changes=changes), self.assertRaises(MediaError):
                essential_cookies([{**cookies()[0], **changes}])

    def test_expired_link_cache_does_not_mask_live_profile_login(self):
        store_browser_login(self.cfg, cookies(), browser="chrome")
        path = auth_directory(self.cfg) / "browser-session.json"
        record = read_private(path)
        for row in record["cookies"]:
            row["expires"] = time.time() - 1
        from gemini_browser_auth import write_private
        write_private(path, record)
        self.database(psid="current-login")
        self.assertEqual(cookie_values(self.cfg)[0], "current-login")

    def test_unchanged_sync_keeps_cache_timestamp_and_does_not_trigger_repeated_probes(self):
        rows = cookies()
        self.assertTrue(store_browser_login(self.cfg, rows, browser="chrome"))
        path = auth_directory(self.cfg) / "browser-session.json"
        before = path.stat().st_mtime_ns
        self.assertFalse(store_browser_login(self.cfg, rows, browser="chrome"))
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_firefox_reads_only_default_non_container_gemini_login(self):
        self.cfg.browser = "firefox"
        root = self.root / "Firefox"
        profile = root / "Profiles/default"
        profile.mkdir(parents=True)
        (root / "profiles.ini").write_text("[Profile0]\nPath=Profiles/default\nIsRelative=1\nDefault=1\n")
        with closing(sqlite3.connect(profile / "cookies.sqlite")) as db, db:
            db.execute("CREATE TABLE moz_cookies(host TEXT,name TEXT,value TEXT,expiry INTEGER,path TEXT,originAttributes TEXT)")
            for attrs, value in (("", "firefox-login"), ("^userContextId=1", "other-container")):
                db.execute("INSERT INTO moz_cookies VALUES(?,?,?,?,?,?)", (".google.com", "__Secure-1PSID", value,
                                                                         int(time.time() + 3600), "/", attrs))
        self.assertEqual(cookie_values(self.cfg)[0], "firefox-login")

    def test_system_proxy_is_used_only_when_enabled_and_explicit_environment_wins(self):
        self.cfg.proxy_env = "QQBOT_FIXTURE_GEMINI_PROXY"
        with patch.dict(os.environ, {self.cfg.proxy_env: ""}), \
             patch("gemini_web_client.getproxies", return_value={"https": "http://127.0.0.1:7890"}):
            self.assertEqual(proxy_settings(self.cfg), ("http://127.0.0.1:7890", "系统代理"))
            self.cfg.auto_proxy = False
            self.assertEqual(proxy_settings(self.cfg), (None, "直连"))
            with patch.dict(os.environ, {self.cfg.proxy_env: "http://fixture-proxy:8888"}):
                self.assertEqual(proxy_settings(self.cfg), ("http://fixture-proxy:8888", "代理环境变量"))

    def test_existing_browser_connection_reads_only_gemini_without_restarting_or_navigation(self):
        (self.browser_root / "DevToolsActivePort").write_text("fixture")
        bridge = SimpleNamespace(pages=lambda: [{"url": "https://unrelated.example/"},
                                               {"url": "https://gemini.google.com/app"}],
                                 connection=lambda page: nullcontext("fixture-socket"))
        bridge.command = Mock(return_value={"cookies": cookies("existing-browser-login")})
        with patch("browser_control.BrowserBridge", return_value=bridge), \
             patch("gemini_browser_auth._chromium_rows") as direct:
            self.assertEqual(cookie_values(self.cfg)[0], "existing-browser-login")
        direct.assert_not_called()
        self.assertEqual(bridge.command.call_args.args[2:],
                         ("Network.getCookies", {"urls": ["https://gemini.google.com/"]}))


class BrowserLinkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="qqbot-browser-link-")
        self.addCleanup(temp.cleanup)
        self.cfg = GeminiWebConfig(cookie_file=str(Path(temp.name) / "cookies.json"))
        self.connected, self.notices = threading.Event(), []
        def received(source):
            self.notices.append(source)
            self.connected.set()
        self.link = GeminiBrowserLink(self.cfg, received)
        self.link.start()
        self.addCleanup(self.link.close)
        self.base = self.link.url.split(LINK_PATH)[0]
        self.opener = build_opener(ProxyHandler({}))

    def request(self, payload=None, *, origin="chrome-extension://" + "a" * 32, host=None):
        if payload is not None:
            payload = {"client_id": "fixture-browser-client-1234", "handshake": True, **payload}
        headers = {"Content-Type": "application/json", "Origin": origin}
        if host:
            headers["Host"] = host
        request = Request(self.base + ("/connect" if payload is not None else LINK_PATH),
                          data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        try:
            with self.opener.open(request, timeout=3) as response:
                return response.status, response.read().decode()
        except HTTPError as error:
            return error.code, error.read().decode()

    def test_token_and_origin_are_required_before_any_login_is_saved(self):
        payload = {"token": self.link.token, "cookies": cookies(), "browser": "chrome"}
        for invalid, headers in (({**payload, "token": "bad-token"}, {}),
                                 (payload, {"origin": "https://example.com"}),
                                 (payload, {"host": "remote.example"})):
            with self.subTest(headers=headers):
                status, body = self.request(invalid, **headers)
                self.assertEqual(status, 403, body)
                self.assertNotIn("fixture-browser-login", body)
                self.assertIsNone(connected_login(self.cfg))
        status, page = self.request()
        self.assertEqual(status, 200)
        self.assertNotIn(self.link.token, page)
        self.assertNotIn("fixture-browser-login", page)

    def test_connect_refresh_and_restart_reuse_credentials_without_repeated_ui_notifications(self):
        rows = cookies()
        payload = {"token": self.link.token, "cookies": rows, "browser": "chrome"}
        status, body = self.request(payload)
        self.assertEqual(status, 200, body)
        self.assertTrue(self.connected.wait(1))
        self.assertEqual(cookie_values(self.cfg), ("fixture-browser-login", "fixture-browser-ts"))
        self.assertNotIn("fixture-browser-login", body)
        payload["handshake"] = False
        self.request(payload)
        self.assertEqual(len(self.notices), 1)
        self.connected.clear()
        rows[1]["value"] = "rotated-browser-ts"
        self.request(payload)
        self.assertTrue(self.connected.wait(1))
        self.assertEqual(cookie_values(self.cfg)[1], "rotated-browser-ts")
        original = self.link.url
        self.link.close()
        resumed = GeminiBrowserLink(self.cfg)
        self.addCleanup(resumed.close)
        # The listener just closed; model its absence without probing a port
        # that could already have been reused by an unrelated desktop service.
        with patch.object(resumed, "_existing_listener", return_value=False):
            self.assertEqual(resumed.start(), original)

    def test_unrelated_or_excess_cookies_are_rejected(self):
        for rows in (cookies() + [{"name": "OTHER", "value": "not-needed"}],
                     [{**cookies()[0], "domain": "not-google.example"}]):
            status, body = self.request({"token": self.link.token, "cookies": rows})
            self.assertEqual(status, 400, body)
            self.assertIsNone(connected_login(self.cfg))

    def test_headless_runtime_and_gui_share_an_existing_listener_without_replacing_token(self):
        peer = GeminiBrowserLink(self.cfg)
        self.addCleanup(peer.close)
        self.assertEqual(peer.start(), self.link.url)
        self.assertTrue(peer.shared)
        peer.close()
        status, _ = self.request({"token": self.link.token, "cookies": cookies()})
        self.assertEqual(status, 200)

    def test_switching_profiles_revokes_old_profiles_background_sync(self):
        self.request({"token": self.link.token, "cookies": cookies()})
        status, body = self.request({"token": self.link.token, "cookies": cookies("new-profile-login"),
                                    "client_id": "different-browser-client-5678"})
        self.assertEqual(status, 200, body)
        status, _ = self.request({"token": self.link.token, "cookies": cookies(), "handshake": False})
        self.assertEqual(status, 409)
        self.assertEqual(cookie_values(self.cfg)[0], "new-profile-login")

    @unittest.skipUnless(shutil.which("node"), "Node verifies the actual extension service worker")
    def test_actual_extension_handshake_and_cookie_rotation_reach_local_server(self):
        harness = Path(__file__).parent / "implementation_artifacts/gemini_browser_setup/extension_smoke.cjs"
        result = subprocess.run([shutil.which("node"), str(harness)], input=json.dumps({
            "endpoint": self.base, "token": self.link.token}), text=True, encoding="utf-8",
            capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])
        self.assertEqual(cookie_values(self.cfg), ("extension-fixture-psid", "extension-fixture-rotated-ts"))
        self.assertNotIn("extension-fixture-psid", result.stdout)


class BrowserSetupGuiTests(unittest.TestCase):
    def test_shared_headless_listener_updates_gui_and_disabling_auto_login_stops_gui_watch(self):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from ui.components.media_tab import MediaTabWidget
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(media=MediaConfig(cache_dir=tmp, gemini_web=GeminiWebConfig(cookie_file=str(Path(tmp) / "cookies.json"))))
            owner = GeminiBrowserLink(cfg.media.gemini_web)
            owner.start()
            try:
                widget = MediaTabWidget(cfg)
                try:
                    self.assertTrue(widget._gemini_link.shared)
                    with patch.object(widget, "probe_gemini_models") as probe:
                        rows = cookies()
                        store_browser_login(cfg.media.gemini_web, rows)
                        widget._poll_gemini_link()
                        probe.assert_called_once()
                        store_browser_login(cfg.media.gemini_web, rows)
                        widget._poll_gemini_link()
                        probe.assert_called_once()
                    widget.gemini_auto_browser.setChecked(False)
                    self.assertIsNone(widget._gemini_link)
                    self.assertFalse(widget._gemini_link_timer.isActive())
                finally:
                    widget.close()
            finally:
                owner.close()

    def test_auto_settings_connection_button_and_model_probe_preserve_selected_model(self):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from ui.components.media_tab import MediaTabWidget
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(media=MediaConfig(cache_dir=tmp, gemini_web=GeminiWebConfig(cookie_file=str(Path(tmp) / "cookies.json"))))
            widget = MediaTabWidget(cfg)
            self.addCleanup(widget.close)
            self.assertTrue(widget.gemini_auto_browser.isChecked())
            self.assertTrue(widget.gemini_auto_proxy.isChecked())
            widget.gemini_web_model.setCurrentText("flash")
            with patch("ui.components.media_tab.QDesktopServices.openUrl", return_value=True) as browser:
                widget.connect_gemini_browser()
                self.assertEqual(browser.call_args.args[0].host(), "127.0.0.1")
            widget._receive_gemini_account({"source": "Chrome / 当前配置", "proxy": "系统代理",
                "models": [{"name": "gemini-pro", "available": True}]}, "")
            self.assertEqual(widget.gemini_web_model.currentText(), "flash")
            self.assertIn("Chrome", widget.gemini_web_status.text())
            self.assertIn("系统代理", widget.gemini_web_status.text())
            restored = MediaConfig.model_validate(widget.collect_media_config_values()).gemini_web
            self.assertTrue(restored.auto_browser)
            self.assertEqual(restored.browser_profile, "")
            self.assertEqual(restored.model, "flash")
            widget.close()


if __name__ == "__main__":
    unittest.main()
