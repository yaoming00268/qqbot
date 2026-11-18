"""Local pywebview bridge. No Qt dependency, shell execution or remote UI assets."""
from __future__ import annotations

from collections import deque
from contextlib import closing
from dataclasses import asdict
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
import uuid

from config import AppConfig, EmotionConfig, config_lock
from persona_manager import PersonaManager, PersonaPreset


def bridge(function):
    @functools.wraps(function)
    def invoke(*args, **kwargs):
        try:
            return {"ok": True, "data": function(*args, **kwargs)}
        except Exception as error:
            return {"ok": False, "error": str(error), "code": type(error).__name__}
    return invoke


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def revision(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


PREFERENCES = {"theme": "light", "surface_opacity": 78, "window_opacity": 100,
               "blur": 24, "accent": "blue", "font_size": 14, "native_glass": True,
               "reduced_motion": False}


def validated_preferences(value):
    result = {**PREFERENCES, **{key: val for key, val in value.items() if key in PREFERENCES}}
    if result["theme"] not in ("light", "dark", "system") or result["accent"] not in ("blue", "green", "violet", "slate"):
        raise ValueError("无效的主题或强调色。")
    for name, lower, upper in (("surface_opacity", 35, 100), ("window_opacity", 65, 100),
                               ("blur", 0, 48), ("font_size", 12, 17)):
        number = float(result[name])
        if not math.isfinite(number) or not lower <= number <= upper:
            raise ValueError(f"{name} 必须在 {lower} 到 {upper} 之间。")
        result[name] = int(number)
    for name in ("native_glass", "reduced_motion"):
        if type(result[name]) is not bool:
            raise ValueError("开关必须为布尔值。")
    return result


class StudioAPI:
    def __init__(self, home, config_path=None):
        self._home = Path(home).resolve()
        self._config_path = Path(config_path or self._home / "config.yaml").resolve()
        self._preferences_path = self._home / "data/studio/appearance.json"
        self._lock = threading.RLock()
        self._process = None
        self._state = "stopped"
        self._started_at = 0
        self._exit_code = None
        self._events = deque(maxlen=1600)
        self._sequence = 0
        self._task = None
        self._task_stop = threading.Event()
        self._window = None
        self._native = {"effect": "pending", "supported": False}
        self._allow_close = False
        self._restart = False
        self._closing = False
        self._maximized = False
        self._browser_link = None
        try:
            self._preferences = validated_preferences(json.loads(self._preferences_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            self._preferences = dict(PREFERENCES)

    def _config(self):
        return AppConfig.load(str(self._config_path))

    def _path(self, value):
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self._home / path).resolve()

    def _emit(self, kind, data):
        with self._lock:
            self._sequence += 1
            self._events.append({"seq": self._sequence, "kind": kind, "time": time.time(), "data": data})

    def _status(self):
        pid = self._process.pid if self._process and self._process.poll() is None else None
        from voice_live import applied_status
        return {"state": self._state, "pid": pid,
                "voice": applied_status(self._home, self._config().voice, pid),
                "started_at": self._started_at, "exit_code": self._exit_code,
                "task": self._task, "native": self._native, "closing": self._closing}

    def _personas(self):
        manager = PersonaManager(str(self._home / "personas.yaml"))
        return {"active": manager.active_persona_name,
                "items": [item.model_dump() for item in manager.presets.values()],
                "revision": revision(self._home / "personas.yaml")}

    @bridge
    def bootstrap(self):
        with self._lock:
            config = self._config()
            return {"config": config.model_dump(mode="json"), "schema": AppConfig.model_json_schema(),
                    "revision": revision(self._config_path), "personas": self._personas(),
                    "preferences": self._preferences, "status": self._status(),
                    "home": str(self._home), "version": "4.0", "engine": "pywebview · WebView2"}

    @bridge
    def poll(self, after=0):
        with self._lock:
            return {"events": [item for item in self._events if item["seq"] > int(after)],
                    "cursor": self._sequence, "status": self._status()}

    @bridge
    def save_config(self, changes, expected_revision):
        """Merge edited sections with the latest disk state; reject stale editors."""
        if not isinstance(changes, dict) or not changes:
            raise ValueError("没有需要保存的配置。")
        with self._lock, config_lock(self._config_path):
            config = AppConfig.update_fields(self._config_path, changes, expected_revision=expected_revision)
            return {"config": config.model_dump(mode="json"), "revision": revision(self._config_path)}

    @bridge
    def save_persona(self, original, values, expected_revision):
        with self._lock:
            if revision(self._home / "personas.yaml") != expected_revision:
                raise ValueError("角色预设已更新，请重新载入。")
            manager = PersonaManager(str(self._home / "personas.yaml"))
            if original and original not in manager.presets:
                raise ValueError("原角色已不存在。")
            if not original and values.get("name") in manager.presets:
                raise ValueError("角色名称已存在。")
            preset = PersonaPreset.model_validate(values)
            if not preset.name.strip() or not preset.prompt.strip():
                raise ValueError("请填写角色名称和角色预设。")
            manager.update_preset(original or preset.name, preset)
            if original and original != preset.name:
                config = self._config()
                for group in config.bot.group_settings.values():
                    if group.persona == original:
                        group.persona = preset.name
                config.save(str(self._config_path))
            return self._personas()

    @bridge
    def conversations(self, operation="list", params=None):
        from conversation_studio import catalog, update
        params = params or {}
        with self._lock, config_lock(self._config_path):
            config = self._config()
            personas = PersonaManager(str(self._home / "personas.yaml"))
            if operation in ("save", "remove"):
                if params.get("revision") != revision(self._config_path):
                    raise ValueError("配置已更新，请重新载入后保存对话设置。")
                value = update(config, personas, params, remove=operation == "remove")
                config = AppConfig.update_fields(self._config_path,
                    {"bot": {"conversations": {key: {source: rule.model_dump() for source, rule in mapping.items()}
                                                for key, mapping in value.items()}}},
                    expected_revision=params["revision"])
            elif operation != "list":
                raise ValueError("未知对话设置操作。")
            return {**catalog(self._home, config, personas), "revision": revision(self._config_path),
                    "settings": {key: {source: rule.model_dump() for source, rule in mapping.items()}
                                 for key, mapping in config.bot.conversations.items()}}

    @bridge
    def persona_action(self, action, name, expected_revision):
        with self._lock:
            if revision(self._home / "personas.yaml") != expected_revision:
                raise ValueError("角色预设已更新，请重新载入。")
            manager = PersonaManager(str(self._home / "personas.yaml"))
            if action == "activate":
                if not manager.set_active(name):
                    raise ValueError("角色不存在。")
            elif action == "delete":
                preset = manager.get_preset(name)
                if preset and any(rule.persona_id == preset.id or rule.history_persona_id == preset.id
                                  for mapping in self._config().bot.conversations.values() for rule in mapping.values()):
                    raise ValueError("该人设仍被对话设置或共享历史引用，请先在“对话与代理”中取消绑定。")
                if not manager.delete_preset(name):
                    raise ValueError("至少需要保留一个角色。")
            else:
                raise ValueError("未知角色操作。")
            return self._personas()

    def _consume_line(self, line):
        for prefix, kind in (("[FEED_MSG] ", "message"), ("[FEED_ACTION] ", "reply"),
                             ("[MEDIA_JOB] ", "job"), ("[MEDIA_ASSET] ", "asset"), ("[CONTROL_ACK] ", "control")):
            if line.startswith(prefix):
                try:
                    self._emit(kind, json.loads(line[len(prefix):]))
                    return
                except ValueError:
                    break
        # Remove decorative emoji from system logs, never from human messages.
        clean = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", "", line)
        self._emit("log", {"text": clean, "level": "error" if re.search(r"ERROR|Traceback|失败", clean) else
                              "warning" if re.search(r"WARNING|警告", clean) else "info"})

    def _read_process(self, process):
        try:
            for line in process.stdout:
                self._consume_line(line.rstrip("\r\n"))
            code = process.wait()
        except Exception as error:
            self._emit("log", {"text": str(error), "level": "error"})
            code = process.poll()
        finally:
            if process.stdout:
                process.stdout.close()
        with self._lock:
            if self._process is not process:
                return
            self._state, self._exit_code = "stopped", code
            restart, self._restart = self._restart, False
            self._emit("log", {"text": f"核心已停止，退出代码 {code}", "level": "info"})
        if restart and not self._closing:
            self.control("start")
        if self._closing:
            self._finish_close()

    def _stop(self):
        if self._process and self._process.poll() is None:
            path = self._home / "data/runtime_control.json"
            atomic_json(path, {"id": uuid.uuid4().hex, "action": "stop", "time": time.time()})
            self._state = "stopping"
            self._emit("log", {"text": "已请求停止，正在等待核心保存记忆并退出。", "level": "info"})

    @bridge
    def control(self, action):
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            if action == "start":
                if running:
                    return self._status()
                self._config()  # Validate before launching; never overwrite the editor.
                from runtime_paths import module_command
                command = module_command("core")
                self._process = subprocess.Popen(command, cwd=self._home, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                    env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), bufsize=1)
                self._state, self._started_at, self._exit_code = "running", time.time(), None
                threading.Thread(target=self._read_process, args=(self._process,), daemon=True).start()
                self._emit("log", {"text": "核心进程已启动。", "level": "info"})
            elif action == "stop":
                self._restart = False
                self._stop()
            elif action == "restart":
                if running:
                    self._restart = True
                    self._stop()
                else:
                    return self.control.__wrapped__(self, "start")
            elif action == "force_stop":
                self._restart = False
                if running:
                    self._process.kill()
            else:
                raise ValueError("未知运行操作。")
            return self._status()

    @bridge
    def set_master(self, user_id, nickname="", title="主人", group_id="", global_scope=False, remove=False):
        from master.manager import MasterManager
        with self._lock:
            manager = MasterManager(self._config(), str(self._config_path))
            if remove:
                manager.remove_master(user_id=user_id, group_id=group_id, is_global=bool(global_scope))
            else:
                if not str(user_id).strip():
                    raise ValueError("需要真实的平台用户 ID。")
                manager.set_master(user_id=user_id, nickname=nickname, title=title, group_id=group_id,
                                   is_global=bool(global_scope))
            return {"config": self._config().model_dump(mode="json"), "revision": revision(self._config_path)}

    def _read_table(self, path, table, order="rowid", limit=100):
        if not path.is_file():
            return []
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                return []
            return [dict(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY "{order}" DESC LIMIT ?', (limit,))]

    @bridge
    def records(self, category="tasks", query="", limit=100):
        config = self._config()
        root = self._path(config.bot.runtime_database_path).parent
        errors = []
        def read(path, table, order="rowid"):
            try:
                rows = self._read_table(path, table, order, max(1, min(500, int(limit))))
                return [row for row in rows if not query or str(query).casefold() in json.dumps(row, ensure_ascii=False).casefold()]
            except (sqlite3.Error, OSError) as error:
                errors.append(str(error))
                return []
        if category == "tasks":
            return {"work": read(self._path(config.bot.runtime_database_path), "work"),
                    "media": read(self._path(config.media.database_path), "jobs"),
                    "actors": read(self._path(config.agent.database_path or str(root / "actors.sqlite3")), "actor_actions"), "errors": errors}
        if category == "memory":
            path = root / "main_chat/identity.sqlite3"
            return {"events": read(path, "memory_events", "updated"), "contacts": read(path, "contacts", "updated"),
                    "growth": read(path, "presets", "updated"), "archives": read(path, "identity_archives", "updated"), "errors": errors}
        if category == "stickers":
            return {"items": read(self._path(config.stickers.directory) / "library.sqlite3", "stickers", "created"), "errors": errors}
        if category == "autonomy":
            path = self._path(config.sandbox.directory) / "sandbox.sqlite3"
            titles = {row["id"]: row["title"] for row in read(path, "games")}
            return {"sessions": [{**row, "title": titles.get(row["target"], row["target"])} for row in read(path, "sessions", "created")],
                    "notes": [{**row, "title": titles.get(row["target"], row["target"])} for row in read(path, "notes", "created")], "errors": errors}
        raise ValueError("未知资料类型。")

    @bridge
    def recover_activity(self, session_id, operation, url=""):
        if operation not in ("skip", "restart"):
            raise ValueError("未知活动恢复操作。")
        config = self._config()
        sessions = self._read_table(self._path(config.sandbox.directory) / "sandbox.sqlite3", "sessions", "created", 500)
        session = next((row for row in sessions if row["id"] == session_id), None)
        if not session:
            raise ValueError("活动记录不存在，请刷新后重试。")
        from console_tools.broker import ConsoleBroker
        from console_tools.cli import execute
        path = self._path(config.bot.runtime_database_path).parent / "main_chat/console.sqlite3"
        if not path.is_file():
            raise ValueError("当前运行目录没有对应的控制台任务记录。")
        broker = ConsoleBroker(path)
        request = broker.record(session["request_id"])
        if not request:
            raise ValueError("原任务记录不存在，不能重放操作。")
        original = broker.context(request["token"], allow_expired=True)
        context = {**original, "sandbox_session_id": session["id"], "parent_request_id": request["id"],
                   "activity_controller": True, "activity_target_id": session["target"], "source_text": "",
                   "sandbox_config": {**config.sandbox.model_dump(), "directory": str(self._path(config.sandbox.directory))},
                   "artifact_roots": [], "output_dir": ""}
        context_file = broker.issue_context(context, path.parent / "gui_controls")
        return execute("sandbox", context_file, operation=operation, params={"url": str(url).strip()})

    @bridge
    def browser_tools(self, operation, values=None):
        from config import GeminiWebConfig
        config = GeminiWebConfig.model_validate({**self._config().media.gemini_web.model_dump(), **(values or {})})
        config.cookie_file = str(self._path(config.cookie_file))
        config.page_profile_dir = str(self._path(config.page_profile_dir))
        if operation == "connect" and config.transport == "browser":
            from gemini_page_browser import launch_page_login
            launch_page_login(config)
            return {"opened": True}
        if operation == "connect":
            import webbrowser
            from gemini_browser_link import GeminiBrowserLink
            with self._lock:
                if self._browser_link and self._browser_link.config.cookie_file != config.cookie_file:
                    self._browser_link.close()
                    self._browser_link = None
                if not self._browser_link:
                    self._browser_link = GeminiBrowserLink(config, lambda source: self._emit("browser_connected", {"source": source}))
                url = self._browser_link.start()
            if not webbrowser.open(url):
                raise ValueError("系统默认浏览器未能打开，请检查默认浏览器设置。")
            return {"opened": True}
        if operation == "inspect":
            import asyncio
            from gemini_web_client import inspect_account, public_error
            from media_downloader import MediaError
            def inspect(stop, progress):
                try:
                    if config.transport == "browser":
                        from gemini_page_browser import inspect_page_account
                        return asyncio.run(asyncio.wait_for(inspect_page_account(config), timeout=min(config.timeout_seconds, 60)))
                    return asyncio.run(inspect_account(config))
                except Exception as error:
                    raise ValueError(str(error) if isinstance(error, MediaError) else public_error(error)) from None
            return self._run_task("gemini", inspect)
        if operation == "extension":
            from gemini_browser_link import EXTENSION_DIRECTORY
            os.startfile(str(EXTENSION_DIRECTORY))
            return {"opened": True}
        if operation == "bert_studio":
            path = self._home / "emotion_studio/run.bat"
            if not path.is_file():
                raise ValueError("数据目录中未找到 emotion_studio/run.bat。请先安装独立 BERT 工作台，或填写已有推理服务描述路径。")
            os.startfile(str(path))
            return {"opened": True}
        raise ValueError("未知浏览器操作。")

    def _run_task(self, kind, operation):
        with self._lock:
            if self._task and self._task["state"] == "running":
                raise ValueError("请等待当前模型任务结束，或先停止它。")
            identity = uuid.uuid4().hex[:12]
            stop = self._task_stop = threading.Event()
            self._task = {"id": identity, "kind": kind, "state": "running", "progress": {}, "started": time.time()}
        def progress(event):
            with self._lock:
                self._task["progress"] = event
                self._emit("model_progress", {"id": identity, **event})
        def work():
            from emotion_training import TrainingCancelled
            try:
                result = operation(stop, progress)
                state = "cancelled" if stop.is_set() else "completed"
                with self._lock:
                    self._task.update(state=state, result=result)
                self._emit("model_result", {"id": identity, "kind": kind, "state": state, "result": result})
            except Exception as error:
                state = "cancelled" if isinstance(error, TrainingCancelled) else "failed"
                with self._lock:
                    self._task.update(state=state, error=str(error))
                self._emit("model_result", {"id": identity, "kind": kind, "state": state, "error": str(error)})
            if self._closing:
                self._finish_close()
        threading.Thread(target=work, daemon=True).start()
        return {"id": identity, "state": "running"}

    @bridge
    def voice(self, operation, params=None):
        import asyncio
        from voice_config import VoiceConfig
        from voice_service import VoiceService, load_catalog, voice_context
        params = params or {}
        if operation == "apply":
            changes = params.get("changes", {})
            from voice_config import VoiceConfig
            if not isinstance(changes, dict) or not changes or set(changes) - set(VoiceConfig.model_fields):
                raise ValueError("请选择有效的语音配置字段。")
            result = self.save_config({"voice": changes}, params.get("revision", ""))
            if not result["ok"]:
                raise ValueError(result["error"])
            return {**result["data"], "applied": self._status()["voice"]}
        config = VoiceConfig.model_validate({**self._config().voice.model_dump(), **params.get("config", {})})
        for key in ("catalog_path", "cache_dir", "engine_python", "engine_directory", "engine_config"):
            setattr(config, key, str(self._path(getattr(config, key))))
        config.voicestudio.source_directory = str(self._path(config.voicestudio.source_directory))
        if operation == "catalog":
            from voice_emotions import presets
            path = Path(config.catalog_path)
            return {"catalog": load_catalog(config) if path.is_file() else {"version": 1, "characters": {}},
                    "revision": revision(path), "capability": voice_context(config), "presets": presets()}
        if operation == "save_catalog":
            from voice_emotions import presets
            path = Path(config.catalog_path)
            with self._lock:
                if params.get("revision", "") != revision(path):
                    raise ValueError("样本目录已更新，请重新载入后编辑。")
                value = params.get("catalog", {})
                if value.get("version") != 1 or not isinstance(value.get("characters"), dict):
                    raise ValueError("样本目录需要 version: 1 和 characters。")
                for key, character in value["characters"].items():
                    if not isinstance(character, dict) or not isinstance(character.get("emotions"), dict):
                        raise ValueError("每个角色需要 emotions 情绪参考映射。")
                    for label, reference in character["emotions"].items():
                        if not label or not isinstance(reference, dict):
                            raise ValueError("情绪标签不能为空，参考切片需要对象。")
                atomic_json(path, value)
            return {"catalog": value, "revision": revision(path), "capability": voice_context(config), "presets": presets()}
        if operation == "prepare":
            from voice_text import prepare_chunks
            return {"chunks": [item.to_dict() for item in prepare_chunks(params.get("text", ""), config, params.get("emotion", ""))]}
        if operation == "cancel":
            self._task_stop.set()
            return {"state": "cancelling"}
        if operation == "engine_status":
            from voice_engine import engine_status
            return engine_status()
        if operation == "engine_stop":
            from voice_engine import stop_engine, stop_studio
            return self._run_task("voice-engine", lambda stop, progress: {"studio": stop_studio(), "engine": stop_engine()})
        if operation == "engine_start":
            from voice_engine import ready
            def start(stop, progress):
                def current():
                    if stop.is_set():
                        from emotion_training import TrainingCancelled
                        raise TrainingCancelled()
                return asyncio.run(ready(config, check_current=current))
            return self._run_task("voice-engine", start)
        if operation == "check":
            return self._run_task("voice-check", lambda stop, progress: asyncio.run(VoiceService(config).check()))
        if operation == "preview":
            import base64
            config.enabled = True
            config.max_duration_seconds = min(30, config.max_duration_seconds)
            def preview(stop, progress):
                from emotion_training import TrainingCancelled
                async def work():
                    def stream(event):
                        if event.get("type") == "audio":
                            self._emit("voice_audio", {**event, "task_id": self._task["id"]})
                        else:
                            progress(event)
                    task = asyncio.create_task(VoiceService(config).synthesize(params.get("text", ""),
                        params.get("emotion", ""), spoken_text=params.get("spoken_text", ""), progress=stream))
                    try:
                        while not task.done():
                            if stop.is_set():
                                raise TrainingCancelled()
                            await asyncio.wait({task}, timeout=0.1)
                        result = task.result()
                        result["wav_base64"] = base64.b64encode(Path(result["wav_path"]).read_bytes()).decode("ascii")
                        return result
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                return asyncio.run(work())
            return self._run_task("voice-preview", preview)
        raise ValueError("未知语音工作室操作。")

    @bridge
    def voice_training(self, operation, params=None):
        from voice_training import VoiceTraining
        params = params or {}
        from voice_config import VoiceConfig
        voice = VoiceConfig.model_validate({**self._config().voice.model_dump(), **params.get("config", {})})
        manager = VoiceTraining(voice, self._home)
        if operation == "status":
            return manager.status()
        if operation == "dataset":
            return manager.dataset(params.get("dataset_id", ""))
        if operation == "save_annotations":
            from voice_dataset import update_annotations
            with self._lock:
                return update_annotations(manager.root, params.get("dataset_id", ""),
                    params.get("edits", []), params.get("revision", ""))
        if operation == "use_reference":
            with self._lock:
                return manager.use_reference(params.get("dataset_id", ""), params.get("sample_id", ""),
                    params.get("character_id", ""), dataset_revision=params.get("dataset_revision", ""),
                    catalog_revision=params.get("catalog_revision", ""), replace=params.get("replace") is True)
        if operation == "audio":
            import base64
            data = manager.dataset(params.get("dataset_id", ""))
            row = next((r for r in data["rows"] if r["id"] == params.get("sample_id")), None)
            if row is None:
                raise ValueError("训练切片不存在。")
            path = manager.root / "datasets" / data["id"] / row["audio"]
            return {"wav_base64": base64.b64encode(path.read_bytes()).decode("ascii")}
        if operation == "cancel":
            self._task_stop.set()
            return {"state": "cancelling"}
        if operation == "log":
            return manager.log(params.get("run_id", ""))
        if operation == "publish":
            with self._lock:
                return manager.publish(params.get("run_id", ""), params.get("character_id", ""),
                    expected_revision=params.get("revision", ""))
        if operation == "setup":
            from voice_training_setup import setup_training
            return self._run_task("voice-training-setup", lambda stop, progress: setup_training(self._home, voice, progress=progress))
        if operation == "assets":
            return self._run_task("voice-training-assets", lambda stop, progress: manager.worker("assets",
                {"version": manager.options.version}, stop=stop, progress=progress))
        if operation == "import":
            return self._run_task("voice-training-import", lambda stop, progress: manager.import_annotations(
                params.get("path", ""), speaker=params.get("speaker", ""), name=params.get("name", ""), stop=stop, progress=progress))
        if operation == "prepare_audio":
            paths = params.get("paths", [])
            if not isinstance(paths, list) or not paths or len(paths) > 1000:
                raise ValueError("请选择 1–1000 个本地音频文件。")
            sources = [{"audio": str(self._path(p)), "source": str(self._path(p))} for p in paths]
            return self._run_task("voice-training-import", lambda stop, progress: manager.prepare_audio(sources,
                speaker=params.get("speaker", ""), language=params.get("language", "ja"), name=params.get("name", ""), stop=stop, progress=progress))
        if operation == "chieri":
            def acquire(stop, progress):
                result = manager.acquire_chieri(stop=stop, progress=progress)
                return manager.prepare_audio(result["sources"], speaker="chieri", name="雪村千绘莉 · 官方普通试听样本",
                    stop=stop, progress=progress, source="https://www.cabbage-soft.com/products/amachoco/",
                    license_text=result["license"], attribution=result["attribution"])
            return self._run_task("voice-training-import", acquire)
        if operation == "train":
            return self._run_task("voice-training-run", lambda stop, progress: manager.run(params.get("dataset_id", ""),
                allow_unreviewed=params.get("allow_unreviewed") is True, resume=params.get("resume", ""), stop=stop, progress=progress))
        if operation == "validate":
            return self._run_task("voice-training-validate", lambda stop, progress: manager.validate(params.get("run_id", ""), stop=stop, progress=progress))
        if operation == "validation_audio":
            import base64
            folder, manifest, _, _ = manager.character(params.get("run_id", ""))
            samples = manifest.get("validation", {}).get("samples", [])
            index = int(params.get("index", 0))
            if not 0 <= index < len(samples):
                raise ValueError("请先执行验证试听。")
            path = Path(samples[index]["wav_path"]).resolve()
            if not path.is_relative_to((folder / "validation-audio").resolve()):
                raise ValueError("验证音频路径无效。")
            return {"wav_base64": base64.b64encode(path.read_bytes()).decode("ascii")}
        raise ValueError("未知语音训练操作。")

    @bridge
    def singing(self, operation, params=None):
        import asyncio
        import base64
        from singing_service import SingingService, singing_context
        from voice_config import VoiceConfig
        from emotion_training import TrainingCancelled
        params = params or {}
        config = VoiceConfig.model_validate({**self._config().voice.model_dump(), **params.get("config", {})})
        for key in ("catalog_path", "cache_dir", "engine_python", "engine_directory", "engine_config"):
            setattr(config, key, str(self._path(getattr(config, key))))
        for key in ("cache_dir", "engine_python", "engine_directory", "generation_python", "generation_directory"):
            setattr(config.singing, key, str(self._path(getattr(config.singing, key))))
        if operation == "status":
            return singing_context(config)
        if operation == "cancel":
            self._task_stop.set()
            return {"state": "cancelling"}
        if operation == "load_lyrics":
            path = self._path(params.get("path", ""))
            if not path.is_file() or path.stat().st_size > 128 * 1024:
                raise ValueError("请选择小于 128KB 的歌词文本。")
            raw = path.read_bytes()
            text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
            if len(text) > config.singing.max_lyrics_chars:
                raise ValueError("歌词超过唱歌字数上限。")
            return {"text": text}
        if operation == "open_result":
            path = self._path(params.get("directory", ""))
            root = Path(config.singing.cache_dir).resolve()
            if not path.is_relative_to(root) or not (path / "manifest.json").is_file():
                raise ValueError("请选择唱歌工作室生成的成果目录。")
            os.startfile(str(path))
            return {"opened": True}
        if operation == "check":
            return self._run_task("singing-check", lambda stop, progress: asyncio.run(SingingService(config).check()))
        if operation == "render":
            def render(stop, progress):
                def current():
                    if stop.is_set():
                        raise TrainingCancelled()
                result = asyncio.run(SingingService(config).render(params.get("request", {}), progress=progress,
                                                                  check_current=current, allow_disabled=True))
                result["mp3_base64"] = base64.b64encode(Path(result["mp3_path"]).read_bytes()).decode("ascii")
                return result
            return self._run_task("singing-render", render)
        raise ValueError("未知唱歌操作。")

    @bridge
    def broadcast(self, operation, params=None):
        """Local operations UI; secrets stay in private files and transport code."""
        from broadcast_host import execute
        params = params or {}
        if operation in {"login_open", "login_status", "browser_open", "livehime_status", "livehime_open", "livehime_prepare"}:
            from bilibili_desktop import open_browser, login_status, livehime_status, open_livehime, prepare_livehime
            config = self._config()
            if operation == "login_status":
                return login_status(self._home, config.bilibili.credentials_file)
            if operation in {"login_open", "browser_open"}:
                if params.get("reset") and any(os.environ.get("BILIBILI_" + name) for name in ("SESSDATA", "BILI_JCT")):
                    raise ValueError("当前使用环境变量登录态，请先清除环境变量再切换浏览器账号。")
                return open_browser(self._home, url=params.get("url"), reset=bool(params.get("reset", False)))
            if operation == "livehime_status":
                return livehime_status(config.bilibili.livehime)
            if operation == "livehime_open":
                return open_livehime(config.bilibili.livehime)
            return prepare_livehime(self._home, config)
        if operation.startswith("voice_"):
            import asyncio
            from broadcast_tts import broadcast_voice_context
            from voice_service import VoiceService, load_catalog
            from voice_engine import ready, engine_status, studio_status, stop_engine, stop_studio
            voice = self._config().broadcast_voice.model_copy(deep=True)
            for key in ("catalog_path", "cache_dir", "engine_python", "engine_directory", "engine_config"):
                setattr(voice, key, str(self._path(getattr(voice, key))))
            voice.voicestudio.source_directory = str(self._path(voice.voicestudio.source_directory))
            if operation == "voice_catalog":
                from voice_emotions import presets
                return {"catalog": load_catalog(voice), "capability": broadcast_voice_context(voice), "presets": presets()}
            if operation == "voice_check":
                async def check():
                    result = await VoiceService(voice).check()
                    return {"reachable": result["reachable"], **broadcast_voice_context(voice)}
                return self._run_task("broadcast-voice-check", lambda stop, progress: asyncio.run(check()))
            if operation == "voice_engine_status":
                return {"engine": engine_status(voice), "studio": studio_status(voice)}
            if operation == "voice_engine_start":
                return self._run_task("broadcast-voice-engine", lambda stop, progress: asyncio.run(ready(voice)))
            if operation == "voice_engine_stop":
                return self._run_task("broadcast-voice-engine", lambda stop, progress: {"studio": stop_studio(voice), "engine": stop_engine(voice)})
            raise ValueError("未知直播语音操作。")
        if operation == "preview":
            from vtuber_host import overlay_url
            return {"url": overlay_url(self._config().vtuber, preview=True)}
        if operation in {"bind_actor", "bind_account_actor", "account_preset"}:
            from agent_core.configuration import ActorConfig
            with self._lock, config_lock(self._config_path):
                current_revision = revision(self._config_path)
                if (operation == "account_preset" or "revision" in params) and params.get("revision") != current_revision:
                    raise ValueError("配置已被其他窗口或机器人更新。请重新载入后再保存。")
                config = self._config()
                mode = params.get("mode")
                if operation == "account_preset" and mode not in {"browse", "operate", "pause"}:
                    raise ValueError("请选择浏览与草稿、自主运营或暂停。")
                actor_id = config.bilibili.autonomy.actor_id
                if operation == "account_preset" and mode == "pause":
                    changes = {"bilibili": {"autonomy": {**config.bilibili.autonomy.model_dump(), "enabled": False}}}
                else:
                    manager = PersonaManager(str(self._home / "personas.yaml"))
                    persona = manager.presets.get(manager.active_persona_name)
                    if persona is None:
                        raise ValueError("请先选择一个角色预设。")
                    actor_id = config.agent.bindings.get("persona:" + persona.id, "")
                    if not actor_id or config.agent.actors[actor_id].persona_id != persona.id:
                        actor_id = next((key for key, actor in config.agent.actors.items() if actor.persona_id == persona.id), "")
                    if not actor_id:
                        actor_id = "broadcast_" + re.sub(r"[^a-zA-Z0-9_-]", "", persona.id)[:48]
                        if actor_id in config.agent.actors:
                            actor_id += "_" + uuid.uuid4().hex[:8]
                    existing = config.agent.actors.get(actor_id)
                    config.agent.actors[actor_id] = (existing.model_copy(update={"enabled": True}) if existing else
                        ActorConfig(name=persona.name, persona_id=persona.id, enabled=True))
                    config.agent.bindings["persona:" + persona.id] = actor_id
                    agent = {"actors": config.agent.actors, "bindings": config.agent.bindings, "enabled": True}
                    if operation == "bind_actor":
                        bili = {"actor_id": actor_id}
                    else:
                        autonomy = {**config.bilibili.autonomy.model_dump(), "actor_id": actor_id}
                        bili = {"autonomy": autonomy}
                        if operation == "account_preset":
                            autonomy.update(enabled=True, allow_comments=mode == "operate", allow_likes=mode == "operate",
                                            allow_dynamics=mode == "operate", allow_video_uploads=False, allow_profile_update=False)
                            bili["enabled"] = True
                            if mode == "operate":
                                bili["allow_publish"] = True
                            agent["autonomous_model_enabled"] = True
                    changes = {"agent": agent, "bilibili": bili}
                config = AppConfig.update_fields(self._config_path, changes, expected_revision=current_revision)
                return {"config": config.model_dump(mode="json"), "revision": revision(self._config_path), "actor_id": actor_id}
        if operation == "import_media":
            import shutil
            path = self._path(params.get("path", ""))
            if not path.is_file() or path.suffix.lower() not in {".mp4", ".mkv", ".mov", ".flv", ".webm", ".png", ".jpg", ".jpeg", ".webp", ".wav", ".mp3", ".flac", ".ogg", ".aac", ".m4a"}:
                raise ValueError("请选择视频、封面或音频文件。")
            with path.open("rb") as stream:
                signature = hashlib.file_digest(stream, "sha256").hexdigest()
            directory = self._home / "data/broadcast/media"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / (signature[:20] + path.suffix.lower())
            if not target.exists():
                temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    shutil.copyfile(path, temporary)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            return {"path": str(target), "sha256": signature, "name": path.name}
        if operation == "avatar":
            for old, new in (("avatar_emotion", "emotion"), ("motion_group", "group"), ("motion_index", "index")):
                if old in params:
                    params[new] = params.pop(old)
        return execute(self._config_path, operation, params)

    @bridge
    def dsh(self, operation, params=None):
        import asyncio
        from dsh_config import DSHConfig
        params = params or {}
        config = DSHConfig.model_validate({**self._config().deepseek_harness.model_dump(), **params.get("config", {})})
        config.home, config.workspace = str(self._path(config.home)), str(self._path(config.workspace))
        if operation == "export_mcp":
            from dsh_profile import export_mcp_config
            return {"path": str(export_mcp_config(config.home)), "sdk": "0.2.2rc2"}
        if operation == "check":
            from dsh_client import DeepSeekHarnessClient
            def check(stop, progress):
                client = DeepSeekHarnessClient(config, sessions_file=str(self._home / "data/studio/dsh-check-sessions.json"))
                ok, message = asyncio.run(client.probe(config.initialize_timeout_seconds))
                if not ok:
                    raise ValueError(message)
                return {"ok": ok, "message": message, "provider": config.provider, "model": config.model,
                        "api_key_present": bool(os.environ.get(config.api_key_env)), "home": config.home}
            return self._run_task("dsh-check", check)
        raise ValueError("未知 DSH 操作。")

    @bridge
    def emotion(self, operation, params=None):
        params = params or {}
        config = EmotionConfig.model_validate({**self._config().emotion.model_dump(), **params.get("config", {})})
        config.classifier_path = str(self._path(config.classifier_path))
        config.bert_service_file = str(self._path(config.bert_service_file))
        if operation == "cancel":
            self._task_stop.set()
            return {"state": "cancelling"}
        if operation == "models":
            from emotion_models import list_lms_models
            return self._run_task("models", lambda stop, progress: list_lms_models(config.lms_host))
        if operation == "analyze":
            from emotion_models import analyze_sentences, split_sentences
            def analyze(stop, progress):
                from emotion_training import TrainingCancelled
                def check():
                    if stop.is_set():
                        raise TrainingCancelled()
                results = analyze_sentences(config, split_sentences(str(params.get("text", ""))), check=check,
                    progress=lambda done, total: progress({"type": "analysis", "done": done, "total": total}))
                return [{**asdict(item), "confidence": item.confidence} for item in results]
            return self._run_task("analysis", analyze)
        if operation == "train":
            from emotion_training import train_classifier
            rows = params.get("rows", [])
            return self._run_task("training", lambda stop, progress: train_classifier(config, rows,
                output_path=self._path(config.classifier_path),
                warm_start=self._path(config.classifier_path) if params.get("warm_start") else None,
                stop=stop, on_progress=progress))
        if operation == "load_model":
            from emotion_models import LocalEmotionClassifier
            model = LocalEmotionClassifier.load(self._path(config.classifier_path), host=config.lms_host)
            return {"labels": model.labels, "features": model.encoder.metadata(), "training": model.training}
        if operation == "load_dataset":
            from emotion_training import read_dataset
            return read_dataset(self._path(params.get("path") or config.dataset_path), config.labels)
        if operation == "save_dataset":
            from emotion_training import write_dataset
            rows = params.get("rows", [])
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not str(row.get("text", "")).strip()
                or row.get("label", "") not in ["", *config.labels] for row in rows):
                raise ValueError("请检查样本文本及情感标签。")
            path = self._path(params.get("path") or config.dataset_path)
            write_dataset(path, rows)
            return {"path": str(path), "count": len(rows)}
        raise ValueError("未知情感工作室操作。")

    @bridge
    def glossary(self, operation="read", text="", expected_revision=""):
        from slang_glossary import SlangGlossary
        path = self._home / "data/slang_glossary.md"
        glossary = SlangGlossary(path)
        with self._lock:
            if operation == "save":
                with glossary._locked():
                    if revision(path) != expected_revision:
                        raise ValueError("话术表已由机器人更新，请重新载入。")
                    glossary._parse(text)  # Validate before atomically replacing it.
                    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                    try:
                        temporary.write_text(text, encoding="utf-8")
                        os.replace(temporary, path)
                    finally:
                        temporary.unlink(missing_ok=True)
            elif operation != "read":
                raise ValueError("未知话术表操作。")
            return {"text": glossary._read(), "revision": revision(path)}

    @bridge
    def stickers(self, operation, params=None):
        from sticker_library import StickerLibrary
        params = params or {}
        library = StickerLibrary(self._path(self._config().stickers.directory))
        if operation == "import":
            return {"id": library.import_file(self._path(params["path"]), params.get("tags", []), params.get("description", ""))}
        if operation == "remove":
            library.remove(params["id"])
            return {"removed": params["id"]}
        raise ValueError("未知素材操作。")

    @bridge
    def export_file(self, path, kind, content):
        target = self._path(path)
        if kind == "json":
            atomic_json(target, content)
        elif kind == "logs":
            if not isinstance(content, str):
                raise ValueError("日志必须为文字。")
            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(content, encoding="utf-8")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            raise ValueError("未知导出格式。")
        return {"path": str(target)}

    @bridge
    def choose_file(self, mode="open", file_types=None):
        if self._window is None:
            raise ValueError("文件选择器尚未就绪。")
        import webview
        kinds = {"open": webview.FileDialog.OPEN, "save": webview.FileDialog.SAVE, "folder": webview.FileDialog.FOLDER}
        if mode not in kinds:
            raise ValueError("未知文件选择方式。")
        paths = self._window.create_file_dialog(kinds[mode], directory=str(self._home),
                    file_types=tuple(file_types or ["All files (*.*)"]))
        return str(paths[0]) if paths else ""

    @bridge
    def preferences(self, values, persist=True):
        with self._lock:
            result = validated_preferences({**self._preferences, **values})
            if self._window:
                from ui.window_effects import apply_effects
                self._native = apply_effects(self._window, result)
            if persist:
                atomic_json(self._preferences_path, result)
                self._preferences = result
            return {"preferences": result, "native": self._native}

    def _finish_close(self):
        with self._lock:
            if self._process and self._process.poll() is None or self._task and self._task["state"] == "running":
                return
            self._allow_close = True
        if self._browser_link:
            self._browser_link.close()
            self._browser_link = None
        from broadcast_host import stop_server as stop_broadcast
        from vtuber_host import stop_server as stop_avatar
        stop_broadcast(self._config_path)
        stop_avatar(self._config().vtuber)
        if self._window:
            self._window.destroy()

    @bridge
    def window_action(self, action):
        if not self._window:
            return None
        if action == "minimize":
            self._window.minimize()
        elif action == "maximize":
            if self._maximized:
                self._maximized = False
                self._window.restore()
            else:
                self._maximized = True
                self._window.maximize()
        elif action == "close":
            self._closing = True
            self._restart = False
            self._task_stop.set()
            self._stop()
            threading.Thread(target=self._finish_close, daemon=True).start()
        else:
            raise ValueError("未知窗口操作。")
        return self._status()
