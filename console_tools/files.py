"""Small, dependency-free file contracts for the console CLI and MCP."""
from __future__ import annotations

import hashlib
import json
import ntpath
from pathlib import Path
import re


class ConsoleFileError(ValueError):
    def __init__(self, code, message, hint=""):
        super().__init__(message)
        self.code, self.hint = code, hint


def read_json(path):
    raw = Path(path).read_bytes()
    return json.loads(raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"))


def safe_file_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", ntpath.basename(str(name or "file"))).strip(" .")
    return name[:180] or "file"


def outgoing_files(context, files):
    if not isinstance(files, list) or not files:
        raise ConsoleFileError("FILES_REQUIRED", "请指定待发文件。", "send --file <路径>，多个文件重复 --file。")
    base = Path(context.get("output_dir") or ".").resolve()
    roots = [Path(p).resolve() for p in context.get("artifact_roots", [])]
    if not roots:
        raise ConsoleFileError("CONTEXT_NEEDS_REFRESH", "旧上下文没有文件交付目录。", "使用当前事件提供的 context_file。")
    result, seen = [], set()
    for item in files:
        value = item.get("path") if isinstance(item, dict) else item
        if not isinstance(value, str) or not value.strip():
            raise ConsoleFileError("INVALID_FILE", "每项文件必须是路径字符串或包含 path 的对象。")
        path = Path(value).expanduser()
        path = (base / path).resolve() if not path.is_absolute() else path.resolve()
        if not any(path.is_relative_to(root) for root in roots):
            raise ConsoleFileError("FILE_OUTSIDE_OUTPUT", "文件不在本次交付目录或资料库中。",
                                   "先将成果写入 context 命令返回的 output_dir，再使用 send。")
        if not path.is_file():
            raise ConsoleFileError("FILE_NOT_FOUND", "文件不存在：" + str(path), "检查路径；相对路径以 output_dir 为基准。")
        if str(path) in seen:
            continue
        seen.add(str(path))
        stat = path.stat()
        if not stat.st_size:
            raise ConsoleFileError("EMPTY_FILE", "文件为空：" + path.name)
        result.append({"path": str(path), "name": safe_file_name(item.get("name") if isinstance(item, dict)
                      and item.get("name") else path.name), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    return result


def incoming_files(event):
    """Read only gateway attachment fields; never trust a supplied local path."""
    raw = []
    if event.get("notice_type") in ("group_upload", "offline_file"):
        raw.append(event.get("file") or {})
    message = event.get("message")
    for segment in message if isinstance(message, list) else []:
        if isinstance(segment, dict) and segment.get("type") in ("file", "video"):
            data = segment.get("data")
            if isinstance(data, dict):
                raw.append({**data, "media_type": segment["type"]})
    if not isinstance(message, list):
        # QQBot configures NapCat's messagePostFormat as string. Split attributes
        # before decoding so an escaped comma/bracket cannot create a field.
        text = message if isinstance(message, str) else event.get("raw_message", "")
        for match in re.finditer(r"\[CQ:(file|video),([^\]]*)\]", str(text or "")):
            fields = {"media_type": match[1]}
            for part in match.group(2).split(","):
                key, separator, value = part.partition("=")
                if separator:
                    for escaped, decoded in (("&#44;", ","), ("&#91;", "["), ("&#93;", "]"), ("&amp;", "&")):
                        value = value.replace(escaped, decoded)
                    fields[key] = value
            raw.append(fields)
    files = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        native_id = str(item.get("id") or item.get("file_id") or item.get("file") or "")
        url = str(item.get("url") or "")
        if not native_id and not url:
            continue
        try:
            size = max(0, int(item.get("size") or item.get("file_size") or 0))
            busid = int(item.get("busid") or 0)
        except (ValueError, TypeError):
            size, busid = 0, 0
        files.append({"native_id": native_id, "name": safe_file_name(item.get("name") or item.get("file_name") or
                      ("video.mp4" if item.get("media_type") == "video" else "")),
                      "size": size, "busid": busid, "url": url,
                      **({"media_type": "video"} if item.get("media_type") == "video" else {})})
    return files


def attachment_id(namespace, scope, item):
    identity = [namespace, scope, item.get("native_id") or item.get("url"), item["name"]]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]


def file_error(error):
    code = getattr(error, "code", "FORBIDDEN" if isinstance(error, PermissionError) else
                   "FILE_NOT_FOUND" if isinstance(error, FileNotFoundError) else "INVALID_REQUEST")
    return {"ok": False, "code": code, "error": str(error),
            "hint": getattr(error, "hint", "使用 schema 查看参数；使用 context 查看当前输出目录和收件文件。"),
            **({"details": error.details} if getattr(error, "details", None) else {}),
            **({"retryable": error.retryable} if hasattr(error, "retryable") else {})}
