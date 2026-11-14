"""Scoped Gemini login discovery; credentials never enter UI/status messages."""
from __future__ import annotations

import base64
from contextlib import closing
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

from media_downloader import MediaError


COOKIE_NAMES = ("__Secure-1PSID", "__Secure-1PSIDTS")
COOKIE_DOMAINS = {"google.com", "gemini.google.com"}
BROWSER_LABELS = {"chrome": "Chrome", "edge": "Edge", "brave": "Brave", "firefox": "Firefox"}


@dataclass
class BrowserLogin:
    psid: str = field(repr=False)
    psidts: str = field(default="", repr=False)
    source: str = ""


def essential_cookies(rows):
    """Only the two required Google cookies, never another site's session."""
    selected = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name, value = row.get("name"), row.get("value")
        expires = row.get("expirationDate", row.get("expires", 0))
        if (name not in COOKIE_NAMES or not isinstance(value, str) or not value.strip() or len(value) > 8192
                or any(ord(character) < 33 for character in value)
                or str(row.get("domain", "")).lstrip(".").lower() not in COOKIE_DOMAINS
                or row.get("path", "/") != "/" or row.get("partitionKey")):
            continue
        if not isinstance(expires, (int, float)) or (expires > 0 and expires <= time.time()):
            continue
        if name in selected and selected[name]["value"] != value:
            raise MediaError("浏览器返回了多个不同的 Gemini 登录态，请在目标浏览器配置中重新连接。")
        selected[name] = {"name": name, "value": value.strip(), "domain": row["domain"],
                          "path": "/", "expires": expires}
    if COOKIE_NAMES[0] not in selected:
        raise MediaError("此浏览器配置尚未登录 Gemini；请先在 gemini.google.com 登录，再点连接或重新检测。")
    return list(selected.values())


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, *, protect):
    """Windows current-user protection, with UI prompts explicitly disabled."""
    crypt, kernel = ctypes.WinDLL("crypt32", use_last_error=True), ctypes.WinDLL("kernel32")
    incoming, outgoing = DataBlob(), DataBlob()
    buffer = ctypes.create_string_buffer(data)
    incoming.size, incoming.data = len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise OSError("Current-user data protection unavailable")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


def auth_directory(config):
    return Path(config.cookie_file or "data/gemini_web/cookies.json").expanduser().resolve().parent


def read_private(path):
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    if record.get("protection") == "windows-current-user":
        record = json.loads(_dpapi(base64.b64decode(record["data"], validate=True), protect=False))
    return record


def write_private(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(record, ensure_ascii=False).encode("utf-8")
    if os.name == "nt":
        data = json.dumps({"protection": "windows-current-user",
                           "data": base64.b64encode(_dpapi(data, protect=True)).decode("ascii")}).encode("utf-8")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def store_browser_login(config, rows, source="当前浏览器", browser="auto"):
    cookies = essential_cookies(rows)
    # No cookie values in config.yaml, task records, GUI signals, or HTTP replies.
    path = auth_directory(config) / "browser-session.json"
    record = {"cookies": sorted(cookies, key=lambda row: row["name"]), "source": str(source)[:80], "browser": browser}
    try:
        old = read_private(path)
        if all(old.get(key) == value for key, value in record.items()):
            return False
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    write_private(path, {**record, "updated": time.time()})
    return True


def connected_login(config):
    try:
        record = read_private(auth_directory(config) / "browser-session.json")
        if config.browser_profile or (config.browser != "auto" and record.get("browser") != config.browser):
            return None
        rows = essential_cookies(record["cookies"])
        values = {row["name"]: row["value"] for row in rows}
        return BrowserLogin(values[COOKIE_NAMES[0]], values.get(COOKIE_NAMES[1], ""),
                            "已连接的浏览器：" + str(record.get("source", "当前配置"))[:80])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, MediaError):
        return None


def browser_roots():
    local, roaming = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    if os.name == "nt" and local:
        return {"chrome": Path(local) / "Google/Chrome/User Data",
                "edge": Path(local) / "Microsoft/Edge/User Data",
                "brave": Path(local) / "BraveSoftware/Brave-Browser/User Data",
                "firefox": Path(roaming or local) / "Mozilla/Firefox"}
    if sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
        return {"chrome": root / "Google/Chrome", "edge": root / "Microsoft Edge",
                "brave": root / "BraveSoftware/Brave-Browser", "firefox": root / "Firefox"}
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return {"chrome": root / "google-chrome", "edge": root / "microsoft-edge",
            "brave": root / "BraveSoftware/Brave-Browser", "firefox": Path.home() / ".mozilla/firefox"}


def default_browser():
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as key:
                name = winreg.QueryValueEx(key, "ProgId")[0].lower()
            for needle, browser in (("firefox", "firefox"), ("brave", "brave"), ("msedge", "edge"), ("chrome", "chrome")):
                if needle in name:
                    return browser
        except OSError:
            pass
    return next((name for name, root in browser_roots().items() if root.exists()), "chrome")


