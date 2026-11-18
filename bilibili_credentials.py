"""Local Bilibili login storage. Cookie values never enter agent responses."""
from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
import uuid

SECRET_FIELDS = ("sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value")


def _dpapi(value: bytes, *, decrypt=False):
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_wchar_p, ctypes.POINTER(Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt32.CryptProtectData.restype = crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if decrypt:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = crypt32.CryptProtectData(ctypes.byref(source), "QQBot Bilibili", None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise OSError("无法使用当前 Windows 用户保护或读取 B站登录态。")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel32.LocalFree(output.data)


def load_credentials(path):
    path = Path(path)
    if not path.is_file():
        return {}
    if path.stat().st_size > 32768:
        raise ValueError("B站凭据文件过大。")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("protection") == "windows-dpapi":
        if os.name != "nt":
            raise ValueError("该登录态仅能由保存它的 Windows 用户读取。")
        data = json.loads(_dpapi(base64.b64decode(data["data"], validate=True), decrypt=True))
    return {key: str(data[key]) for key in SECRET_FIELDS if data.get(key)}


def save_credentials(path, values):
    data = {key: str(values[key]) for key in SECRET_FIELDS if values.get(key)}
    if not data.get("sessdata") or not data.get("bili_jct"):
        raise ValueError("登录尚未完成，请在内置浏览器完成扫码或验证码。")
    if any(len(value) > 8192 for value in data.values()):
        raise ValueError("登录数据无效。")
    if os.name == "nt":
        protected = _dpapi(json.dumps(data).encode("utf-8"))
        data = {"protection": "windows-dpapi", "data": base64.b64encode(protected).decode("ascii")}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def credentials_from_cookies(cookies):
    result = {}
    for jar in cookies:
        for name, morsel in jar.items():
            domain = str(morsel["domain"]).lower().lstrip(".")
            if domain != "bilibili.com" and not domain.endswith(".bilibili.com"):
                continue
            key = name.lower()
            if key in SECRET_FIELDS:
                result[key] = morsel.value
    return result
