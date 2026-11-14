"""Durable handoffs between agent tools, the console worker and main chat."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from console_tools.files import ConsoleFileError, attachment_id, outgoing_files
from console_tools.contracts import TASK_TYPES
from persona_isolation import context_persona
from console_tools.outgoing import OutgoingCommands
from console_tools.speech import SpeechCommands


class ConsoleBroker(OutgoingCommands, SpeechCommands):
    def __init__(self, path, *, initialize=True):
        self.path = Path(path).resolve()
        if not initialize:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS contexts(token TEXT PRIMARY KEY, namespace TEXT,
                    scope TEXT, payload TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, token TEXT, namespace TEXT,
                    scope TEXT, kind TEXT, task TEXT, state TEXT, result TEXT, created REAL,
                    request_key TEXT, UNIQUE(token, request_key));
                CREATE INDEX IF NOT EXISTS console_pending ON requests(namespace,state,created);
                CREATE TABLE IF NOT EXISTS received_files(id TEXT PRIMARY KEY, namespace TEXT, scope TEXT,
                    source_message_id TEXT, sender_id TEXT, info TEXT, created REAL);
                CREATE INDEX IF NOT EXISTS console_inbox ON received_files(namespace,scope,created);
                CREATE TABLE IF NOT EXISTS reply_controls(token TEXT PRIMARY KEY, mode TEXT, reason TEXT);
                CREATE TABLE IF NOT EXISTS reply_batches(token TEXT, commit_key TEXT, receipt TEXT,
                    created REAL, PRIMARY KEY(token,commit_key));
                CREATE TABLE IF NOT EXISTS reply_targets(token TEXT, segment INTEGER, label TEXT,
                    quoted INTEGER, PRIMARY KEY(token,segment));
                CREATE TABLE IF NOT EXISTS chat_messages(namespace TEXT, scope TEXT, id TEXT,
                    sender_id TEXT, text TEXT, received_at REAL, PRIMARY KEY(namespace,scope,id));
                CREATE TABLE IF NOT EXISTS outgoing(id TEXT PRIMARY KEY, token TEXT, request_key TEXT,
                    payload TEXT, state TEXT, receipt TEXT, created REAL, updated REAL, UNIQUE(token,request_key));
                CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY, namespace TEXT, scope TEXT,
                    sender_id TEXT, source_message_id TEXT, kind TEXT, text TEXT, created REAL,
                    UNIQUE(namespace,scope,source_message_id,kind));
            """)
            from agent_mailbox import initialize
            initialize(db)
            from console_tools.speech import initialize as initialize_speech
            initialize_speech(db)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def issue_context(self, payload, directory):
        token = secrets.token_urlsafe(32)
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (hashlib.sha256(token.encode()).hexdigest()[:24] + ".json")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            payload = self._bind_speech(db, payload)
            db.execute("INSERT INTO contexts VALUES(?,?,?,?,?)", (token, payload["namespace"],
                payload["scope"], json.dumps(payload, ensure_ascii=False),
                None if payload.get("sandbox_session_id") or payload.get("agent_worker_id") else time.time() + 86400))
            for message in payload.get("reply_sources", []):
                if message.get("id"):
                    db.execute("INSERT OR IGNORE INTO chat_messages VALUES(?,?,?,?,?,?)", (
                        payload["namespace"], payload["scope"], str(message["id"]), str(message.get("sender_id", "")),
                        str(message.get("text", "")), message.get("received_at", time.time())))
        path.write_text(json.dumps({"database": str(self.path), "token": token}, ensure_ascii=False), encoding="utf-8")
        return str(path)

    @classmethod
    def from_context(cls, context_file):
        data = json.loads(Path(context_file).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or any(not isinstance(data.get(key), str) or not data[key].strip() for key in ("database", "token")):
            raise ConsoleFileError("CONTEXT_INVALID", "上下文文件缺少有效的 database/token。", "使用当前事件提供的 context_file，无需打开、编辑或生成凭据。")
        if not Path(data["database"]).is_file():
            raise ConsoleFileError("CONTEXT_INVALID", "上下文对应的控制台数据库已不存在。", "使用 QQBot 当前事件提供的 context_file。")
        # The bot owns schema migrations. A CLI/MCP call only opens the existing
        # database; repeated DDL contends with replies and background workers.
        broker = cls(data["database"], initialize=False)
        broker.context(data["token"])
        return broker, data["token"]

    def context(self, token, *, allow_expired=False):
        with self.connect() as db:
            row = db.execute("SELECT payload,expires FROM contexts WHERE token=?", (token,)).fetchone()
        if not row or (not allow_expired and row["expires"] is not None and row["expires"] <= time.time()):
            raise ConsoleFileError("CONTEXT_EXPIRED", "调用上下文已过期或失效。", "使用当前消息的 console_context_file；不要修改凭据或重放未知操作。")
        return json.loads(row["payload"])

    def submit(self, token, kind, task, request_key="", *, provider="", model=""):
        context = self.context(token)
        if (context.get("sandbox_session_id") or context.get("parent_request_id") or context.get("idle_event")
                or context.get("agent_worker_id")):
            raise PermissionError("游戏工作会话不再派任务；只使用当前沙箱接口。")
        if kind not in TASK_TYPES:
            raise ValueError("未知任务类型：" + str(kind))
        task = str(task).strip()
        if not task:
            raise ValueError("任务要求不能为空。")
        from command_backends import backend_options
        route = backend_options(kind, provider, model)
        return self._enqueue(token, context, kind, task, {**self.task_inputs(token, task, kind), **route}, request_key)

    def task_inputs(self, token, task, kind):
        """Bind inputs at submission, before newer uploads can change 'latest'."""
        import re
        context = self.context(token)
        identities = list(dict.fromkeys(re.findall(r"(?<![a-f0-9])[a-f0-9]{24}(?![a-f0-9])", task)))
        scope = (context["namespace"], context["scope"])
        # The displayed inbox is paginated. A quoted upload or explicit ID
        # remains usable after newer messages have pushed it off that page.
        with self.connect() as db:
            selected = []
            if identities:
                marks = ",".join("?" for _ in identities)
                selected = db.execute("SELECT id FROM received_files WHERE namespace=? AND scope=? "
                    f"AND id IN ({marks}) ORDER BY created DESC,id", (*scope, *identities)).fetchall()
            if not selected:
                selected = db.execute("SELECT id FROM received_files WHERE namespace=? AND scope=? "
                    "AND source_message_id IN (?,?) ORDER BY created DESC,id",
                    (*scope, context.get("source_message_id"), context.get("quoted_message_id"))).fetchall()
            if not selected and (kind in ("code", "process") or re.search(
                    r"附件|刚才.{0,8}(?:文件|图片)|最近.{0,8}(?:文件|上传)", task)):
                selected = db.execute("SELECT id FROM received_files WHERE namespace=? AND scope=? "
                    "AND sender_id=? ORDER BY created DESC,id LIMIT 1", (*scope, context.get("sender_id"))).fetchall()
        return {"input_file_ids": [item["id"] for item in selected]}

    def read_memory(self, token, path, *, view="summary", query="", offset=0, max_chars=4000):
        """Only controller-listed memory files are readable by the main agent."""
        context = self._chat_context(token, allow_committed=True)
        target = Path(path).resolve()
        allowed = {str(Path(value).resolve()) for value in context.get("memory_files", [])}
        if str(target) not in allowed:
            raise PermissionError("主代理只能读取本轮清单中的记忆文件；附件与源码请提交工作子代理。")
        from memory_digest import read_memory_page
        return {"ok": True, "path": str(target), **read_memory_page(target, user_id=context.get("sender_id", ""),
            view=view, query=query, offset=offset, max_chars=max_chars)}

    def stickers(self, token, query="", limit=6):
        from sticker_library import StickerLibrary
        context = self.context(token)
        cfg = context.get("sticker_config") or {}
        if not cfg.get("enabled"):
            return []
        return StickerLibrary(cfg["directory"]).search(query, limit)

    def send_sticker(self, token, identity, request_key=""):
        context = self._chat_context(token, allow_committed=True)
        cfg = context.get("sticker_config") or {}
        if not cfg.get("enabled") or context.get("sandbox_session_id"):
            raise ValueError("当前会话未启用表情包发送。")
        if self.reply_control(token).get("mode") == "wait":
            raise ValueError("本轮已选择等待，不再发送表情包。")
        from sticker_library import StickerLibrary
        library = StickerLibrary(cfg["directory"])
        if not any(item["id"] == identity for item in library.search("", 50)):
            # A large library is paginated for display; ID lookup remains exact.
            with library.connect() as db:
                if not db.execute("SELECT 1 FROM stickers WHERE id=?", (identity,)).fetchone():
                    raise ValueError("表情包 ID 无效，请先使用 stickers 搜索。")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_active_turn(db, token)
            self._require_speech_ready(db, token)
            existing = db.execute("SELECT id,state FROM requests WHERE token=? AND kind='sticker'", (token,)).fetchone()
            if existing:
                return dict(existing)
            recent = db.execute("SELECT 1 FROM requests WHERE namespace=? AND scope=? AND kind='sticker' "
                "AND state NOT IN ('failed','cancelled') AND created>?", (context["namespace"], context["scope"],
                    time.time() - cfg.get("cooldown_seconds", 60))).fetchone()
            if recent:
                raise ValueError("当前群聊表情包发送间隔未到，继续用自然语言交流即可。")
            identity_request = uuid.uuid4().hex
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)", (identity_request, token, context["namespace"],
                context["scope"], "sticker", "发送表情包", "submitted", json.dumps({"sticker_id": identity}), time.time(), "sticker"))
        return {"id": identity_request, "state": "submitted"}

    def require_effect_context(self, token):
        """An ended main turn loses write access; bound workers keep their grant."""
        context = self.context(token)
        if not any(context.get(key) for key in ("sandbox_session_id", "parent_request_id", "agent_worker_id")):
            with self.connect() as db:
                try:
                    self._require_active_turn(db, token)
                except ConsoleFileError as error:
                    error.input_posted = False
                    raise
        return context

    def desktop(self, token, operation, **params):
        if operation not in ("targets", "screenshot", "inspect"):
            self.require_effect_context(token)
        if self.context(token).get("activity_controller"):
            raise PermissionError("主对话的阅读控制上下文只调整方向和读取反馈，键鼠由执行分支串行操作。")
        from console_tools.desktop import DesktopControl
        return DesktopControl(self, token).run(operation, **params)

    def sandbox(self, token, action, *, executable="", target_id="", session_id="", observation_id="",
                key="", x=None, y=None, button="left", text="", evidence=None, kind="reflection", query="", path="",
                writable=False, grant_id="", content="", enabled=False, url="", profile="", delta=0, limit=3, element_id=""):
        from sandbox_runtime import SandboxRuntime
        context = self.context(token)
        if action not in ("memories", "notes", "status", "activities", "environment", "games", "apps", "observe", "list", "read"):
            context = self.require_effect_context(token)
        if context.get("activity_controller"):
            if action not in ("steer", "activities", "memories", "notes", "status", "pause", "skip", "restart"):
                raise PermissionError("阅读控制上下文不能新授权软件、账号、文件或派生任务。")
            session_id = context["sandbox_session_id"]
            if target_id and target_id != context["activity_target_id"]:
                raise PermissionError("阅读控制上下文不能调整其他目标。")
            target_id = context["activity_target_id"]
        if action in ("play", "use", "browse", "autoplay", "grant") and not context.get("sandbox_session_id"):
            self._chat_context(token, allow_committed=True)
        cfg = context.get("sandbox_config") or {}
        if not cfg.get("enabled"):
            raise ValueError("沙箱接口已关闭。")
        mode = cfg.get("mode", "background")
        runtime = SandboxRuntime(cfg["directory"], max_actions=cfg.get("max_actions", 0), vm_config=cfg.get("vm"), mode=mode)
        if action == "memories":
            from activity_memory import activity_memories
            return {"memories": activity_memories(runtime, context, query, limit)}
        if action == "notes":
            return {"notes": runtime.notes(context, query)}
        runtime.require_owner(context)
        if action in ("skip", "restart"):
            from activity_recovery import request_recovery
            if context.get("sandbox_session_id") and not context.get("activity_controller"):
                raise PermissionError("执行分支不能自行重启；恢复由主对话或控制台执行。")
            if not session_id:
                from activity_control import snapshots
                candidates = [item for item in snapshots(self, context)
                              if not target_id or item["target_id"] == target_id]
                if len(candidates) != 1:
                    raise ValueError("请选择要恢复的 session_id；activities 可查询暂停或失败任务。")
                session_id = candidates[0]["session_id"]
            session = runtime.session(context, session_id)
            request = self.record(session["request_id"])
            receipt = request_recovery(runtime, context, session_id, action, url=url, source_token=token)
            if receipt["state"] == "paused":
                return {"id": request["id"], **receipt}
            if request["state"] in ("paused", "failed"):
                payload = {**json.loads(request["result"]), "resume_session_id": session_id, "recovery": receipt}
                # A running task can outlive the original chat token. An
                # explicitly authorized recovery renews its original scope;
                # the controller's broader/current chat input is not replayed.
                original = self.context(request["token"], allow_expired=True)
                # This is an explicit continuation of an already acknowledged
                # activity, not a new user request awaiting an initial reply.
                original = {**original, "ack_before_work": False}
                original.pop("ack_token", None)
                renewed_file = self.issue_context(original, self.path.parent / "recovery_contexts")
                renewed_token = json.loads(Path(renewed_file).read_text(encoding="utf-8"))["token"]
                with self.connect() as db:
                    db.execute("UPDATE requests SET state='submitted',result=?,token=? WHERE id=? AND state IN ('paused','failed')",
                               (json.dumps(payload, ensure_ascii=False), renewed_token, request["id"]))
            return {"id": request["id"], **receipt}
        if action == "activities":
            from activity_control import snapshots
            return {"activities": snapshots(self, context)}
        if action == "steer":
            from activity_control import active_request, redirect
            if context.get("sandbox_session_id") and not context.get("activity_controller"):
                raise PermissionError("执行分支不能自行更改主对话的阅读目标。")
            if session_id:
                target_id = runtime.session(context, session_id)["target"]
            if not target_id:
                candidates = [game for game in runtime.games(context) if active_request(self, context, game["id"])]
                if len(candidates) != 1:
                    raise ValueError("请选择要调整的 target_id 或 session_id。")
                target_id = candidates[0]["id"]
            runtime.game(context, target_id)
            request = active_request(self, context, target_id)
            if not request or not str(text).strip():
                raise ValueError("没有可调整的运行任务，或缺少具体阅读方向。")
            return redirect(self, runtime, context, request, text, url, token)
        if action == "environment":
            return runtime.device().status()
        if action in ("games", "apps", "play", "use", "browse", "autoplay", "grant", "stop") and context.get("sandbox_session_id"):
            raise PermissionError("工作会话不能新授权路径、开启自主游玩或派生任务。")
        if action in ("games", "apps"):
            return {action: runtime.games(context, "software" if action == "apps" else "game"), "mode": mode,
                    "os_isolated": mode == "qemu", "host_input": False,
                    "profiles": list(cfg.get("profiles", {})), "browser_profiles": list(cfg.get("browser_profiles", {}))}
        if action in ("play", "use", "browse"):
            target_kind = "game" if action == "play" else "software"
            if target_kind == "software" and mode != "background":
                raise ValueError("通用软件当前使用 background 模式；请在控制台选择后台窗口后继续。")
            if action == "browse":
                if executable or target_id:
                    raise ValueError("browse 只接收 url/profile/text；指定软件路径时使用 use。")
                game = runtime.register_browser(context, url, token, profile=profile, goal=text or "浏览 " + url + "，自主阅读帖子或观看视频，记录实际观察。")
            else:
                game = (runtime.register(context, executable, token, kind=target_kind, url=url, profile=profile, goal=text)
                        if executable else runtime.game(context, target_id))
            if game["kind"] != target_kind:
                raise ValueError("软件用 use，游戏用 play；请按目标类型选择。")
            if action != "browse" and not executable and (url or profile):
                raise ValueError("继续任务沿用原登录目录；更换网址请在任务目标中说明，由工作会话 navigate。")
            game = runtime.set_goal(context, game["id"], text, token)
            from activity_control import active_request, redirect
            running = active_request(self, context, game["id"])
            if running:
                if running["token"] == token and running["task"] == game["goal"]:
                    return {"id": running["id"], "state": running["state"]}
                return redirect(self, runtime, context, running, game["goal"], url, token)
            return self._enqueue(token, context, "sandbox_play", game["goal"] or
                                 ("持续浏览感兴趣的视频和帖子，记录实际观察和感受，直到主人停止。" if target_kind == "software" else "持续阅读游戏剧情，记录感受和当前进度，直到主人停止。"),
                                 {"target_id": game["id"], "quiet": False, **({"url": url} if action == "browse" else {})}, "play:" + game["id"])
        if action == "autoplay":
            game = runtime.game(context, target_id)
            runtime.set_goal(context, game["id"], text, token)
            receipt = {}
            if text:
                from activity_control import active_request, redirect
                current = active_request(self, context, target_id)
                if current:
                    receipt = redirect(self, runtime, context, current, text, "", token)
            with runtime.connect() as db:
                db.execute("UPDATE games SET autoplay=?,source_token=? WHERE id=?", (int(bool(enabled)), token, game["id"]))
            return {**receipt, "enabled": bool(enabled), "target_id": game["id"],
                    "scheduler_enabled": bool(cfg.get("autonomous_enabled")),
                    "hint": "已结束任务的自动续用还需控制台开启自主调度；运行中任务默认不限时、不限次数，结果只保存在本地。"}
        if action == "grant":
            return runtime.grant(context, path, writable=writable)
        if action == "stop":
            games = runtime.games(context)
            if target_id:
                games = [runtime.game(context, target_id)]
            stopped = []
            requests = []
            for game in games:
                with runtime.connect() as db:
                    db.execute("UPDATE games SET autoplay=0 WHERE id=?", (game["id"],))
                    rows = db.execute("SELECT id,request_id FROM sessions WHERE target=? AND state='running'", (game["id"],)).fetchall()
                    db.execute("UPDATE sessions SET state='cancelled',summary=? WHERE target=? AND state='running'",
                               ("主人要求停止游玩，保留游戏窗口与已有记录。", game["id"]))
                for row in rows:
                    stopped.append(row["id"])
                # A stop can arrive before begin() creates a session. Cancel
                # submitted/queued requests as well so they cannot launch later.
                with self.connect() as db:
                    pending = db.execute("SELECT id,result FROM requests WHERE namespace=? AND scope=? "
                        "AND kind='sandbox_play' AND state IN ('submitted','queued','running')",
                        (context["namespace"], context["scope"])).fetchall()
                    for record in pending:
                        if json.loads(record["result"] or "{}").get("target_id") == game["id"]:
                            db.execute("UPDATE requests SET state='cancel_requested' WHERE id=?", (record["id"],))
                            requests.append(record["id"])
            return {"sessions": stopped, "requests": requests, "state": "stopping",
                    "hint": "停止后续自动操作并关闭自主续玩；正在投递的单次操作可能完成。保留游戏窗口、存档和已有记录。"}
        session_id = context.get("sandbox_session_id") or session_id
        row = runtime.session(context, session_id)
        if action == "status":
            from activity_recovery import recovery
            return {**row, "direction": runtime.direction(context, session_id), "recovery": recovery(runtime, context, session_id)}
        request = self.record(row["request_id"])
        if not request or request["state"] != "running":
            raise ValueError("游玩请求已结束或已取消，禁止继续控制游戏。")
        if action == "observe":
            return runtime.observe(context, session_id)
        if action in ("finish", "pause"):
            receipt = runtime.request_end(context, session_id, action, text)
            return {**receipt, **({"autoplay": False, "reason": receipt["text"]} if action == "pause" else {}),
                    "hint": "已提交任务结束请求，直接结束本轮回复；控制台保存结果。"}
        if action in ("invoke", "fill", "select"):
            return runtime.act(context, session_id, observation_id, action, element_id=element_id, text=text,
                               guard=lambda: self.require_effect_context(token))
        if action in ("key", "click", "type", "scroll", "navigate"):
            return runtime.act(context, session_id, observation_id, action, key=key, x=x, y=y, button=button,
                               guard=lambda: self.require_effect_context(token),
                               **({"text": text, "delta": delta, "url": url} if action in ("type", "scroll", "navigate") else {}))
        if action == "note":
            return runtime.note(context, session_id, text, evidence, kind)
        if action in ("list", "read", "write"):
            return runtime.files(context, session_id, action, relative=path, grant_id=grant_id, content=content)
        raise ValueError("未知沙箱动作。使用 schema 查看 games/play/observe/key/click/note/notes/list/read/write。")

    def wait_for_message(self, token, reason=""):
        self.context(token)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._wait_in_transaction(db, token, reason)
        return {"ok": True, "state": "waiting", "next_action": "next_message",
                "instruction": "控制台已标记本轮等待。直接结束，不用轮询或读取源码；若后端要求文字，用简短自然语言结束即可，本轮文字不会发送。"}

    def _wait_in_transaction(self, db, token, reason=""):
        control = db.execute("SELECT mode FROM reply_controls WHERE token=?", (token,)).fetchone()
        if control and control[0] != "wait":
            self._require_open_turn(db, token)
        decision = db.execute("SELECT state FROM speech_decisions WHERE token=?", (token,)).fetchone()
        if decision and decision[0] == "released":
            raise ConsoleFileError("SPEECH_ALREADY_RELEASED", "本轮已有输出提交，不能再取消回答。")
        db.execute("INSERT OR REPLACE INTO reply_controls VALUES(?,?,?)", (token, "wait", str(reason)[:500]))
        db.execute("UPDATE outgoing SET state='cancelled' WHERE token=? AND state='staged'", (token,))
        db.execute("UPDATE requests SET state='cancel_requested' WHERE token=? AND kind='sticker' "
                   "AND state IN ('submitted','queued','running')", (token,))

    def reply_control(self, token):
        with self.connect() as db:
            row = db.execute("SELECT mode,reason FROM reply_controls WHERE token=?", (token,)).fetchone()
        return dict(row) if row else {}

    def select_reply(self, token, label, quote=True, segment=0):
        raise ConsoleFileError("REPLY_LABEL_RETIRED", "标签引用已停用。", "用 messages 查真实 ID，再调用 message --reply-to ID。")

    def reply_targets(self, token):
        with self.connect() as db:
            return {row["segment"]: {"label": row["label"], "quote": bool(row["quoted"])}
                    for row in db.execute("SELECT * FROM reply_targets WHERE token=?", (token,))}

    def _enqueue(self, token, context, kind, task, payload, request_key=""):
        if context.get("emotion_analysis"):
            raise PermissionError("情绪分析由主代理本人完成，不能派生其他任务。")
        # Repeating a tool call for the same message must not duplicate work.
        route = {key: payload[key] for key in ("provider", "model") if payload.get(key)}
        identity = kind + "\n" + task
        if route:
            identity += "\n" + json.dumps(route, sort_keys=True, ensure_ascii=False)
        request_key = request_key or hashlib.sha256(identity.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT id,state FROM requests WHERE token=? AND request_key=?",
                                  (token, request_key)).fetchone()
            if previous:
                return dict(previous)
            # Reply submission leaves work open. Natural completion or explicit
            # cancellation seals admission under this same transaction lock.
            self._require_active_turn(db, token)
            if kind == "send_files":
                self._require_speech_ready(db, token, release=True)
            from console_tools.work_permissions import authorize_work
            authorize_work(kind, context, payload)
            db.execute("INSERT OR IGNORE INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, token, context["namespace"], context["scope"], kind, task,
                 "submitted", json.dumps(payload, ensure_ascii=False), time.time(), request_key))
            row = db.execute("SELECT id,state FROM requests WHERE token=? AND request_key=?",
                (token, request_key)).fetchone()
        return dict(row)

    def send_files(self, token, files, request_key=""):
        context = self._chat_context(token, allow_committed=True)
        files = outgoing_files(context, files)
        request_key = request_key or "send:" + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        return self._enqueue(token, context, "send_files", f"发送 {len(files)} 个本地文件",
                             {"files": files, "delivery": {"approved_by": token, "mode": "main_agent",
                                 "text": "", "reply_to": "", "mention": ""}}, request_key)

    def record_files(self, namespace, scope, message_id, sender_id, files):
        ids = []
        with self.connect() as db:
            for item in files:
                key = attachment_id(namespace, scope, item)
                db.execute("INSERT OR IGNORE INTO received_files VALUES(?,?,?,?,?,?,?)",
                    (key, namespace, scope, str(message_id), str(sender_id), json.dumps(item, ensure_ascii=False), time.time()))
                ids.append(key)
        return ids

    def inbox(self, token, limit=20):
        context = self.context(token)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM received_files WHERE namespace=? AND scope=? ORDER BY created DESC,id LIMIT ?",
                (context["namespace"], context["scope"], min(100, max(1, int(limit))))).fetchall()
        files = []
        for row in rows:
            info = json.loads(row["info"])
            cached = bool(info.get("local_path") and Path(info["local_path"]).is_file())
            files.append({"file_id": row["id"], "name": info["name"], "size": info["size"],
                "source_message_id": row["source_message_id"], "sender_id": row["sender_id"],
                "state": "received" if cached else "available",
                **{key: info[key] for key in ("local_path", "sha256") if cached and info.get(key)}})
        return files

    def attachment(self, token, file_id):
        context = self.context(token)
        with self.connect() as db:
            row = db.execute("SELECT * FROM received_files WHERE id=? AND namespace=? AND scope=?",
                (file_id, context["namespace"], context["scope"])).fetchone()
        if row is None:
            raise ConsoleFileError("FILE_NOT_FOUND", "当前会话没有这份收件文件。", "先使用 inbox 获取 file_id。")
        return {**dict(row), "info": json.loads(row["info"])}

    def receive_file(self, token, file_id="latest", request_key=""):
        context = self.context(token)
        if not file_id or file_id == "latest":
            files = self.inbox(token, 100)
            current = [item for item in files if item["source_message_id"] == context.get("source_message_id")]
            choices = current or files[:1]
            if not choices:
                raise ConsoleFileError("NO_FILES", "当前会话还没有收到文件。", "收到群文件上传或私聊文件消息后使用 inbox。")
            if len(choices) != 1:
                raise ConsoleFileError("FILE_ID_REQUIRED", "本条消息包含多份文件。", "使用 inbox 列出的 file_id 分别接收。")
            file_id = choices[0]["file_id"]
        record = self.attachment(token, file_id)
        return self._enqueue(token, context, "receive_file", "接收文件：" + record["info"]["name"],
            {"file_id": file_id}, request_key or "receive:" + file_id)

    def save_received_file(self, token, file_id, **updates):
        record = self.attachment(token, file_id)
        with self.connect() as db:
            db.execute("UPDATE received_files SET info=? WHERE id=?", (json.dumps({**record["info"], **updates},
                ensure_ascii=False), file_id))

    def pending(self, namespace):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM requests WHERE namespace=? AND state='submitted' ORDER BY created", (namespace,))]

    def update(self, request_id, state, result=None):
        with self.connect() as db:
            if result is None:
                db.execute("UPDATE requests SET state=? WHERE id=?", (state, request_id))
            else:
                db.execute("UPDATE requests SET state=?,result=? WHERE id=?",
                    (state, json.dumps(result, ensure_ascii=False, default=str), request_id))

    def update_running_result(self, request_id, **fields):
        """Merge progress atomically without undoing cancellation or steering."""
        if not fields:
            return False
        assignments = ", ".join("?, json(?)" for _ in fields)
        values = [value for name, data in fields.items()
                  for value in ("$." + name, json.dumps(data, ensure_ascii=False, default=str))]
        with self.connect() as db:
            changed = db.execute("UPDATE requests SET result=json_set(result, " + assignments +
                                 ") WHERE id=? AND state='running'", (*values, request_id))
            return changed.rowcount == 1

    def status(self, token, request_id):
        context = self.context(token)
        if context.get("sandbox_session_id") and context.get("parent_request_id") and request_id != context["parent_request_id"]:
            raise PermissionError("工作和阅读控制上下文只查询绑定任务。")
        with self.connect() as db:
            row = db.execute("SELECT id,kind,task,state,result,token,created FROM requests WHERE id=? AND namespace=? AND scope=?",
                (request_id, context["namespace"], context["scope"])).fetchone()
        if not row:
            raise ValueError("当前会话中没有这条任务记录。")
        req_token = row["token"]
        if req_token != token:
            req_ctx = self.context(req_token, allow_expired=True)
            if context_persona(req_ctx) != context_persona(context):
                raise PermissionError("无权访问其他人设的任务。")
            req_sender = str(req_ctx.get("sender_id") or req_ctx.get("user_id") or req_ctx.get("owner") or "").strip()
            caller_sender = str(context.get("sender_id") or context.get("user_id") or context.get("owner") or "").strip()
            if (req_sender or caller_sender) and req_sender != caller_sender:
                raise PermissionError("无权访问其他发起者的任务。")
        res_data = dict(row)
        res_data.pop("token", None)
        return {**res_data, "result": json.loads(row["result"])}

    def list_requests(self, token, states=(), limit=20):
        context = self.context(token)
        sender = str(context.get("sender_id") or context.get("user_id") or context.get("owner") or "").strip()
        query = ("SELECT r.id FROM requests r JOIN contexts c ON c.token=r.token WHERE r.namespace=? AND r.scope=? "
            "AND trim(COALESCE(NULLIF(json_extract(c.payload,'$.sender_id'),''),"
            "NULLIF(json_extract(c.payload,'$.user_id'),''),json_extract(c.payload,'$.owner'),''))=? ")
        params = [context["namespace"], context["scope"], sender]
        persona = context_persona(context)
        # Legacy callers have no persona; a scoped caller sees only its exact
        # identity. SQL LIKE's wildcard characters never occur in the hash.
        if persona:
            query += "AND COALESCE(json_extract(c.payload,'$.session_key'),'') LIKE ? "
            params.append("%::persona:" + persona)
        else:
            query += "AND instr(COALESCE(json_extract(c.payload,'$.session_key'),''),'::persona:')=0 "
        if context.get("sandbox_session_id"):
            # The bound capability does not expose the owner's other work.
            query += "AND r.id=? "
            params.append(context.get("parent_request_id", ""))
        if states:
            query += "AND r.state IN (" + ",".join("?" for _ in states) + ") "
            params.extend(states)
        params.append(min(100, max(1, int(limit))))
        with self.connect() as db:
            return [row[0] for row in db.execute(query + "ORDER BY r.created DESC,r.id LIMIT ?", params)]


    def record(self, request_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def completed(self, namespace, scope):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM requests WHERE namespace=? AND scope=? AND state='ready' ORDER BY created", (namespace, scope))]

    def active(self, namespace):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM requests WHERE namespace=? AND state IN ('queued','running','ready') ORDER BY created", (namespace,))]

    def cancellations(self, namespace):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM requests WHERE namespace=? AND state='cancel_requested'", (namespace,))]