def browser_profiles(browser="auto"):
    browser = default_browser() if browser == "auto" else browser
    root = browser_roots()[browser]
    if browser == "firefox":
        from configparser import ConfigParser, Error as ConfigError
        parser = ConfigParser()
        try:
            parser.read(root / "profiles.ini", encoding="utf-8")
        except (OSError, ConfigError, UnicodeError):
            return []
        defaults = {parser.get(section, "Default", fallback="") for section in parser.sections()
                    if section.startswith("Install")}
        profiles = []
        for section in parser.sections():
            if section.startswith("Profile"):
                value = parser.get(section, "Path", fallback="")
                if value:
                    path = root / value if parser.get(section, "IsRelative", fallback="1") == "1" else Path(value)
                    profiles.append({"browser": browser, "path": str(path), "name": path.name,
                                     "default": value in defaults or parser.get(section, "Default", fallback="0") == "1"})
        return sorted(profiles, key=lambda row: not row["default"])
    try:
        state = json.loads((root / "Local State").read_text(encoding="utf-8"))
        profile = state.get("profile", {})
        names = list(profile.get("info_cache", {}))
        last = profile.get("last_used", "Default")
    except (OSError, ValueError, AttributeError):
        names, last = ["Default"], "Default"
    return [{"browser": browser, "path": str(root / name), "name": name, "default": name == last}
            for name in sorted(names, key=lambda name: name != last)
            if Path(name).name == name and (root / name).is_dir()]


def _chromium_rows(profile):
    database = profile / "Network/Cookies"
    if not database.is_file():
        database = profile / "Cookies"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=.5)) as db:
        version = int(db.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0])
        rows = db.execute("SELECT host_key,name,value,encrypted_value,expires_utc FROM cookies "
                          "WHERE host_key IN ('.google.com','google.com','gemini.google.com','.gemini.google.com') "
                          "AND name IN ('__Secure-1PSID','__Secure-1PSIDTS') AND path='/'").fetchall()
    key = None
    result = []
    for domain, name, value, encrypted, expires in rows:
        expiry = expires / 1e6 - 11644473600 if expires else 0
        if expires and expiry <= time.time():
            continue
        if not value and encrypted:
            if encrypted.startswith(b"v20") or os.name != "nt":
                raise MediaError("Chrome/Edge 登录态受浏览器保护；请点“连接当前浏览器”，连接后自动同步，无需复制 Cookie。")
            if encrypted.startswith((b"v10", b"v11")):
                if key is None:
                    state = json.loads((profile.parent / "Local State").read_text(encoding="utf-8"))
                    encrypted_key = base64.b64decode(state["os_crypt"]["encrypted_key"])
                    if not encrypted_key.startswith(b"DPAPI"):
                        raise ValueError("Unsupported browser key")
                    key = _dpapi(encrypted_key[5:], protect=False)
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                from cryptography.exceptions import InvalidTag
                try:
                    plain = AESGCM(key).decrypt(encrypted[3:15], encrypted[15:], None)
                except InvalidTag:
                    raise ValueError("Browser cookie integrity check failed") from None
                if version >= 24:
                    if plain[:32] != hashlib.sha256(domain.encode()).digest():
                        raise ValueError("Cookie domain binding mismatch")
                    plain = plain[32:]
            else:
                plain = _dpapi(encrypted, protect=False)
            value = plain.decode("utf-8")
        result.append({"domain": domain, "name": name, "value": value, "expires": expiry})
    return result


def _devtools_rows(profile):
    """Reuse an existing local browser connection without enabling/restarting it."""
    from browser_control import BrowserBridge, BrowserError
    bridge = BrowserBridge({"profile_path": str(profile.parent)})
    try:
        for page in bridge.pages():
            from urllib.parse import urlsplit
            if urlsplit(page.get("url", "")).hostname != "gemini.google.com":
                continue
            with bridge.connection(page) as socket:
                return bridge.command(socket, 1, "Network.getCookies", {"urls": ["https://gemini.google.com/"]}).get("cookies", [])
    except BrowserError:
        pass
    return []


def _firefox_rows(profile):
    with closing(sqlite3.connect((profile / "cookies.sqlite").as_uri() + "?mode=ro", uri=True, timeout=.5)) as db:
        rows = db.execute("SELECT host,name,value,expiry FROM moz_cookies WHERE host IN "
                          "('.google.com','google.com','gemini.google.com','.gemini.google.com') "
                          "AND name IN ('__Secure-1PSID','__Secure-1PSIDTS') AND path='/' AND originAttributes=''").fetchall()
    return [{"domain": host, "name": name, "value": value, "expires": expires} for host, name, value, expires in rows]


def browser_login(config):
    if not config.auto_browser:
        raise MediaError("网页端 Gemini 尚未配置登录态；可开启自动读取浏览器，或提供 Cookie 文件/环境变量。")
    if connected := connected_login(config):
        return connected
    profiles = browser_profiles(config.browser)
    if config.browser_profile:
        browser = default_browser() if config.browser == "auto" else config.browser
        profile = Path(config.browser_profile).expanduser()
        if not profile.is_absolute():
            profile = browser_roots()[browser] / profile
    elif profiles:
        browser, profile = profiles[0]["browser"], Path(profiles[0]["path"])
    else:
        raise MediaError("未找到浏览器登录态；请点“连接当前浏览器”并在 Gemini 登录，或选择已有的浏览器配置。")
    try:
        profile = profile.resolve()
        if browser == "firefox":
            rows = _firefox_rows(profile)
        else:
            rows = _devtools_rows(profile) if (profile.parent / "DevToolsActivePort").is_file() else []
            if not rows:
                rows = _chromium_rows(profile)
        values = {row["name"]: row["value"] for row in essential_cookies(rows)}
        return BrowserLogin(values[COOKIE_NAMES[0]], values.get(COOKIE_NAMES[1], ""),
                            BROWSER_LABELS[browser] + " / " + profile.name)
    except MediaError:
        raise
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, ImportError):
        raise MediaError("暂不能直接读取当前浏览器登录态（浏览器占用或加密保护）；点“连接当前浏览器”即可自动同步，无需手动填写 Cookie。") from None
