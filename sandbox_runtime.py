"""Audited software/game control with separate observations and learning records.

Games use a private background desktop or an explicitly configured QEMU guest.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from persona_isolation import context_persona
import re
import sqlite3
import time
import uuid


class ActivityDirectionChanged(ValueError):
    code = "ACTIVITY_DIRECTION_CHANGED"
    hint = "主对话已调整目标；旧轮次停止操作，等待控制台送入最新方向和观察。"


class SandboxRuntime:
    def __init__(self, directory="data/sandbox", *, backend=None, max_actions=0, vm_config=None, mode="background"):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.backend = backend
        self.max_actions = int(max_actions)
        if self.max_actions < 0:
            raise ValueError("操作次数上限不能为负数；0 表示不限次数。")
        self.vm_config = vm_config or {}
        self.mode = mode
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS games(id TEXT PRIMARY KEY, namespace TEXT, scope TEXT, owner TEXT,
                    executable TEXT, title TEXT, source_token TEXT, autoplay INTEGER DEFAULT 0, last_run REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, namespace TEXT, scope TEXT, owner TEXT,
                    target TEXT, request_id TEXT, state TEXT, actions INTEGER, created REAL, summary TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_game ON sessions(target) WHERE state='running';
                CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,session TEXT,data TEXT,created REAL,used INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,session TEXT,state TEXT,data TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY,namespace TEXT,scope TEXT,target TEXT,session TEXT,
                    text TEXT,kind TEXT,evidence TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS grants(id TEXT PRIMARY KEY,namespace TEXT,scope TEXT,owner TEXT,path TEXT,writable INTEGER);
                CREATE TABLE IF NOT EXISTS session_controls(session TEXT PRIMARY KEY,action TEXT,text TEXT);
                CREATE TABLE IF NOT EXISTS session_directions(session TEXT PRIMARY KEY,revision INTEGER,text TEXT,url TEXT,
                    source_token TEXT,updated REAL,applied_revision INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS direction_receipts(id TEXT PRIMARY KEY,session TEXT,revision INTEGER);
                CREATE TABLE IF NOT EXISTS session_recoveries(session TEXT PRIMARY KEY,revision INTEGER,
                    applied_revision INTEGER,action TEXT,url TEXT,code TEXT,blocked_urls TEXT,
                    consecutive INTEGER,state TEXT,updated REAL);
                CREATE TABLE IF NOT EXISTS recovery_receipts(id TEXT PRIMARY KEY,revision INTEGER);
                CREATE TABLE IF NOT EXISTS activity_digests(target TEXT PRIMARY KEY,cursor INTEGER,data TEXT,
                    recovery TEXT,updated REAL,session TEXT);
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(games)")}
            for name, declaration in (("kind", "TEXT DEFAULT 'game'"), ("launch_url", "TEXT DEFAULT ''"),
                                      ("profile", "TEXT DEFAULT ''"), ("profile_path", "TEXT DEFAULT ''"),
                                      ("goal", "TEXT DEFAULT ''")):
                if name not in columns:
                    db.execute(f"ALTER TABLE games ADD COLUMN {name} {declaration}")
            if "persona" not in columns:
                db.execute("ALTER TABLE games ADD COLUMN persona TEXT DEFAULT ''")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "sandbox.sqlite3", timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def require_owner(context):
        if not context.get("is_master") and not context.get("self_directed"):
            raise PermissionError("只有配置的主人可以授权路径、启动游戏或操作沙箱。")

    def register(self, context, executable, source_token="", *, kind="game", url="", profile="", goal=""):
        self._require_registration(context)
        path = Path(str(executable).strip().strip('"')).expanduser()
        source = str(context.get("source_text") or "").replace("/", "\\").casefold()
        if str(path).replace("/", "\\").casefold() not in source:
            raise PermissionError("游戏路径必须由主人在当前 QQ 消息中明确提供，不能由 agent 自行猜测。")
        return self._register_target(context, path, source_token, kind=kind, url=url, profile=profile, goal=goal)

    def register_browser(self, context, url, source_token="", *, profile="", goal=""):
        self._require_registration(context)
        from browser_control import checked_url, installed_browser
        from browser_requests import browser_urls
        url = checked_url(url)
        if not context.get("self_directed") and url not in browser_urls(context.get("source_text", "")):
            raise PermissionError("浏览网址必须由主人在当前 QQ 消息中明确提供。")
        configured = (context.get("sandbox_config") or {}).get("browser_executable", "")
        if not configured:
            from browser_control import browser_executable
            for target in self.games(context, "software"):
                if target["profile"] == profile and browser_executable(target["executable"]) and Path(target["executable"]).is_file():
                    configured = target["executable"]
                    break
        path = installed_browser(configured)
        return self._register_target(context, path, source_token, kind="software", url=url, profile=profile, goal=goal)

    def _require_registration(self, context):
        self.require_owner(context)
        if context.get("sandbox_session_id"):
            raise PermissionError("工作任务不能自行授权新的软件路径。")

    def _register_target(self, context, path, source_token, *, kind, url, profile, goal):
        if kind not in ("game", "software"):
            raise ValueError("目标类型必须为 game/software。")
        path = path.resolve(strict=True)
        forbidden = {"cmd.exe", "powershell.exe", "pwsh.exe", "python.exe", "pythonw.exe", "wscript.exe", "cscript.exe",
                     "mshta.exe", "rundll32.exe", "regsvr32.exe", "explorer.exe", "powershell_ise.exe",
                     "wt.exe", "wsl.exe", "bash.exe", "sh.exe", "py.exe"}
        system = Path(os.environ.get("SystemRoot", "C:/Windows")).resolve()
        if (not path.is_file() or path.suffix.casefold() != ".exe" or path.name.casefold() in forbidden
                or kind == "game" and (path.is_relative_to(system) or path.name.casefold() in ("msedge.exe", "chrome.exe", "firefox.exe"))
                or path.parent == Path(path.anchor)):
            raise ValueError("请提供应用目录内的 .exe；游戏入口不运行浏览器，软件入口不运行命令或脚本解释器。")
        from browser_control import browser_executable, checked_url
        if url:
            url = checked_url(url)
        if (url or profile) and (kind != "software" or not browser_executable(path)):
            raise ValueError("url/profile 仅用于受支持的 Chromium 浏览器（Chrome/Edge/Chromium）。")
        if kind == "software" and self.mode != "background":
            raise ValueError("通用软件当前使用 background 模式；QEMU 保留原游戏控制流程。")
        identity = hashlib.sha256((context["namespace"] + context["scope"] + context["sender_id"] + str(path).casefold()).encode()).hexdigest()[:24]
        if context_persona(context):
            identity = hashlib.sha256((identity + context_persona(context)).encode()).hexdigest()[:24]
        profile_path = ""
        if kind == "software":
            identity = hashlib.sha256((identity + ":software:" + profile).encode()).hexdigest()[:24]
            if browser_executable(path):
                configured = (context.get("sandbox_config") or {}).get("browser_profiles", {})
                if profile and profile not in configured:
                    raise PermissionError("浏览器登录目录别名未配置，请先在控制台添加专用登录目录。")
                profile_root = Path(configured[profile]).resolve() if profile else self.root / "browser_profiles" / identity
                if profile_root == Path(profile_root.anchor) or profile_root.exists() and not profile_root.is_dir():
                    raise ValueError("浏览器登录目录必须是专用文件夹。")
                profile_path = str(profile_root)
        with self.connect() as db:
            db.execute("INSERT INTO games(id,namespace,scope,owner,executable,title,source_token,kind,launch_url,profile,profile_path,goal,persona) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_token=excluded.source_token,"
                       "goal=CASE WHEN excluded.goal<>'' THEN excluded.goal ELSE games.goal END,"
                       "launch_url=CASE WHEN excluded.launch_url<>'' THEN excluded.launch_url ELSE games.launch_url END",
                       (identity, context["namespace"], context["scope"], context["sender_id"], str(path),
                        path.stem if kind == "software" else path.parent.name, source_token, kind, url, profile, profile_path, goal, context_persona(context)))
        return self.game(context, identity)

    def games(self, context, kind=None):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,title,executable,autoplay,last_run,kind,profile,goal FROM games "
                "WHERE namespace=? AND scope=? AND owner=? AND persona=? " + ("AND kind=? " if kind else "") + "ORDER BY last_run DESC",
                (context["namespace"], context["scope"], context["sender_id"], context_persona(context)) + ((kind,) if kind else ()))]

    def set_goal(self, context, identity, goal, source_token):
        self.game(context, identity)
        with self.connect() as db:
            db.execute("UPDATE games SET source_token=?,goal=CASE WHEN ?<>'' THEN ? ELSE goal END WHERE id=?",
                       (source_token, goal, goal, identity))
        return self.game(context, identity)

    def game(self, context, identity):
        self.require_owner(context)
        with self.connect() as db:
            row = db.execute("SELECT * FROM games WHERE id=? AND namespace=? AND scope=? AND owner=? AND persona=?",
                (identity, context["namespace"], context["scope"], context["sender_id"], context_persona(context))).fetchone()
        if row is None:
            raise PermissionError("当前主人及会话未授权这款游戏。")
        return dict(row)

    def begin(self, context, identity, request_id):
        target = self.game(context, identity)
        if target["kind"] == "software" and self.mode != "background":
            raise ValueError("通用软件当前使用 background 模式；请在控制台选择后台窗口后继续。")
        sid = uuid.uuid4().hex
        try:
            with self.connect() as db:
                db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?)", (sid, context["namespace"], context["scope"],
                    context["sender_id"], identity, request_id, "running", 0, time.time(), ""))
                db.execute("UPDATE games SET last_run=? WHERE id=?", (time.time(), identity))
                db.execute("INSERT INTO session_directions VALUES(?,?,?,?,?,?,?)",
                    (sid, 0, target["goal"], "", target["source_token"], time.time(), 0))
        except sqlite3.IntegrityError as error:
            raise ValueError("这款游戏已有游玩任务；先查询或停止原任务。") from error
        (self.root / "sessions" / sid).mkdir(parents=True)
        return sid, target

    def session(self, context, sid):
        self.require_owner(context)
        if context.get("sandbox_session_id") and context["sandbox_session_id"] != sid:
            raise PermissionError("当前任务不能操作另一游玩会话。")
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=? AND namespace=? AND scope=? AND owner=?",
                (sid, context["namespace"], context["scope"], context["sender_id"])).fetchone()
        if row is None:
            raise PermissionError("无权访问这段游玩会话。")
        self.game(context, row["target"])
        return dict(row)

    def _active(self, context, sid):
        row = self.session(context, sid)
        if row["state"] != "running":
            raise ValueError("游玩会话已结束；启动新任务后继续，不重放旧操作。")
        self.check_direction(context, sid)
        return row, self.game(context, row["target"])

    def direction(self, context, sid):
        self.session(context, sid)
        with self.connect() as db:
            row = db.execute("SELECT * FROM session_directions WHERE session=?", (sid,)).fetchone()
        return dict(row) if row else {"session": sid, "revision": 0, "text": "", "url": "", "applied_revision": 0}

    def check_direction(self, context, sid):
        if context.get("sandbox_session_id") and not context.get("activity_controller"):
            current = self.direction(context, sid)["revision"]
            if context.get("activity_revision", 0) != current:
                raise ActivityDirectionChanged("主对话已更改阅读目标；未执行旧方向的输入、笔记或完成请求。")

    def steer(self, context, sid, text, *, url="", source_token=""):
        self.require_owner(context)
        row = self.session(context, sid)
        if context.get("sandbox_session_id") and not context.get("activity_controller"):
            raise PermissionError("执行分支不能自行替换主对话的阅读目标。")
        if row["state"] != "running" or not str(text).strip():
            raise ValueError("请为运行中的任务提供具体阅读方向。")
        if url:
            from browser_control import browser_executable, checked_url
            url = checked_url(url)
            if not browser_executable(self.game(context, row["target"])["executable"]):
                raise ValueError("网址调整只适用于已经授权的浏览器任务。")
        identity = hashlib.sha256(json.dumps([sid, source_token, text, url]).encode()).hexdigest()
        from vm_protocol import file_lock
        # Finish any already-posting input before committing the new direction.
        with file_lock(self.root / "sessions" / sid / "desktop.lock", timeout=12):
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute("SELECT revision FROM direction_receipts WHERE id=?", (identity,)).fetchone()
                if previous:
                    return {"session_id": sid, "direction_revision": previous[0], "repeated": True}
                active = db.execute("SELECT state FROM sessions WHERE id=?", (sid,)).fetchone()
                if active[0] != "running" or db.execute("SELECT 1 FROM session_controls WHERE session=?", (sid,)).fetchone():
                    raise ValueError("任务正在结束或已停止，请重新继续后再调整方向。")
                db.execute("INSERT INTO session_directions VALUES(?,1,?,?,?,?,0) ON CONFLICT(session) DO UPDATE SET "
                    "revision=revision+1,text=excluded.text,url=excluded.url,source_token=excluded.source_token,updated=excluded.updated",
                    (sid, str(text).strip(), url, source_token, time.time()))
                revision = db.execute("SELECT revision FROM session_directions WHERE session=?", (sid,)).fetchone()[0]
                db.execute("INSERT INTO direction_receipts VALUES(?,?,?)", (identity, sid, revision))
                db.execute("UPDATE games SET goal=?,source_token=CASE WHEN ? THEN source_token ELSE ? END,"
                    "launch_url=CASE WHEN ?<>'' THEN ? ELSE launch_url END WHERE id=?",
                    (str(text).strip(), bool(context.get("activity_controller")), source_token, url, url, row["target"]))
        return {"session_id": sid, "direction_revision": revision, "repeated": False}

    def acknowledge_direction(self, context, sid, revision):
        self._active(context, sid)
        with self.connect() as db:
            db.execute("UPDATE session_directions SET applied_revision=? WHERE session=? AND revision=?", (revision, sid, revision))

    def device(self):
        if self.backend is None:
            from game_window import GameWindow
            self.backend = GameWindow(self.vm_config, self.root, mode=self.mode)
        return self.backend

    def launch(self, context, sid):
        _, target = self._active(context, sid)
        return self.device().launch(target)

    def observe(self, context, sid):
        _, target = self._active(context, sid)
        with self.connect() as db:
            previous = db.execute("SELECT id FROM actions WHERE session=? AND state='posted' ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
        oid = uuid.uuid4().hex
        path = self.root / "sessions" / sid / (oid + ".png")
        revision = self.direction(context, sid)["revision"]
        data = self.device().observe(target, path)
        if self.direction(context, sid)["revision"] != revision:
            raise ActivityDirectionChanged("取图期间阅读方向已更新，请等待新方向的观察。")
        if not path.is_file():
            raise ValueError("游戏截图未生成。")
        data.update(id=oid, target_id=target["id"], image_path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    source="software_observation" if target["kind"] == "software" else "fictional_game",
                    game=target["title"], after_action=previous[0] if previous else "",
                    direction_revision=revision)
        with self.connect() as db:
            db.execute("INSERT INTO observations(id,session,data,created) VALUES(?,?,?,?)", (oid, sid, json.dumps(data), time.time()))
        return data

    def inspect(self, context, sid, observation_id):
        _, target = self._active(context, sid)
        with self.connect() as db:
            obs = db.execute("SELECT * FROM observations WHERE id=? AND session=?", (observation_id, sid)).fetchone()
            latest = db.execute("SELECT id FROM observations WHERE session=? ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
        if not obs or obs["used"] or latest[0] != observation_id or time.time() - obs["created"] > 60:
            raise ValueError("inspect 需要未使用的最新观察。")
        data = json.loads(obs["data"])
        data["semantic"] = self.device().inspect(target, data)
        self._active(context, sid)
        self.check_direction(context, sid)
        with self.connect() as db:
            db.execute("UPDATE observations SET data=? WHERE id=? AND used=0", (json.dumps(data), observation_id))
        return data

    def act(self, context, sid, observation_id, action, *, key="", x=None, y=None, button="left", text="", delta=0, url="", element_id="", guard=None):
        def check_active():
            if guard is not None:
                guard()
            return self._active(context, sid)
        check_active()
        _, target = self._active(context, sid)
        from game_window import GameWindow
        extra = {"text": text, "delta": delta, "url": url} if action in ("type", "scroll", "navigate") else {}
        if action in ("invoke", "fill", "select"):
            extra = {"text": text, "element_id": element_id}
        GameWindow.validate_action(action, key=key, x=x, y=y, button=button, software=target["kind"] == "software", **extra)
        if action == "navigate":
            from browser_control import browser_executable
            if not browser_executable(target["executable"]):
                raise ValueError("navigate 仅用于受支持的浏览器页面。")
            from activity_recovery import recovery
            if url in json.loads(recovery(self, context, sid)["blocked_urls"]):
                raise ValueError("该页面已受内容拦截，不能再次打开；请选择其他入口。")
        identity = hashlib.sha256(json.dumps([sid, observation_id, action, key, x, y, button, extra]).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.check_direction(context, sid)
            previous = db.execute("SELECT state,data FROM actions WHERE id=?", (identity,)).fetchone()
            if previous:
                return {"state": previous["state"], "action_id": identity, "replayed": False, **json.loads(previous["data"])}
            obs = db.execute("SELECT * FROM observations WHERE id=? AND session=?", (observation_id, sid)).fetchone()
            current = db.execute("SELECT actions,state FROM sessions WHERE id=?", (sid,)).fetchone()
            if current["state"] != "running":
                raise ValueError("游玩会话已结束，未投递输入。")
            count = current["actions"]
            if not obs or obs["used"] or time.time() - obs["created"] > 60:
                raise ValueError("截图无效、已用于操作或已过期，请重新观察。")
            if self.max_actions > 0 and count >= self.max_actions:
                raise ValueError("本轮游玩操作额度已用完，请记录当前进度后结束；下次从存档或当前画面继续。")
            if db.execute("SELECT 1 FROM session_controls WHERE session=?", (sid,)).fetchone():
                raise ValueError("当前任务已申请完成或暂停，不能继续投递输入。")
            data = json.loads(obs["data"])
            if data.get("direction_revision", 0) != self.direction(context, sid)["revision"]:
                raise ActivityDirectionChanged("观察属于旧阅读方向，未投递输入。")
            if hashlib.sha256(Path(data["image_path"]).read_bytes()).hexdigest() != data["sha256"]:
                raise ValueError("观察截图已变更，请重新观察。")
            db.execute("UPDATE observations SET used=1 WHERE id=?", (observation_id,))
            db.execute("UPDATE sessions SET actions=actions+1 WHERE id=?", (sid,))
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?)", (identity, sid, "unknown", "{}", time.time()))
        # Persist uncertainty before touching a window. A cancelled/failed
        # command cannot be silently replayed into the game on restart.
        state = "posted"
        try:
            result = self.device().act(target, data, action, key=key, x=x, y=y, button=button,
                                       guard=check_active, **extra)
        except Exception as error:
            if getattr(error, "input_posted", None) is not False:
                raise
            # The backend explicitly confirmed it stopped before sending input.
            # Persist this separately from an uncertain transport failure.
            state = "rejected"
            result = {"ok": False, "posted": False, "input_posted": False,
                      "code": getattr(error, "code", "INPUT_REJECTED"), "error": str(error),
                      "hint": getattr(error, "hint", "重新截图并核对输入目标。")}
        with self.connect() as db:
            db.execute("UPDATE actions SET state=?,data=? WHERE id=?", (state, json.dumps(result), identity))
        return {"state": state, "action_id": identity, **result}

    def note(self, context, sid, text, evidence, kind="reflection"):
        row, target = self._active(context, sid)
        allowed = ("observation", "reflection", "preference") if target["kind"] == "software" else ("plot", "reflection", "preference")
        if kind not in allowed or not str(text).strip() or len(str(text)) > 3000:
            raise ValueError("学习记录需为 1–3000 字；软件用 observation/reflection/preference，游戏用 plot/reflection/preference。")
        if not isinstance(evidence, list) or not evidence or len(evidence) > 20:
            raise ValueError("学习记录必须关联实际观察的 observation_id。")
        with self.connect() as db:
            for identity in evidence:
                observation = db.execute("SELECT data FROM observations WHERE id=? AND session=?", (identity, sid)).fetchone()
                if not observation:
                    raise ValueError("学习依据不是本次会话的游戏画面。")
                if json.loads(observation[0]).get("direction_revision", 0) != self.direction(context, sid)["revision"]:
                    raise ActivityDirectionChanged("笔记引用的是旧方向的画面，请观察当前页面后再记录。")
            identity = uuid.uuid4().hex
            db.execute("INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?)", (identity, context["namespace"], context["scope"],
                row["target"], sid, str(text).strip(), kind, json.dumps(evidence), time.time()))
        from agent_core.activity import record_note
        record_note(context, target, identity, str(text).strip(), kind, evidence)
        return {"id": identity, "source": "software_observation" if target["kind"] == "software" else "fictional_game", "kind": kind}

    def notes(self, context, query="", limit=6):
        with self.connect() as db:
            rows = db.execute("SELECT notes.id,notes.text,notes.kind,notes.created,games.title,games.kind AS target_kind FROM notes JOIN games ON games.id=notes.target "
                "WHERE notes.namespace=? AND notes.scope=? AND games.owner=? AND games.persona=? "
                "AND NOT EXISTS (SELECT 1 FROM session_recoveries r WHERE r.session=notes.session "
                "AND r.blocked_urls<>'[]' AND notes.created<r.updated) "
                + ("AND notes.target=(SELECT target FROM sessions WHERE id=?) " if context.get("sandbox_session_id") else "")
                + "ORDER BY notes.created DESC LIMIT 200",
                (context["namespace"], context["scope"], context.get("sender_id", ""), context_persona(context))
                + ((context["sandbox_session_id"],) if context.get("sandbox_session_id") else ())).fetchall()
        words = re.findall(r"[\w]+", str(query).casefold())
        return [{**dict(row), "source": "software_observation" if row["target_kind"] == "software" else "fictional_game"} for row in rows
                if not words or any(word in (row["text"] + row["title"]).casefold() for word in words)][:min(20, max(1, int(limit)))]

    def evidence(self, context, sid):
        """Actual work, including observations captured after posted input."""
        session = self.session(context, sid)
        with self.connect() as db:
            notes = db.execute("SELECT COUNT(*) FROM notes WHERE session=?", (sid,)).fetchone()[0]
            actions = db.execute("SELECT COUNT(*) FROM actions WHERE session=? AND state='posted'", (sid,)).fetchone()[0]
            reobserved = bool(db.execute("SELECT 1 FROM observations o JOIN actions a "
                "ON json_extract(o.data,'$.after_action')=a.id WHERE o.session=? AND a.session=? AND a.state='posted' LIMIT 1",
                (sid, sid)).fetchone())
        return {"notes": notes, "actions": actions, "attempts": session["actions"], "reobserved": reobserved}

    def control(self, context, sid):
        self.session(context, sid)
        with self.connect() as db:
            row = db.execute("SELECT action,text FROM session_controls WHERE session=?", (sid,)).fetchone()
        return dict(row) if row else {}

    def request_end(self, context, sid, action, text):
        row, target = self._active(context, sid)
        if action not in ("finish", "pause") or not str(text).strip():
            raise ValueError("完成或暂停时请提供实际结果或具体阻碍。")
        if action == "finish":
            proof = self.evidence(context, sid)
            if not proof["notes"] or (target["kind"] != "software" or proof["actions"] > 0) and (
                    not proof["actions"] or not proof["reobserved"]):
                raise ValueError("完成需有截图依据的记录，已操作时还需操作后观察；阶段性进度请继续执行。")
        with self.connect() as db:
            # The first explicit decision wins; retries cannot change it.
            db.execute("INSERT OR IGNORE INTO session_controls VALUES(?,?,?)", (sid, action, str(text).strip()[:6000]))
            if action == "pause":
                db.execute("UPDATE games SET autoplay=0 WHERE id=?", (row["target"],))
        return {"target_id": row["target"], **self.control(context, sid)}

    def finish(self, context, sid, state, summary=""):
        self.session(context, sid)
        if state not in ("completed", "paused", "failed", "cancelled", "interrupted"):
            raise ValueError("无效游玩会话状态。")
        with self.connect() as db:
            changed = db.execute("UPDATE sessions SET state=?,summary=? WHERE id=?" +
                (" AND state='running'" if state in ("completed", "paused") else ""), (state, str(summary)[:6000], sid))
            if not changed.rowcount:
                raise ValueError("任务已结束或取消，不能覆盖停止状态。")

    def grant(self, context, path, *, writable=False):
        self.require_owner(context)
        if context.get("sandbox_session_id") or str(path).casefold() not in str(context.get("source_text", "")).casefold():
            raise PermissionError("额外路径必须由主人在当前消息明确提供。")
        target = Path(path).resolve(strict=True)
        if not target.is_dir() or target == Path(target.anchor):
            raise ValueError("请授权具体资料目录，不能授权整个磁盘。")
        if writable and not re.search(r"(?:允许|授权|可以).{0,8}(?:写入|修改)", str(context.get("source_text", ""))):
            raise PermissionError("写入资料目录需要主人明确说明允许写入或修改；否则以只读方式授权。")
        identity = hashlib.sha256((context["namespace"] + context["scope"] + str(target)).encode()).hexdigest()[:24]
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?,?)", (identity, context["namespace"], context["scope"],
                context["sender_id"], str(target), int(writable)))
        return {"id": identity, "path": str(target), "writable": bool(writable)}

    def files(self, context, sid, action, *, relative="", grant_id="", content=""):
        row = self.session(context, sid)
        root, writable = self.root / "sessions" / sid / "workspace", True
        root.mkdir(exist_ok=True)
        if grant_id:
            if grant_id.startswith("profile:"):
                profile = (context.get("sandbox_config") or {}).get("profiles", {}).get(grant_id[8:])
                grant = {"path": profile, "writable": False} if profile else None
            else:
                with self.connect() as db:
                    grant = db.execute("SELECT path,writable FROM grants WHERE id=? AND namespace=? AND scope=? AND owner=?",
                        (grant_id, context["namespace"], context["scope"], context["sender_id"])).fetchone()
            if not grant:
                raise PermissionError("资料目录未授权给当前主人及会话。")
            root, writable = Path(grant["path"]).resolve(), bool(grant["writable"])
        target = (root / relative).resolve()
        if not target.is_relative_to(root.resolve()):
            raise PermissionError("路径超出了已授权的资料目录。")
        if action == "list":
            return {"files": [{"name": item.name, "directory": item.is_dir()} for item in target.iterdir()][:200]}
        if action == "read":
            if target.stat().st_size > 1024 * 1024:
                raise ValueError("文本超过 1 MB，请分文件整理。")
            return {"text": target.read_text(encoding="utf-8-sig")}
        if action == "write":
            if not writable or row["state"] != "running":
                raise PermissionError("当前目录只读或任务已结束。")
            if target.exists():
                raise ValueError("此接口只创建新文件，不覆盖既有资料。")
            if len(str(content).encode()) > 1024 * 1024:
                raise ValueError("单份文本超过 1 MB。")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8", newline="") as stream:
                stream.write(str(content))
            return {"path": str(target)}
        raise ValueError("资料接口只支持 list/read/write，不执行脚本或删除文件。")
