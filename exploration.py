"""Read-only exploration of granted documents and public page source."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urljoin


EXPLORER_NAME = "兴趣探索"
EXPLORER_RULES = "围绕自己已有兴趣、未完成问题或新发现持续研究，实际使用工具阅读资料并保留来源。先查 explore sources，再自主选择本机授权文本或公开网络；文本/HTML/JSON 优先 explore read/fetch/parse 或原生网页读取，视觉交互才用浏览器。每次报告实际新发现、未解决问题和下一步；不把计划当经历，不接触其他本机目录或账号。"
TEXT_SUFFIXES = {".txt", ".md", ".html", ".htm", ".json", ".csv", ".tsv", ".xml", ".rst", ".srt", ".vtt"}
_DEFAULT_SOURCE = object()


def configuration(coordinator, key, *, source=_DEFAULT_SOURCE):
    cfg = coordinator.client.app_config
    workspace = coordinator.directory(key) / "exploration"
    roots = {"workspace": str(workspace), "library": str(Path(cfg.bot.runtime_database_path).resolve().parent / "library")}
    for index, path in enumerate([*getattr(cfg.bot, "self_activity_roots", []), *cfg.media.local_library_roots]):
        roots["authorized-" + str(index + 1)] = str(Path(path).expanduser().resolve())
    for name, path in cfg.sandbox.profiles.items():
        roots["profile-" + name] = str(Path(path).expanduser().resolve())
    if source is _DEFAULT_SOURCE:
        source = coordinator.contexts.get(key)
    database = Path(cfg.sandbox.directory).resolve() / "sandbox.sqlite3"
    if source and database.is_file():
        # Open an existing grant registry read-only. A different group/account's
        # grants and browser login profiles are never exploration sources.
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='grants'").fetchone():
                group = source.conversation_id if source.is_group else ""
                for grant_id, owner, path in db.execute("SELECT id,owner,path FROM grants WHERE namespace=? AND scope=?", (coordinator.namespace, source.scope)):
                    if cfg.bot.is_master(group, owner):
                        roots["grant-" + grant_id] = str(Path(path).resolve())
    return {"roots": roots, "workspace": str(workspace), "network": True}


def roots_for(context):
    cfg = context.get("exploration_config") or {}
    roots = {name: Path(path).resolve() for name, path in cfg.get("roots", {}).items()}
    if context.get("output_dir"):
        roots["output"] = Path(context["output_dir"]).resolve()
    return roots


def source_path(context, root_id, path):
    roots = roots_for(context)
    root = roots.get(root_id)
    if root is None:
        raise PermissionError("目录未授权；先 explore sources 选择 root_id。")
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise PermissionError("路径超出已授权目录。")
    return target


class PageText(HTMLParser):
    def __init__(self, base=""):
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.links, self.hidden = base, [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        if tag in ("p", "div", "br", "h1", "h2", "h3", "li", "tr", "article") and not self.hidden:
            self.parts.append("\n")
        if tag == "a" and not self.hidden:
            url = dict(attrs).get("href", "")
            if url:
                resolved = urljoin(self.base, url)
                if resolved.startswith(("http://", "https://")):
                    self.links.append(resolved)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, text):
        if not self.hidden and text.strip():
            self.parts.append(text.strip() + " ")


def parse_text(text, format="text", query="", base=""):
    if format == "html":
        result = text
    elif format == "json":
        value = json.loads(text)
        for part in query.split(".") if query else []:
            value = value[int(part)] if isinstance(value, list) else value[part]
        return json.dumps(value, ensure_ascii=False, indent=2)
    elif "<" in text and ">" in text:
        parser = PageText(base)
        parser.feed(text)
        result = "\n".join(dict.fromkeys(parser.links)) if format == "links" else "".join(parser.parts)
    else:
        result = text
    if query:
        result = "\n".join(line for line in result.splitlines() if query.casefold() in line.casefold())
    return result


def fetch_page(url):
    from media_downloader import http_request
    async def get():
        return await http_request(url, max_bytes=2 * 1024 * 1024, timeout=20)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(get())
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(get())).result(timeout=25)


def decode_text(data, mime=""):
    declared = re.search(r"charset\s*=\s*['\"]?([\w-]+)", mime, re.I)
    if not declared:
        declared = re.search(r"charset\s*=\s*['\"]?([\w-]+)", data[:4096].decode("ascii", errors="ignore"), re.I)
    candidates = [declared[1]] if declared else []
    candidates += ["utf-8-sig", "gb18030"]
    for encoding in candidates:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeError):
            continue
    return data.decode("utf-8-sig", errors="replace")


def reading_evidence(trace):
    pending, completed = [], set()
    def plain(value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value
    for row in trace:
        if not isinstance(row, dict):
            continue
        if row.get("tool_calls"):
            pending = [call for call in row["tool_calls"] if isinstance(call, dict)]
        elif pending and (row.get("type") in ("GENERIC", "TOOL_RESULT", "TOOL_RESPONSE") or row.get("source") == "TOOL"):
            result_id = str(row.get("tool_call_id") or row.get("call_id") or "")
            index = next((i for i, call in enumerate(pending) if result_id and str(call.get("id") or call.get("tool_call_id") or "") == result_id), None)
            if result_id and index is None:
                continue
            call = pending.pop(0 if index is None else index)
            if row.get("status") not in ("DONE", "SUCCESS", "COMPLETED") or row.get("error") or row.get("isError") or row.get("success") is False:
                continue
            args = plain(call.get("args") or call.get("arguments") or {})
            if not isinstance(args, dict):
                continue
            if "action" not in args and isinstance(plain(args.get("arguments")), dict):
                args = plain(args["arguments"])
            action, operation = plain(args.get("action")), plain(args.get("operation"))
            content = str(row.get("content", ""))
            body = content[content.find("{"):] if "{" in content else ""
            try:
                receipt = json.JSONDecoder().raw_decode(body)[0]
            except ValueError:
                receipt = {}
            for _ in range(3):
                if not isinstance(receipt, dict) or receipt.get("isError"):
                    receipt = {}
                    break
                if isinstance(receipt.get("result"), dict):
                    receipt = receipt["result"]
                elif isinstance(receipt.get("content"), list):
                    try:
                        receipt = json.loads(next(item["text"] for item in receipt["content"] if item.get("type") == "text"))
                    except (ValueError, StopIteration, KeyError, TypeError):
                        receipt = {}
                else:
                    break
            if action == "explore" and operation in ("read", "fetch", "parse") and receipt.get("ok") is True and str(receipt.get("text", "")).strip():
                completed.add("explore." + operation)
    return completed


def operate(broker, token, operation, *, root_id="output", path="", url="", query="", format="text", offset=0, max_chars=6000, limit=40):
    context = broker.context(token)
    from console_tools.contracts import capabilities
    if operation not in capabilities(context).get("explore", []):
        raise PermissionError("当前上下文没有自主阅读能力。")
    if operation == "sources":
        return {"sources": [{"root_id": name, "path": str(root), "exists": root.is_dir(), "access": "read"} for name, root in roots_for(context).items()],
                "instruction": "自主挑选有兴趣的文本；fetch 取得网页源码，parse 提取正文/链接/JSON 字段，按页读取，交互和视觉内容才用截图。"}
    broker.require_effect_context(token)
    if operation == "list":
        target = source_path(context, root_id, path)
        if not target.is_dir():
            raise ValueError("资料目录不存在或不是目录。")
        result = []
        for entry in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
            if entry.name.startswith(".") or not entry.resolve().is_relative_to(target) or (not entry.is_dir() and entry.suffix.lower() not in TEXT_SUFFIXES):
                continue
            if query and query.casefold() not in entry.name.casefold():
                continue
            result.append({"name": entry.name, "directory": entry.is_dir(), "path": str(entry.relative_to(roots_for(context)[root_id]))})
        return {"files": result[offset:offset + limit], "next_offset": offset + limit if len(result) > offset + limit else None}
    if operation == "fetch":
        if (context.get("exploration_config") or {}).get("network") is False:
            raise PermissionError("当前探索未授权网络。")
        data, mime, final_url = fetch_page(url)
        if not (mime.startswith("text/") or mime.split(";")[0] in ("application/json", "application/xml", "application/xhtml+xml")):
            raise ValueError("当前资源不是文本；使用匹配的媒体工具处理。")
        text = decode_text(data, mime)
        workspace = Path(context.get("output_dir") or (context.get("exploration_config") or {}).get("workspace", "")).resolve()
        if not context.get("output_dir") and not (context.get("exploration_config") or {}).get("workspace"):
            raise PermissionError("没有绑定的探索工作目录。")
        workspace.mkdir(parents=True, exist_ok=True)
        source = workspace / ("page-" + hashlib.sha256(final_url.encode()).hexdigest()[:20] + (".json" if "json" in mime else ".html"))
        source.write_text(text, encoding="utf-8")
        value = parse_text(text, format, query, final_url)
        return {"url": final_url, "mime": mime, "source_file": str(source), "root_id": "output" if context.get("output_dir") else "workspace",
                "path": source.name, "text": value[offset:offset + max_chars], "truncated": len(value) > offset + max_chars,
                "next_offset": offset + max_chars if len(value) > offset + max_chars else None}
    target = source_path(context, root_id, path)
    if not target.is_file() or target.suffix.lower() not in TEXT_SUFFIXES or target.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("请选取 2 MB 内的已授权文本文件；目录用 list 查看。")
    text = decode_text(target.read_bytes())
    value = parse_text(text, format, query) if operation == "parse" else text
    return {"root_id": root_id, "path": path, "text": value[offset:offset + max_chars], "truncated": len(value) > offset + max_chars,
            "next_offset": offset + max_chars if len(value) > offset + max_chars else None}


def ensure_exploration(coordinator, key, *, source_token="", goal="", force=False):
    """Keep one persistent research identity and at most one active instruction."""
    cfg = coordinator.client.app_config.bot
    if not (cfg.self_activity_enabled and cfg.autonomous_enabled):
        return None
    metadata = coordinator.broker.context(source_token) if source_token else {}
    source = (coordinator.context_for(key, metadata["scope"]) if source_token
              else coordinator.contexts.get(key))
    if source is None or not source.guard():
        return None
    from agent_mailbox import AgentMailbox
    from runtime_control import session_generation
    mailbox = AgentMailbox(coordinator.broker)
    session = coordinator.client.ai.get_or_create_session(key)
    if goal:
        session.chat_state["autonomy_goal"] = goal
    with coordinator.broker.connect() as db:
        for row in db.execute("SELECT r.id,r.state,c.payload FROM requests r JOIN contexts c ON r.token=c.token "
                "WHERE r.namespace=? AND r.state IN ('submitted','queued','running')", (coordinator.namespace,)):
            context = json.loads(row["payload"])
            if context.get("self_directed") and context.get("session_key") == key and context.get("generation") == session.generation:
                return {"id": row["id"], "existing": True, "state": row["state"]}
    for agent in mailbox.all(coordinator.namespace):
        if agent["parent_key"] == key and agent["generation"] == session_generation(coordinator.client.ai, key) and agent["autonomous"]:
            with coordinator.broker.connect() as db:
                active = db.execute("SELECT task_id FROM agent_mail WHERE agent_id=? AND direction='to_agent' AND state IN ('queued','running') LIMIT 1", (agent["id"],)).fetchone()
            if active:
                return {"id": agent["id"], "task_id": active[0], "existing": True}
    if not force and time.time() - session.chat_state.get("last_exploration", 0) < cfg.self_activity_interval_minutes * 60:
        return None
    from companion_life import state_for
    personal = state_for(coordinator.broker, coordinator.namespace, key)
    interests = personal.get("interests", [])[-6:]
    binding = __import__("bot_identity").bind(coordinator.client, key)
    questions = coordinator.identity.events(binding["identity_owner"], source.scope, limit=8)["events"]
    instruction = goal or session.chat_state.get("autonomy_goal") or "结合自己的兴趣选择下一份有趣资料，实际阅读，记录新发现及下一步；可以先比较本机资料与网络线索，不重复上次成果。"
    metadata = {**metadata, "namespace": coordinator.namespace, "scope": source.scope, "session_key": key,
        "generation": session.generation, "main_agent": True, "autonomous_agents": True, "idle_event": True, "self_activity": True,
        "turn_managed": False, "is_master": False, "is_admin": False, "sender_id": "", "source_message_id": "",
        "self_activity_config": {"enabled": True}, "exploration_config": configuration(coordinator, key, source=source),
        "work_permissions": cfg.get_work_permissions(source.conversation_id if source.is_group else "").model_dump(),
        "ack_before_work": bool(source_token), "ack_token": source_token}
    context_file = coordinator.broker.issue_context(metadata, coordinator.directory(key) / "contexts")
    token = json.loads(Path(context_file).read_text(encoding="utf-8"))["token"]
    receipt = mailbox.operate(token, "create", name=EXPLORER_NAME, task_type="search", instructions=EXPLORER_RULES,
        text=json.dumps({"goal": instruction, "interests": interests,
            "unresolved": [{"id": row["id"], "title": row["title"]} for row in questions if row.get("status") == "open"],
            "previous_discovery": session.chat_state.get("last_discovery", "")}, ensure_ascii=False))
    session.chat_state["last_exploration"] = time.time()
    coordinator.broker.seal_messages(token)
    coordinator.client.ai._save_sessions()
    return receipt
