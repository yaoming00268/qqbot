"""Persistent main conversations with incremental input and console handoffs."""
from __future__ import annotations

from runtime_control import finish_thread

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import uuid

from antigravity_client import AgentServiceError, ContentBlockedError, SessionInvalidatedError
from console_tools.broker import ConsoleBroker, TASK_TYPES
from console_tools.contracts import invocation
from prompt_builder import compose_chat_system_prompt
from runtime_control import generation_is_current, session_generation
from chat_turns import plain_reply, reply_json
from runtime_paths import resource_path

logger = logging.getLogger("MainChat")
TOOLS_DIR = resource_path("console_tools")
SELF_ACTIVITY_WAIT_LIMIT = 3

MAIN_RULES = "\n\n".join((TOOLS_DIR / name).read_text(encoding="utf-8") for name in ("MAIN.md", "API.md"))


def compose_main_rules(system):
    return ("# QQBot 主会话总预设\n\n" + system + "\n\n" + MAIN_RULES +
        "\n通过常驻 qqbot_cli 调用：action 为命令名，operation 为可选子操作，params 为参数对象。"
        f"\n用法文档：{(TOOLS_DIR / 'README.md').as_posix()}")


class LiveUnavailable(RuntimeError):
    pass


class MainChat:
    def __init__(self, client):
        self.client = client
        self.root = Path(client.app_config.bot.runtime_database_path).resolve().parent / "main_chat"
        self.namespace = client._image_scope("placeholder").rsplit(":", 2)[0]
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        from companion_life import repair_pending_browse
        repair_pending_browse(self.broker, self.namespace)
        self.locks = {}
        from reply_queue import ReplyQueue
        self.replies = ReplyQueue()
        self.contexts = {}
        self.contexts_by_scope = {}
        self.requests = {}
        self.request_contexts = {}
        self.worker = None
        self.closed = False
        self.recalling = set()
        self.recall_tasks = {}
        self.startup_tasks = {}
        self.result_pending = set()
        self.delivery_tasks = {}
        self.retry_after = {}
        self.sandbox_schedule_after = 0
        self.activity_review_tasks = {}
        self.activity_review_due = {}
        self.self_activity_tasks = {}
        self.live_task = None
        self.live_schedule_task = None
        self.live_output_task = None
        self.live_audio = None
        self.live_idle_check_after = 0
        self.live_heartbeat_after = 0
        from live_events import recover as recover_live
        recover_live(self.broker, self.namespace)
        self.self_activity_was_enabled = False
        self.activity_review_scan_lock = asyncio.Lock()
        self.identity_tasks = {}
        self.identity_attempted = {}
        self.identity_scan_after = 0
        from bot_identity import store_for
        self.identity = store_for(client)
        with self.broker.connect() as db:
            db.execute("UPDATE outgoing SET state='unknown' WHERE state='sending' AND token IN "
                       "(SELECT token FROM contexts WHERE namespace=?)", (self.namespace,))
        # Executing a partly completed task again could repeat external work.
        # Retain its record and report the interruption to the original main chat.
        for row in self.broker.active(self.namespace):
            if row["state"] in ("queued", "running"):
                context = self.broker.context(row["token"], allow_expired=True)
                if row["kind"] == "bot_command" and json.loads(row["result"] or "{}").get("command") == "emotion":
                    self.broker.update(row["id"], "failed", {"command": "emotion", "success": False,
                        "error": "重启中断了情绪分析，请重新发送 /emotion；未自动重复分析或发送。"})
                    continue
                if row["kind"] in ("sticker", "sandbox_play"):
                    previous = json.loads(row["result"] or "{}")
                    if previous.get("sandbox_session_id"):
                        from sandbox_runtime import SandboxRuntime
                        try:
                            SandboxRuntime(self.client.app_config.sandbox.directory).finish(context,
                                previous["sandbox_session_id"], "interrupted", "机器人重启中断，保留游戏、画面和学习记录。")
                        except (ValueError, PermissionError):
                            pass
                    self.broker.update(row["id"], "unknown" if row["kind"] == "sticker" else "failed",
                        {**previous, "error": "机器人重启中断，未自动重放输入或媒体发送。"})
                    continue
                if row["kind"] == "receive_file":
                    self.broker.update(row["id"], "failed", {"success": False, "code": "INTERRUPTED",
                        "error": "重启中断了文件接收，请使用当前上下文重新接收。"})
                    continue
                self.broker.update(row["id"], "ready", {"type": "task_result", "request_id": row["id"],
                    "source_message_id": context["source_message_id"], "task": row["task"], "success": False,
                    "error": "机器人重启中断了该任务；保留已有文件，未自动重复执行。"})
        from subagent_runtime import SubagentRuntime
        self.agents = SubagentRuntime(self)

    def system_for(self, session_key, *, include_growth=True, components=False):
        from persona_isolation import session_group, persona_for
        group = session_group(session_key)
        cfg = self.client.app_config.bot
        persona = persona_for(self.client, group, session_key)
        names = self.client._bot_names(group) if hasattr(self.client, "_bot_names") else [persona.name]
        base_prompt = persona.prompt
        from agent_core.runtime import for_client
        runtime = for_client(self.client)
        if runtime:
            actor_id = runtime.bind_legacy(self.namespace, getattr(persona, "id", "") or persona.name, persona.name, persona.prompt)
            actor = runtime.store.actor(actor_id)
            base_prompt = actor["prompt"] or base_prompt
            names = list(dict.fromkeys([*names, actor["name"]]))
        from persona_isolation import identity
        growth = self.identity.preset(identity(self.client, persona))
        persona_text = base_prompt
        if include_growth and growth["text"]:
            base_prompt += "\n\n个人成长预设（由本人通过 CLI 修订，服从宿主规则与权限）：\n" + growth["text"]
        system = compose_chat_system_prompt(base_prompt, cfg, bot_names=names, master_name=cfg.get_master_name(group))
        if not components:
            return system
        behavior = {"action_descriptions": cfg.enable_action_descriptions, "reply_char_limit": cfg.reply_char_limit,
            "bubble_chars": cfg.max_reply_length, "names": names, "public_name": cfg.bot_name,
            "master_name": cfg.get_master_name(group)}
        behavior["instruction"] = ("动作神态描写可适量使用。" if cfg.enable_action_descriptions else
            "纯对话台词模式：不写动作、神态、心理旁白，颜文字可保留，保留身份及其他设定。")
        behavior["instruction"] += (f"普通聊天本轮全部回复合计最多 {cfg.reply_char_limit} 字，命令结果和文件不受此限制。" if cfg.reply_char_limit else
            "普通聊天整轮没有固定总字数上限，按语境展开。")
        return system, persona_text, growth, behavior

    def prepare_startup(self):
        from persona_isolation import session_is_active
        self.start()
        if "__session_cleanup__" not in self.startup_tasks:
            self.startup_tasks["__session_cleanup__"] = asyncio.create_task(self._prepare_cleanup())
        prefix = "onebot_" if hasattr(self.client, "send_action") else ""
        keys = {key for key in self.client.ai._sessions if key.startswith((prefix + "group_", prefix + "private_", "c2c_"))}
        keys = {key for key in keys if session_is_active(self.client, key)}
        keys.update(self.client._chat_session_key(prefix + "group_" + str(group)) for group in self.client.app_config.bot.group_settings)
        for key in keys:
            if key not in self.startup_tasks:
                self.startup_tasks[key] = asyncio.create_task(self._prepare(key))

    async def _prepare_cleanup(self):
        """Recover cleanup from saved successes, including results awaiting delivery."""
        ai = self.client.ai
        from activity_memory import flush_activity_memories
        await flush_activity_memories(self)
        if self.client.app_config.bot.memory_enabled:
            await self.client.memory_mgr.summarize_and_save_session(ai, recover_only=True)
        if not hasattr(ai, "queue_work_cleanup"):
            return
        try:
            ai.queue_probe_cleanup()
            with self.broker.connect() as db:
                rows = db.execute("SELECT id,kind,state,result FROM requests WHERE namespace=? "
                    "AND result LIKE '%agent_session_key%'", (self.namespace,)).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["result"])
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("success") is True and isinstance(payload.get("result"), dict):
                        data = payload["result"]
                        job_id = data.get("job_id")
                        if not isinstance(job_id, str) or not job_id:
                            continue
                        agent_key = "__work__:task:" + job_id
                        archive = data.get("archive")
                    elif row["kind"] == "sandbox_play" and row["state"] == "completed" and payload.get("full_result_file"):
                        # Finished sandbox activities save their notes before
                        # returning. Paused/running activities keep their branch.
                        data = payload
                        agent_key = "__work__:task:sandbox:" + row["id"]
                        archive = data["full_result_file"]
                    else:
                        continue
                    if not archive:
                        continue
                    path = Path(archive)
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(saved, dict) or saved.get("state") in ("paused", "running", "failed", "cancelled"):
                        continue
                    ai.queue_work_cleanup(agent_key, str(path),
                        completed_at=min(saved.get("completed_at", path.stat().st_mtime), path.stat().st_mtime))
                except (OSError, ValueError, TypeError):
                    logger.warning("一项已完成任务的清理记录不可用，保留原会话。")
            ai.tick_archives()
        except Exception:
            logger.exception("启动会话清理未完成，已保留结果和会话")

    async def _prepare(self, key):
        try:
            # Local restoration never waits for a model or archive cleanup.
            async with self.locks.setdefault(key, asyncio.Lock()):
                path, _, _ = await finish_thread(self.rules, key, self.system_for(key))
                self.client.ai.get_or_create_session(key).chat_state["rules_file"] = str(path)
                from persona_isolation import session_group, persona_for, identity
                from bot_identity import scope_for, archive_ready
                group = session_group(key)
                if group and self.client.app_config.bot.memory_enabled:
                    persona = persona_for(self.client, group, key)
                    owner = identity(self.client, persona)
                    manager = self.client.memory_mgr.manager(group, persona=persona)
                    memory = await finish_thread(manager.get_group_memory, group)
                    for person in memory.participants.values():
                        await finish_thread(self.identity.observe, owner, scope_for(self.client, group), person.openid, person.nickname)
                    latest = Path(manager.storage_dir) / ("group_" + "".join(c for c in group if c.isalnum() or c in "-_") + ".summary.json")
                    if latest.exists():
                        await finish_thread(archive_ready, self.client, owner, group, latest)
                self.client.ai._save_sessions()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("主会话 %s 本地规则准备未完成，下一条消息会继续加载。", key)

    def start(self):
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._watch())

    def directory(self, session_key):
        session_key = self.client._chat_session_key(session_key)
        return self.root / hashlib.sha256((self.namespace + ":" + session_key).encode()).hexdigest()[:24]

    def remember_context(self, key, ctx):
        from conversation_routing import background_allowed
        self.contexts_by_scope[(key, ctx.scope)] = ctx
        previous = self.contexts.get(key)
        if not previous or not previous.guard() or background_allowed(self.client, ctx):
            self.contexts[key] = ctx

    def context_for(self, key, scope=""):
        ctx = self.contexts_by_scope.get((key, scope)) if scope else None
        ctx = ctx or self.contexts.get(key)
        return ctx if ctx and (not scope or ctx.scope == scope) else None

    def rules(self, session_key, system):
        directory = self.directory(session_key)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "rules.md"
        text = compose_main_rules(system)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
        return path, text, hashlib.sha256(text.encode()).hexdigest()

    def issue_chat_context(self, ctx, session_key, memory_manifest=None, *, speech_check_required=False, emotion_analysis=False, sender_name="", ack_before_work=False):
        """Bind public CLI capabilities to real transport messages and persona."""
        client = self.client
        sources = list(client._chat_turns.groups.get(ctx.scope, {}).values())[-100:]
        sources += ctx.reply_sources
        if ctx.message_id and not any(row.get("id") == ctx.message_id for row in sources):
            sources.append({"id": ctx.message_id, "sender_id": ctx.sender_id,
                            "text": ctx.text, "received_at": ctx.received_at})
        sources = [{key: value for key, value in row.items() if key != "label"} for row in sources]
        sources = list({str(row["id"]): row for row in sources if row.get("id")}.values())
        service = client._get_media_service()
        output_root = Path(service.config.cache_dir).resolve() / "console" / hashlib.sha256(
            (ctx.scope + ":" + session_key).encode()).hexdigest()[:20]
        output_dir = output_root / hashlib.sha256(ctx.message_id.encode()).hexdigest()[:24]
        output_dir.mkdir(parents=True, exist_ok=True)
        received_root = Path(service.config.cache_dir).resolve() / "received" / hashlib.sha256(ctx.scope.encode()).hexdigest()[:20]
        from voice_service import voice_context
        from voice_config import VoiceConfig
        from voice_live import current_voice
        voice_config = current_voice(client)
        payload = {"namespace": self.namespace, "scope": ctx.scope, "session_key": session_key,
            "voice_config": voice_config.model_dump(), "voice": voice_context(voice_config),
            "voice_config_path": str(Path(getattr(client, "config_path", "config.yaml")).resolve()),
            "transport": ctx.transport, "turn_managed": True, "reply_slots": max(0, 5 - ctx.sequence),
            "generation": session_generation(client.ai, session_key), "source_message_id": ctx.message_id,
            "sender_id": ctx.sender_id, "is_group": ctx.is_group, "conversation_id": ctx.conversation_id,
            "is_master": client.app_config.bot.is_master(ctx.conversation_id if ctx.is_group else "", ctx.sender_id),
            "is_admin": client.app_config.bot.is_admin(ctx.conversation_id if ctx.is_group else "", ctx.sender_id, ctx.sender_role),
            "sender_role": ctx.sender_role, "mentioned_user_ids": ctx.mentioned_user_ids,
            "input_images": ctx.quoted_images, "reply_char_limit": 0 if emotion_analysis else client.app_config.bot.reply_char_limit,
            "quoted_message_id": ctx.quoted_message_id,
            "main_agent": True, "emotion_analysis": emotion_analysis,
            "stream_replies": True, "ack_before_work": ack_before_work or speech_check_required and not emotion_analysis,
            "memory_files": [item["path"] for item in (memory_manifest or {}).get("files", [])],
            "agent_notice_ids": [row["seq"] for row in self.agents.pending(session_key)],
            "received_at": ctx.received_at, "source_text": ctx.text, "reply_sources": sources,
            "speech_check_required": speech_check_required, "speech_current_messages": ctx.reply_sources,
            "search_enabled": client.app_config.bot.search_enabled, "bot_id": str(getattr(client, "self_id", "") or
                getattr(getattr(getattr(getattr(client, "_connection", None), "state", None), "robot", None), "member_openid", "") or
                getattr(getattr(getattr(getattr(client, "_connection", None), "state", None), "robot", None), "id", "")),
            "runtime_database": str(Path(client.app_config.bot.runtime_database_path).resolve()),
            "proactive_event": ctx.proactive_event,
            "self_activity_config": {"enabled": client.app_config.bot.self_activity_enabled
                and client.app_config.bot.autonomous_enabled},
            "work_permissions": client.app_config.bot.get_work_permissions(
                ctx.conversation_id if ctx.is_group else "").model_dump(),
            "sticker_config": {**client.app_config.stickers.model_dump(), "directory": str(Path(client.app_config.stickers.directory).resolve())},
            "sandbox_config": {**client.app_config.sandbox.model_dump(), "directory": str(Path(client.app_config.sandbox.directory).resolve())},
            "output_dir": str(output_dir), "artifact_roots": [str(output_root), str(received_root),
                str(self.directory(session_key) / "agents"),
                *[str(Path(root).resolve()) for root in client.app_config.media.local_library_roots]]}
        from agent_core.runtime import for_client
        from bot_identity import bind
        payload.update(bind(client, session_key))
        from broadcast_commands import context_binding as broadcast_binding
        if (getattr(getattr(client.app_config, "bilibili", None), "enabled", False)
                or getattr(getattr(client.app_config, "vtuber", None), "enabled", False)):
            payload.update(broadcast_binding(payload["voice_config_path"], is_master=payload["is_master"]))
        from exploration import configuration
        payload["exploration_config"] = configuration(self, session_key, source=ctx)
        from conversation_routing import source_binding
        payload["conversation_binding"] = source_binding(client, ctx)
        self.identity.observe(payload["identity_owner"], ctx.scope, ctx.sender_id, sender_name or getattr(ctx, "sender_name", ""))
        runtime = for_client(client)
        if runtime:
            from persona_isolation import persona_for, session_group
            payload.update(runtime.chat_context(self.namespace, session_key,
                persona_for(client, session_group(session_key), session_key), ctx, self.broker))
        context_file = self.broker.issue_context(payload, self.directory(session_key) / "contexts")
        token = json.loads(Path(context_file).read_text(encoding="utf-8"))["token"]
        payload = self.broker.context(token)
        self.requests[context_file] = ctx
        self.request_contexts[token] = ctx
        if not ctx.proactive_event:
            self.remember_context(session_key, ctx)
        while len(self.requests) > 512:
            self.requests.pop(next(iter(self.requests)))
        while len(self.request_contexts) > 512:
            self.request_contexts.pop(next(iter(self.request_contexts)))
        return context_file, token, payload

    def work_ready(self, token):
        context = self.broker.context(token, allow_expired=True)
        token = context.get("ack_token") or token
        if not context.get("ack_before_work"):
            return True
        mode = self.broker.reply_control(token).get("mode")
        if mode in ("wait", "sealed", "finished") and not self.broker.output_pending(token):
            return True
        with self.broker.connect() as db:
            committed = db.execute("SELECT 1 FROM reply_batches WHERE token=? LIMIT 1", (token,)).fetchone()
        return bool(committed) and not self.broker.output_pending(token)

    async def _chat_with_delivery(self, key, prompt, *, ctx=None, **options):
        """Keep the model active while committed speech reaches the transport.

        This runs inside the owner's reply-queue lease, so sending does not
        reacquire it from a child task or interleave another conversation turn.
        """
        natural_message = options.pop("natural_message", False)
        if ctx is None:
            return await self.client.ai.chat(key, prompt, **options)
        from persona_isolation import persona_for, session_group
        from conversation_runtime import current_event
        active = current_event.get()
        natural = bool(natural_message and active and active.batchable and active.scope == ctx.scope
                       and active.message_id == ctx.message_id)
        if natural:
            active.text = ctx.text
            for row in active.batch:
                if row.get("id") == ctx.message_id:
                    row["text"] = ctx.text
            active.reply_generating = True
        name = persona_for(self.client, session_group(key), key).name
        attempted_deliveries = set()
        async def flush():
            if natural and self.client._rebatch_reply_if_needed(ctx):
                return
            commands = self.broker.committed_messages(ctx.console_token)
            if commands:
                saved = ctx.cli_messages
                ctx.cli_messages = commands
                ctx.cli_streamed = True
                try:
                    text = await self.client._send_decision_batch(None, ctx, name)
                    if text:
                        ctx.cli_sent_texts.append(text)
                finally:
                    ctx.cli_messages = saved
                await self.poll()
            # An approved task result is also independent of model completion.
            # Use the existing receipt/cancellation path in this queue owner.
            with self.broker.connect() as db:
                deliveries = db.execute("SELECT id FROM requests WHERE namespace=? AND scope=? AND state='ready' "
                    "AND json_extract(result,'$.delivery.approved_by')=? ORDER BY created", (self.namespace, ctx.scope, ctx.console_token)).fetchall()
            for row in deliveries:
                if row["id"] in attempted_deliveries:
                    continue
                attempted_deliveries.add(row["id"])
                try:
                    await self.client._deliver_console_result(row["id"], ctx, self.broker.context(ctx.console_token))
                except Exception:
                    logger.exception("已批准成果的即时交付未完成，保留回执供结果队列处理: %s", row["id"])
        model = asyncio.ensure_future(self.client.ai.chat(key, prompt, **options))
        try:
            while not model.done():
                await flush()
                if not model.done():
                    await asyncio.wait({model}, timeout=.02)
            try:
                return await model
            except SessionInvalidatedError:
                if natural:
                    active.rebatch_requested = False
                raise
            except Exception:
                if not (natural and active.rebatch_requested):
                    raise
                return ""
            finally:
                await flush()
        finally:
            if not model.done():
                model.cancel()
            await asyncio.gather(model, return_exceptions=True)
            if natural:
                active.reply_generating = False
                if active.rebatch_requested:
                    with self.broker.connect() as db:
                        delivered = [{"id": row["id"], "state": row["state"],
                                      "text": json.loads(row["receipt"]).get("sent_text", json.loads(row["payload"])["text"])}
                            for row in db.execute("SELECT id,state,payload,receipt FROM outgoing WHERE token=? "
                                "AND state IN ('accepted','queued','unknown','sending') ORDER BY created,rowid", (ctx.console_token,))]
                    active.interrupted_replies.append({"source_message_id": ctx.message_id, "messages": delivered})

    async def submit_chat_command(self, ctx, text):
        from console_tools.chat_commands import submit_command
        from console_tools.files import file_error
        key = self.client._chat_session_key(ctx.ai_session_key)
        from console_tools.chat_commands import parse_command, WORK
        try:
            parsed = parse_command(text)
        except (ValueError, PermissionError):
            parsed = None  # submit_command below produces the scoped error.
        needs_ack = bool(parsed and parsed.name in WORK - {"next", "library", "groupfile", "pick"})
        context_file, token, metadata = self.issue_chat_context(ctx, key, ack_before_work=needs_ack)
        try:
            receipt = submit_command(self.broker, token, text)
        except (ValueError, PermissionError) as error:
            # Invalid commands also enter the result queue; they never become
            # arbitrary shell instructions or direct transport messages.
            receipt = self.broker._enqueue(token, metadata, "command_error", text, file_error(error))
        row = self.broker.record(receipt["id"])
        command = json.loads(row["result"]).get("command", "")
        long_command = row["kind"] in ("search", "generate_image", "generate_video", "image", "video", "file", "task", "code", "process") or command in WORK - {"next", "library", "groupfile", "pick"}
        if long_command:
            # Explicit work commands need no model invocation just to say that
            # preparation is starting. Use the same transactional send path.
            async with self.client._reply_turn(key, ctx):
                ctx.console_token = token
                self.broker.reply(token, ["收到，我在准备了，完成后把结果发来。"], request_key="command-ack", finish=True)
                ctx.cli_messages = self.broker.committed_messages(token)
                from persona_isolation import persona_for, session_group
                name = persona_for(self.client, session_group(key), key).name
                spoken = await self.client._send_decision_batch(None, ctx, name)
                if spoken:
                    self.client._record_console_text(ctx, name, spoken)
                ctx.cli_messages = []
        self.start()
        await self.poll()
        return receipt

    async def interrupt_background(self, session_key, *, foreground=False):
        """Yield silent housekeeping before foreground input waits for a turn."""
        mappings = [self.startup_tasks, self.recall_tasks]
        if foreground:
            mappings += [self.activity_review_tasks, self.self_activity_tasks, self.agents.review_tasks]
        for mapping in mappings:
            task = mapping.get(session_key)
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def chat(self, session_key, prompt, *, ctx=None, system_prompt=None, memory_manifest=None, check_current=None,
                   idle_event=False, activity_scope="", activity_review=False, self_activity=False, agent_event=None,
                   emotion_analysis=False, memory_maintenance=False, live_event=None, **options):
        client = self.client
        session_key = session_key.removeprefix("__work__:")
        session = client.ai.get_or_create_session(session_key)
        generation = session.generation
        if ctx:
            from persona_isolation import base_session
            from live_events import ROOT, reset_activity
            if base_session(session_key) == ROOT: reset_activity(self.broker, self.namespace)
        if live_event:
            from live_events import ROOT, ROOM
            from persona_isolation import base_session
            if ctx or not idle_event or base_session(session_key) != ROOT:
                raise PermissionError("无效直播主会话绑定。")
            with self.broker.connect() as db:
                row = db.execute("SELECT 1 FROM live_events WHERE id=? AND namespace=? AND root=? AND room=? AND state='running' AND generation=?",
                    (live_event.get("event_id"), self.namespace, ROOT, ROOM, generation)).fetchone()
            if not row: raise PermissionError("直播事件不属于当前主会话。")
        self.start()
        await self.interrupt_background(session_key, foreground=bool(ctx))
        async with self.replies.turn(session_key), self.locks.setdefault(session_key, asyncio.Lock()):
            if session.generation != generation:
                raise SessionInvalidatedError("主会话已重置，本轮增量已取消。")
            if ctx and ctx.proactive_event and self.broker.has_pending_speech(self.namespace, ctx.scope, session_key, generation):
                return reply_json(plain_reply("", client.app_config.bot.max_reply_length))
            state = session.chat_state
            from conversation_routing import shared_memory_manifest, source_binding, binding, source_turn_state, remember_turn_state
            route = source_binding(client, ctx) if ctx else binding(client, session_key)
            memory_manifest = shared_memory_manifest(self, session_key, memory_manifest) if client.app_config.bot.memory_enabled else memory_manifest
            from idle_memory import IdleMemory
            try:
                reference = IdleMemory(self.directory(session_key), generation).reference()
            except (OSError, ValueError):
                reference = None
            if reference:
                memory_manifest = {**(memory_manifest or {}), "files": [*(memory_manifest or {}).get("files", []), reference]}
                memory_manifest["fingerprint"] = hashlib.sha256(json.dumps(memory_manifest["files"], sort_keys=True).encode()).hexdigest()[:20]
            from bot_identity import bind
            identity_binding = bind(client, session_key)
            identity_owner = identity_binding["identity_owner"]
            identity_scope = ctx.scope if ctx else activity_scope or session_key
            maintenance = self.identity.pending(identity_owner, identity_scope) if not emotion_analysis else []
            if maintenance:
                archive_refs = [{"role": "archive_summary", "path": item["path"], "version": item["version"]}
                                for item in maintenance if item.get("path") and Path(item["path"]).is_file()]
                memory_manifest = {**(memory_manifest or {}), "files": [*(memory_manifest or {}).get("files", []), *archive_refs]}
                memory_manifest["fingerprint"] = hashlib.sha256(json.dumps(memory_manifest["files"], sort_keys=True).encode()).hexdigest()[:20]
            if route["shared"] and memory_manifest:
                memory_manifest["files"] = list({str(Path(item["path"]).resolve()).casefold(): item
                                                 for item in memory_manifest.get("files", [])}.values())
                memory_manifest["fingerprint"] = hashlib.sha256(json.dumps(memory_manifest["files"], sort_keys=True).encode()).hexdigest()[:20]
            system, persona_text, growth, behavior = self.system_for(session_key, components=True)
            path, rules, fingerprint = self.rules(session_key, system)
            protocol_hash = hashlib.sha256(MAIN_RULES.encode()).hexdigest()
            bootstrap = not session.conversation_id or state.get("conversation_id") != session.conversation_id
            persona_hash = hashlib.sha256(system.encode()).hexdigest()
            persona_base_hash = hashlib.sha256(persona_text.encode()).hexdigest()
            behavior_hash = hashlib.sha256(json.dumps(behavior, sort_keys=True).encode()).hexdigest()
            parts = []
            if ctx and route["shared"]:
                parts.append(json.dumps({"conversation_source": {"scope": ctx.scope, "primary": route["primary"],
                    "mode": route["mode"], "instruction": "本轮只向当前来源回复，权限以当前 CLI 上下文为准。"}}, ensure_ascii=False, separators=(",", ":")))
            context_token = ""
            feedback_signature = None
            previous_failure = source_turn_state(state, "previous_turn_failure", ctx.scope,
                shared=route["shared"]) if ctx else None
            previous_delivery = (source_turn_state(state, "last_delivery", ctx.scope,
                shared=route["shared"]) or {}) if ctx else {}
            if ctx and not ctx.proactive_event and previous_failure:
                parts.append(json.dumps({"previous_turn_failure": previous_failure}, ensure_ascii=False))
            if not session.conversation_id:
                parts.append(f"@rules: {path.as_posix()}")
            else:
                if bootstrap:
                    parts.append(json.dumps({"type": "continuity_resume", "preserve_identity_and_history": True,
                        "instruction": "继续当前持久身份、关系和已发生的经历。本次为程序恢复连接，沿用本会话上下文与长期记忆。"}, ensure_ascii=False))
                if state.get("protocol_hash") != protocol_hash:
                    parts.append(json.dumps({"type": "protocol_update", "preserve_identity_and_history": True,
                        "rules": MAIN_RULES}, ensure_ascii=False))
                if state.get("behavior_hash") != behavior_hash:
                    parts.append(json.dumps({"type": "behavior_update", "constraints": behavior,
                        "preserve_identity_and_history": True}, ensure_ascii=False))
                if state.get("persona_base_hash") and state["persona_base_hash"] != persona_base_hash:
                    parts.append(json.dumps({"type": "persona_update", "system_prompt": persona_text,
                        "preserve_identity_and_history": True}, ensure_ascii=False))
                if (state.get("growth_revision") != growth["revision"] and (growth["text"] or state.get("growth_revision", 0))):
                    parts.append(json.dumps({"type": "growth_update" if "growth_revision" in state else "growth_resume", "growth": growth,
                        "preserve_identity_and_history": True}, ensure_ascii=False))
            if ctx:
                if not ctx.proactive_event:
                    self.remember_context(session_key, ctx)
                from conversation_runtime import current_event
                active = current_event.get()
                # Individual follow-ups wait; a configured backlog can interrupt
                # this natural turn and resubmit the complete input batch.
                ctx.reply_supersedable = False
                ctx.reply_sources = list(active.batch) if active and active.scope == ctx.scope else []
                if ctx.is_group and ctx.reply_supersedable and client._chat_turns.superseded(ctx.scope, ctx.message_id, ctx.sender_id):
                    logger.info("[%s] 来源 %s 已有同轮补充，跳过过时输入。", session_key, ctx.message_id)
                    return reply_json(plain_reply("", client.app_config.bot.max_reply_length))
                message_event = None
                # Private image analysis may precede the controller's JSON.
                # Locate that event without dropping any image context.
                prompt_lines = prompt.splitlines()
                for index, line in enumerate(prompt_lines):
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(event, dict) and event.get("type") == "message":
                        event.update(id=ctx.message_id, user_id=ctx.sender_id)
                        message_event = event
                        state["last_input"] = time.time()
                        if not ctx.proactive_event:
                            state["self_activity_wait_count"] = 0
                        prompt_lines[index] = json.dumps(event, ensure_ascii=False)
                        prompt = "\n".join(prompt_lines)
                        break
                context_file, context_token, payload = self.issue_chat_context(ctx, session_key, memory_manifest,
                    speech_check_required=message_event is not None and not ctx.proactive_event,
                    emotion_analysis=emotion_analysis, sender_name=(message_event or {}).get("sender", ""))
                ctx.console_token = context_token
                ctx.cli_messages = []
                ctx.cli_sent_texts = []
                ctx.cli_streamed = False
                quick_commands = {}
                # Only task results need a bound delivery example; ordinary
                # replies use the current context and the persistent tool schema.
                try:
                    event = json.loads(prompt.splitlines()[0])
                    if isinstance(event, dict) and event.get("type") == "task_result" and event.get("request_id"):
                        delivery_args = {"request_id": event["request_id"], "finish": True}
                        if not event.get("command_result"):
                            delivery_args["text"] = "替换为完成台词"
                        quick_commands["deliver_call"] = {"action": "deliver", "context_file": context_file, "params": delivery_args}
                except (ValueError, IndexError):
                    pass
                files = [item for item in self.broker.inbox(context_token) if item["source_message_id"] == ctx.message_id]
                call_context = {"console_context_file": context_file, **quick_commands}
                voice_capability = payload.get("voice", {"enabled": False})
                if voice_capability.get("enabled") or state.get("voice_enabled"):
                    call_context["voice"] = {key: value for key, value in voice_capability.items() if key != "instruction"}
                state["voice_enabled"] = bool(voice_capability.get("enabled"))
                if files:
                    call_context["received_files"] = files
                if payload["reply_char_limit"]:
                    call_context["reply_char_limit"] = payload["reply_char_limit"]
                if payload.get("speech_check_required"):
                    speech = self.broker.speech_status(context_token)
                    extra = [row for row in speech["current_messages"] if row.get("id") != ctx.message_id]
                    if speech["cached_messages"] or extra:
                        call_context["speech"] = {"cached_messages": speech["cached_messages"], "current_messages": extra}
                parts.append(json.dumps(call_context, ensure_ascii=False, separators=(",", ":")))
                batch = (active.batch or active.merged) if active and active.scope == ctx.scope else []
                from reply_policy import reply_style
                turn = {"source_message_id": ctx.message_id, "reply_style": reply_style(ctx.text, batch),
                    "messages": [item for item in batch if not (item.get("id") == ctx.message_id and item.get("text") == ctx.text)]}
                if active and active.scope == ctx.scope and active.interrupted_replies:
                    turn["interrupted_replies"] = active.interrupted_replies
                    turn["instruction"] = ("聊天积压后重新合并的输入。按发言人、时间和先后关系综合理解，"
                        "由你判断回答哪些问题、如何合并回应或等待。旧轮未发送内容已取消；"
                        "interrupted_replies 中 accepted 已发送，queued 等待投递，unknown/sending 结果不确定，均不要重复发送。"
                        "已提交的后台任务继续运行，先检查现有任务再决定是否安排新工作。")
                delivery = previous_delivery
                if delivery.get("state") in ("unknown", "failed", "blocked", "queued"):
                    turn["previous_delivery"] = delivery
                if turn["messages"] or turn["reply_style"] != "casual" or turn.get("previous_delivery"):
                    parts.append(json.dumps({"turn": turn}, ensure_ascii=False, separators=(",", ":")))
                if message_event is not None:
                    from browser_requests import direct_browser_request
                    browse = direct_browser_request(message_event.get("text", ""), is_master=payload["is_master"],
                                                    directed=message_event.get("directed", not ctx.is_group))
                    if browse:
                        from console_tools.files import file_error
                        try:
                            ctx.check_current()
                            receipt = await finish_thread(self.broker.sandbox, context_token, "browse", **browse)
                            ctx.check_current()
                            parts.append(json.dumps({"browser_request": {**browse, **receipt}}, ensure_ascii=False))
                            # Preparation is gated until the first reply receipt.
                            await self.poll()
                        except (OSError, ValueError) as error:
                            parts.append(json.dumps({"browser_request": {**browse, **file_error(error)}}, ensure_ascii=False))
                    else:
                        from autonomy_requests import direct_self_request
                        if direct_self_request(message_event.get("text", ""), is_master=payload["is_master"],
                                               directed=message_event.get("directed", not ctx.is_group)):
                            from console_tools.files import file_error
                            from agent_mailbox import AgentMailbox
                            try:
                                ctx.check_current()
                                # Admission is automatic only for the owner's
                                # explicit invitation; the worker chooses real
                                # sources, not a hardcoded URL or invented trip.
                                from exploration import ensure_exploration
                                receipt = ensure_exploration(self, session_key, source_token=context_token,
                                    goal="主人邀请你自由活动：" + ctx.text + "。自主选择有兴趣的真实资料，保留阅读发现与下一步。", force=True)
                                parts.append(json.dumps({"self_action_request": receipt}, ensure_ascii=False))
                                await self.poll()
                            except (OSError, ValueError, PermissionError) as error:
                                parts.append(json.dumps({"self_action_request": file_error(error)}, ensure_ascii=False))
            else:
                context_file = ""
                if idle_event:
                    background = self.contexts.get(session_key) if self_activity else None
                    metadata = {"namespace": self.namespace, "scope": activity_scope or session_key,
                        "session_key": session_key, "idle_event": True, "is_master": False,
                        "sender_id": "", "source_text": ""}
                    if background:
                        cfg = client.app_config
                        metadata.update(main_agent=True, self_activity=True, generation=generation,
                            source_message_id="", received_at=time.time(), transport=background.transport,
                            is_group=background.is_group, conversation_id=background.conversation_id,
                            self_activity_config={"enabled": cfg.bot.self_activity_enabled and cfg.bot.autonomous_enabled},
                            work_permissions=cfg.bot.get_work_permissions(
                                background.conversation_id if background.is_group else "").model_dump(),
                            sandbox_config={**cfg.sandbox.model_dump(), "directory": str(Path(cfg.sandbox.directory).resolve())})
                    if live_event:
                        metadata.update(live_event=True, event_id=live_event["event_id"], root=ROOT, room=ROOM,
                            scope="live:" + ROOM, is_master=False, memory_files=[item["path"] for item in (memory_manifest or {}).get("files", [])])
                    if agent_event:
                        metadata = {**agent_event, "main_agent": True, "idle_event": True, "agent_event": True,
                            "self_activity": False, "proactive_event": False, "turn_managed": False,
                            "source_message_id": "", "source_text": "", "generation": generation,
                            "artifact_roots": [str(self.directory(session_key) / "agents")],
                            "output_dir": str(self.directory(session_key) / "agents")}
                    metadata.update(identity_binding, main_agent=True, generation=generation)
                    from exploration import configuration
                    metadata["exploration_config"] = configuration(self, session_key,
                        source=self.context_for(session_key, activity_scope))
                    if memory_maintenance:
                        metadata.update(memory_maintenance=True, turn_managed=True,
                            memory_files=[item["path"] for item in (memory_manifest or {}).get("files", [])])
                    from agent_core.runtime import for_client
                    runtime = for_client(client)
                    if runtime and metadata.get("main_agent"):
                        from persona_isolation import persona_for, session_group
                        persona = persona_for(client, session_group(session_key), session_key)
                        actor_id = runtime.bind_legacy(self.namespace, getattr(persona, "id", "") or persona.name,
                                                       persona.name, persona.prompt)
                        metadata.update(actor_id=actor_id, actor_database=str(runtime.store.path))
                    context_file = self.broker.issue_context(metadata, self.directory(session_key) / "contexts")
                    context_token = json.loads(Path(context_file).read_text(encoding="utf-8"))["token"]
                    parts.append(json.dumps({"console_context_file": context_file,
                        "internal_event": "live" if live_event else "memory_maintenance" if memory_maintenance else "agent_messages" if agent_event else "self_activity" if self_activity else
                                          "activity_observation" if activity_review else "idle"}, ensure_ascii=False))
            if ctx or memory_maintenance or self_activity or agent_event:
                relevant_ids = list(dict.fromkeys([ctx.sender_id, *ctx.mentioned_user_ids,
                    *(str(row.get("sender_id", "")) for row in ctx.reply_sources)])) if ctx else []
                people = [self.identity.get(identity_owner, identity_scope, user) for user in relevant_ids if user]
                people = [row for row in people if not row.get("deleted") and row.get("state") != "missing"]
                recent = self.identity.contacts(identity_owner, identity_scope, limit=8)["contacts"]
                people += [row for row in recent if row["user_id"] not in relevant_ids]
                from bot_identity import brief_contact
                address_book = {"people": [brief_contact(person) for person in people[:16]]}
                address_hash = hashlib.sha256(json.dumps(address_book, sort_keys=True).encode()).hexdigest()
                if (people or state.get("address_hash")) and (bootstrap or state.get("address_hash") != address_hash):
                    parts.append(json.dumps({"address_book": address_book}, ensure_ascii=False))
                if maintenance:
                    parts.append(json.dumps({"memory_maintenance": maintenance}, ensure_ascii=False))
                memory_index = self.identity.memory_index(identity_owner, identity_scope, ctx.sender_id if ctx else "")
                memory_index_hash = hashlib.sha256(json.dumps(memory_index, sort_keys=True).encode()).hexdigest()
                if bootstrap or state.get("memory_index_hash") != memory_index_hash:
                    parts.append(json.dumps({"memory_index": memory_index}, ensure_ascii=False))
            agent_mail = self.agents.pending(session_key) if ctx or self_activity or agent_event else []
            if agent_event:
                with self.broker.connect() as db:
                    agent_mail = [row for row in agent_mail if db.execute(
                        "SELECT 1 FROM agent_mail WHERE seq=? AND token=?", (row["reply_to"], agent_event["_agent_source_token"])).fetchone()]
            if agent_mail:
                parts.append(json.dumps({"agent_messages": [{"seq": row["seq"], "agent_id": row["agent_id"],
                    "name": row["name"], **json.loads(row["payload"]), "text": row["text"][:1600],
                    "truncated": len(row["text"]) > 1600} for row in agent_mail]}, ensure_ascii=False))
            from agent_core.runtime import for_client
            runtime = for_client(client)
            if runtime and context_token:
                binding = self.broker.context(context_token)
                if binding.get("actor_id"):
                    actor_state = runtime.context(binding["actor_id"], scope=binding.get("scope", ""))
                    actor_hash = hashlib.sha256(json.dumps(actor_state, sort_keys=True).encode()).hexdigest()
                    if bootstrap or state.get("actor_hash") != actor_hash:
                        parts.append(json.dumps({"actor_state": actor_state}, ensure_ascii=False))
                    state["actor_hash"] = actor_hash
            from companion_life import state_for
            personal = await finish_thread(state_for, self.broker, self.namespace, session_key)
            personal = {**personal, "wishes": [{key: item[key] for key in ("id", "text", "status")}
                                              for item in personal["wishes"]]}
            personal_hash = hashlib.sha256(json.dumps(personal, sort_keys=True).encode()).hexdigest()
            if ((bootstrap and any(personal.values())) or not bootstrap and state.get("companion_hash") != personal_hash
                    and (any(personal.values()) or state.get("companion_hash"))):
                parts.append(json.dumps({"companion_state": {key: value for key, value in personal.items() if value}}, ensure_ascii=False))
            scope = ctx.scope if ctx else activity_scope
            if scope:
                feedback = await finish_thread(self.activity_feedback, session_key, scope)
                feedback_signature = self.activity_signature(feedback)
                seen_feedback = {} if bootstrap else state.get("activity_feedback_seen", {})
                changed = {key for key, value in feedback_signature.items() if seen_feedback.get(key) != value}
                if changed:
                    feedback = await finish_thread(self.activity_feedback, session_key, scope, True, changed)
                    compact = [{key: value for key, value in item.items() if key not in ("launch", "startup", "cli_steer", "cli_skip", "cli_restart") and value not in (None, "", [], {})}
                               for item in feedback if item["request_id"] in changed]
                    parts.append(json.dumps({"activity_feedback": compact}, ensure_ascii=False))
                removed = sorted(set(seen_feedback) - set(feedback_signature))
                if removed:
                    parts.append(json.dumps({"activity_feedback_removed": removed}, ensure_ascii=False))
            # Only messages omitted while a busy passive queue was coalescing
            # are replayed; the ordinary current message already is prompt.
            rows = []
            if ctx and ctx.is_group:
                rows = client.context_buffer.get_recent_messages(ctx.conversation_id, 50)
                seen_buffer = state.get("buffer_seen_by_scope", {}).get(ctx.scope, []) if route["shared"] else state.get("buffer_seen", [])
                seen = (seen_buffer if not bootstrap
                        or previous_delivery.get("state") == "blocked" else [])
                count = len(ctx.reply_sources) or 1
                pending = [row for row in rows[:-count] if row.get("event_id") not in seen and not row["is_bot"]]
                if pending:
                    parts.append(json.dumps({"unseen_messages": pending}, ensure_ascii=False))
            parts.append(prompt)
            if ctx and message_event is not None and not ctx.proactive_event:
                from idle_memory import IdleMemory
                names = {str(row["author_openid"]): row["author_name"] for row in rows}
                names[str(ctx.sender_id)] = message_event.get("sender") or getattr(ctx, "sender_name", "")
                IdleMemory(self.directory(session_key), generation).append([
                    *({**row, "source_scope": ctx.scope} for row in (pending if ctx.is_group else [])),
                    *({**row, "author_name": names.get(str(row.get("sender_id")), ""), "source_scope": ctx.scope}
                      for row in batch if row.get("id") != ctx.message_id),
                    {**message_event, "timestamp": ctx.received_at,
                             "reply_to": ctx.quoted_message_id, "source_scope": ctx.scope}])
            # Rules are sent in the event once, including on an existing CID
            # restored from older bot versions. They are not resent every turn.
            # Legacy chat timeout settings must not terminate a robot turn.
            options["timeout"] = 0
            options["allow_empty_response"] = True
            options["bind_response"] = True
            from main_agent_policy import main_cascade_config
            options["cascade_config"] = main_cascade_config()
            if ctx or idle_event:
                def stop_requested():
                    if ctx and active and active.scope == ctx.scope and active.rebatch_requested:
                        return "rebatched"
                    mode = self.broker.reply_control(context_token).get("mode")
                    return mode if mode in ("wait", "finished", "sealed") else False
                options["stop_requested"] = stop_requested
            started = time.monotonic()
            maintenance_done = False
            try:
                next_prompt = "\n\n".join(parts)
                for continuation in range(4):
                    if live_event:
                        if not await self.live_available() or not generation_is_current(client.ai, session_key, generation):
                            raise LiveUnavailable("直播关闭或主会话已改变。")
                        with self.broker.connect() as db:
                            still_running = db.execute("SELECT run FROM live_events WHERE id=? AND namespace=? AND state='running' AND generation=?", (live_event['event_id'], self.namespace, generation)).fetchone()
                        if not still_running or not self.live_run_current(still_running["run"]): raise LiveUnavailable("直播事件已停止。")
                    response = await self._chat_with_delivery(session_key, next_prompt, ctx=ctx, system_prompt=rules,
                        memory_manifest=memory_manifest, natural_message=bool(ctx and message_event is not None), **options)
                    if not (ctx or idle_event):
                        break
                    control = self.broker.reply_control(context_token).get("mode")
                    if control in ("wait", "finished", "sealed"):
                        maintenance_done = control == "finished"
                        break
                    if not generation_is_current(client.ai, session_key, generation):
                        raise SessionInvalidatedError("主会话已重置，停止旧轮维护。")
                    if ctx:
                        ctx.check_current()
                    if continuation == 3:
                        raise RuntimeError("主代理连续结束模型输出但未执行 finish；保留已提交回复及未完成整理。")
                    next_prompt = json.dumps({"type": "turn_continuation", "console_context_file": context_file,
                        "state": self.broker.reply_control(context_token).get("mode", "open"),
                        "next_action": "finish"}, ensure_ascii=False, separators=(",", ":"))
                if maintenance_done and maintenance:
                    reviewed = self.identity.reviewed(identity_owner, identity_scope)
                    self.identity.acknowledge(identity_owner, identity_scope,
                        [item["archive_id"] for item in maintenance if item["archive_id"] in reviewed])
            except BaseException as error:
                recoverable = ctx and isinstance(error, Exception) and not isinstance(error, SessionInvalidatedError)
                committed = recoverable and self.broker.seal_messages(
                    context_token, cancel=True, preserve_committed=True) is None
                if committed:
                    # The CLI transaction is the send decision. A missing final
                    # or late model error cannot undo a successful submission.
                    logger.warning("[%s] CLI 回复已提交，忽略随后模型结束错误: %s", session_key, type(error).__name__)
                else:
                    if ctx:
                        self.broker.seal_messages(context_token, cancel=True)
                    if (ctx and not ctx.proactive_event and isinstance(error, AgentServiceError)
                            and generation_is_current(client.ai, session_key, generation)):
                        remember_turn_state(state, "previous_turn_failure", ctx.scope,
                            {"source_message_id": ctx.message_id, "code": error.code, "reason": error.user_message})
                        remember_turn_state(state, "last_delivery", ctx.scope,
                            {"source_message_id": ctx.message_id, "state": "failed", "code": error.code})
                        client.ai._save_sessions()
                    if isinstance(error, ContentBlockedError) and generation_is_current(client.ai, session_key, generation):
                        # Consume this input batch so the next message does not
                        # automatically resubmit it as unseen_messages.
                        state.update(last_event=time.time(), last_should_reply=False,
                            buffer_seen=[row.get("event_id") for row in rows])
                        remember_turn_state(state, "last_delivery", ctx.scope if ctx else activity_scope,
                            {"source_message_id": ctx.message_id if ctx else "",
                             "state": "blocked", "code": ContentBlockedError.code})
                        if ctx and ctx.is_group and route["shared"]:
                            state.setdefault("buffer_seen_by_scope", {})[ctx.scope] = state["buffer_seen"]
                        client.ai._save_sessions()
                    raise
            finally:
                if idle_event:
                    # A finished or interrupted personal turn cannot be reused
                    # later as a standing grant to start another browser.
                    self.broker.seal_messages(context_token)
            if not generation_is_current(client.ai, session_key, generation):
                if ctx and active:
                    active.rebatch_requested = False
                if ctx:
                    self.broker.seal_messages(context_token, cancel=True)
                raise SessionInvalidatedError("主会话已重置，忽略旧结果。")
            if ctx:
                from persona_isolation import session_is_active
                if not session_is_active(client, session_key):
                    if active:
                        active.rebatch_requested = False
                    self.broker.seal_messages(context_token, cancel=True)
                    raise SessionInvalidatedError("当前人设已切换，旧人设回复不再发送。")
            try:
                if ctx:
                    ctx.check_current()
                if check_current:
                    check_current()
            except BaseException:
                if ctx:
                    self.broker.seal_messages(context_token, cancel=True)
                raise
            # Model final text is an internal completion, never a send request.
            decision = plain_reply("", client.app_config.bot.max_reply_length)
            if activity_review:
                decision = plain_reply("", client.app_config.bot.max_reply_length)
            idle_wait = idle_event and self.broker.reply_control(context_token).get("mode") == "wait"
            if idle_wait:
                decision = plain_reply("", client.app_config.bot.max_reply_length)
            if idle_event and self_activity:
                state["self_activity_wait_count"] = (
                    min(SELF_ACTIVITY_WAIT_LIMIT, state.get("self_activity_wait_count", 0) + 1)
                    if idle_wait else 0)
                if state["self_activity_wait_count"] == SELF_ACTIVITY_WAIT_LIMIT:
                    logger.info("[%s] 连续 %d 次 wait，暂停空闲自由活动提醒，等待新聊天消息。",
                                session_key, SELF_ACTIVITY_WAIT_LIMIT)
            if ctx:
                control = self.broker.reply_control(context_token)
                superseded = ctx.is_group and ctx.reply_supersedable and client._chat_turns.superseded(ctx.scope, ctx.message_id, ctx.sender_id)
                ctx.cli_messages = self.broker.seal_messages(
                    context_token, cancel=control.get("mode") == "wait" or superseded)
                decision.messages = [{"text": item["text"], "action": "none"} for item in ctx.cli_messages]
                decision.reply_text = "\n\n".join(item["text"] for item in ctx.cli_messages)
                decision.should_reply = bool(ctx.cli_messages or ctx.cli_sent_texts or ctx.cli_streamed)
                decision.reason = "CLI 已提交发送" if decision.should_reply else "未提交 CLI 发送命令"
                if control.get("mode") == "wait" or superseded:
                    ctx.cli_messages = []
                    decision.should_reply, decision.reply_text, decision.messages = False, "", []
                    decision.reason = "同一发言人已有补充，等待合并后的消息" if superseded else "等待对方说完或下一条消息"
                if not ctx.cli_streamed or ctx.cli_messages:
                    remember_turn_state(state, "last_delivery", ctx.scope,
                        {"source_message_id": ctx.message_id,
                         "state": "suppressed" if not decision.should_reply else "prepared", "reason": decision.reason})
                logger.info("[%s] 来源 %s: %s，agent 等待 %.2f 秒%s", session_key, ctx.message_id,
                    "正文准备发送" if decision.should_reply else "本轮不发送", time.monotonic() - started,
                    "；" + decision.reason if decision.reason else "")
            state.update(conversation_id=session.conversation_id, rules_hash=fingerprint,
                protocol_hash=protocol_hash, persona_hash=persona_hash, persona_base_hash=persona_base_hash,
                behavior_hash=behavior_hash, growth_revision=growth["revision"],
                companion_hash=personal_hash,
                last_event=time.time(), last_should_reply=decision.should_reply,
                rules_file=str(path))
            if ctx or memory_maintenance or self_activity or agent_event:
                state["address_hash"] = address_hash
                state["memory_index_hash"] = memory_index_hash
            if ctx and ctx.is_group:
                state["buffer_seen"] = [row.get("event_id") for row in rows]
                if route["shared"]:
                    state.setdefault("buffer_seen_by_scope", {})[ctx.scope] = state["buffer_seen"]
            if ctx and not ctx.proactive_event and previous_failure:
                state.get("previous_turn_failure_by_scope", {}).pop(ctx.scope, None)
                if previous_failure == state.get("previous_turn_failure"):
                    state.pop("previous_turn_failure", None)
            if feedback_signature is not None:
                state["activity_feedback_seen"] = feedback_signature
            client.ai._save_sessions()
            self.agents.mailbox.acknowledge(agent_mail)
            self.start()
            await self.poll()
            return reply_json(decision)

    async def poll(self):
        await self.agents.poll()
        from console_tools.work_permissions import authorize_work, current_work_context
        for request in self.broker.active(self.namespace):
            if request["state"] not in ("queued", "running"):
                continue
            context = current_work_context(self.client.app_config.bot,
                self.broker.context(request["token"], allow_expired=True))
            try:
                authorize_work(request["kind"], context, json.loads(request["result"]))
            except PermissionError as error:
                self.broker.update(request["id"], "cancel_requested",
                    {**json.loads(request["result"]), "success": False, "error": str(error)})
        for request in self.broker.cancellations(self.namespace):
            delivering = self.delivery_tasks.get(request["id"])
            if delivering and not delivering.done():
                delivering.cancel()
            result = json.loads(request["result"])
            if result.get("runtime_job_id") and self.client._runtime:
                self.client._runtime.cancel(job_id=result["runtime_job_id"], scope=request["scope"])
            plan = result.get("delivery", {})
            pending = plan.get("outbox_ids") or ([plan["outbox_id"]] if plan.get("outbox_id") else [])
            if pending and self.client._text_outbox:
                from messaging import SendError
                for identity in pending:
                    row = self.client._runtime.store.db.execute("SELECT state FROM outbox WHERE id=?", (identity,)).fetchone()
                    if row and row[0] == "pending":
                        self.client._text_outbox._finish(identity, "cancelled", SendError("该任务已取消。"))
            self.broker.update(request["id"], "cancelled")
        for request in self.broker.pending(self.namespace):
            try:
                context = self.broker.context(request["token"])
            except ValueError:
                self.broker.update(request["id"], "cancelled")
                continue
            if not self.work_ready(request["token"]):
                continue
            if context.get("self_directed") and not (self.client.app_config.bot.self_activity_enabled
                    and self.client.app_config.bot.autonomous_enabled):
                self.broker.update(request["id"], "cancelled")
                continue
            if request["kind"] == "sticker" and context.get("turn_managed"):
                if self.broker.reply_control(request["token"]).get("mode") != "sealed":
                    continue
                with self.broker.connect() as db:
                    if db.execute("SELECT 1 FROM outgoing WHERE token=? AND state IN ('staged','sending')",
                                  (request["token"],)).fetchone():
                        continue
            if request["kind"] == "sticker" and hasattr(self.client, "send_action") and not self.client._is_ws_connected():
                if time.time() - request["created"] > self.client.app_config.stickers.expire_seconds:
                    self.broker.update(request["id"], "cancelled", {"error": "表情包排队过期，未补发。"})
                continue
            ctx = self.request_contexts.get(request["token"])
            if ctx is None and context.get("self_directed"):
                from dataclasses import replace
                source = self.context_for(context["session_key"], context["scope"])
                if source:
                    ctx = replace(source, sender_id=context["sender_id"], message_id="",
                                  text=request["task"], proactive_event=False, reply_supersedable=False)
            if ctx is None:
                # OneBot can resume persisted handoffs. Official replies need a
                # fresh SDK reply context, so their files stay ready for pickup.
                if not hasattr(self.client, "send_action"):
                    continue
                ctx = self.client._make_media_context(session_key=context["session_key"], user_id=context["sender_id"],
                    conversation_id=context["conversation_id"], message_id=context["source_message_id"],
                    text=request["task"], is_group=context["is_group"], received_at=context["received_at"])
            if not generation_is_current(self.client.ai, context["session_key"], context["generation"]) or not ctx.guard():
                self.broker.update(request["id"], "cancelled")
                continue
            context = current_work_context(self.client.app_config.bot, context)
            try:
                authorize_work(request["kind"], context, json.loads(request["result"]))
            except PermissionError as error:
                self._persist_result(request, context, {"type": "task_result", "request_id": request["id"],
                    "source_message_id": ctx.message_id, "success": False, "error": str(error)})
                continue
            from conversation_runtime import EventContext
            event = EventContext(ctx.scope, ctx.message_id, ctx.received_at, "work", ctx.owner)
            try:
                job = self.client._get_runtime().submit_work(event, request["task"],
                    lambda r=request, c=ctx, meta=context: self._execute(r, c, meta), ctx.guard)
            except Exception as error:
                from messaging import SendError
                if not isinstance(error, SendError):
                    raise
                continue
            self.broker.update(request["id"], "queued", {**json.loads(request["result"]), "runtime_job_id": job})
        for request in self.broker.active(self.namespace):
            if request["state"] != "ready" or request["id"] in self.result_pending or time.time() < self.retry_after.get(request["id"], 0):
                continue
            payload = json.loads(request["result"] or "{}")
            plan = payload.get("delivery") or {}
            if payload.get("main_reviewed") and not plan.get("approved_by"):
                continue
            rt_id = payload.get("runtime_job_id")
            if rt_id and hasattr(self.client, "_get_runtime"):
                rt = self.client._get_runtime()
                if rt:
                    rt_job = rt.store.db.execute("SELECT state FROM work WHERE id=?", (rt_id,)).fetchone()
                    if rt_job and rt_job[0] == "cancelled":
                        self.broker.update(request["id"], "cancelled")
                        continue
            media_job_id = (payload.get("result") or {}).get("job_id") or payload.get("job_id")
            if media_job_id and hasattr(self.client, "_get_media_service"):
                srv = self.client._get_media_service()
                if srv:
                    try:
                        with srv.store.connect() as sdb:
                            m_row = sdb.execute("SELECT state FROM jobs WHERE id=?", (media_job_id,)).fetchone()
                        if m_row and m_row[0] == "cancelled":
                            self.broker.update(request["id"], "cancelled")
                            continue
                    except Exception:
                        pass
            metadata = self.broker.context(request["token"], allow_expired=True)
            if not generation_is_current(self.client.ai, metadata["session_key"], metadata["generation"]):
                self.broker.update(request["id"], "cancelled")
                continue

            # OneBot delivery always belongs to the task's original source.
            if hasattr(self.client, "send_action"):
                ctx = self.client._make_media_context(session_key=metadata["session_key"], user_id=metadata["sender_id"],
                    conversation_id=metadata["conversation_id"], message_id=metadata["source_message_id"],
                    text=request["task"], is_group=metadata["is_group"], received_at=metadata["received_at"])
            else:
                # A fresh official message renews the platform window. Keep its
                # shared sequence allocator instead of copying/resetting it.
                ctx = self.context_for(metadata["session_key"], metadata["scope"])
            if ctx is not None and ctx.guard():
                try:
                    ctx.check_reply_available()
                except Exception:
                    continue
                self._queue_result(request, ctx, metadata)

    async def _execute(self, request, ctx, metadata):
        from dataclasses import replace
        from skills.base import SkillContext, SkillResult
        from skills.task_skills import RunTaskSkill
        compatibility = json.loads(request["result"])
        runtime_id = json.loads(self.broker.record(request["id"])["result"]).get("runtime_job_id")
        self.broker.update(request["id"], "running")
        try:
            from console_tools.work_permissions import authorize_work, current_work_context
            metadata = current_work_context(self.client.app_config.bot, metadata)
            authorize_work(request["kind"], metadata, compatibility)
            if request["kind"] == "bot_command" and compatibility.get("command") == "emotion":
                from emotion_analysis import run_emotion
                return await run_emotion(self, request, ctx, metadata)
            if request["kind"] in ("sticker", "sandbox_play"):
                from companion_tasks import send_sticker, play_game
                operation = send_sticker if request["kind"] == "sticker" else play_game
                return await operation(self, request, ctx, metadata)
            worker_ctx = replace(ctx, ai_session_key="__work__:" + metadata["session_key"],
                                 message_id=ctx.message_id + ":console:" + request["id"])
            skill_context = SkillContext(worker_ctx.ai_session_key, ctx.conversation_id, ctx.sender_id,
                is_group=ctx.is_group, is_master=metadata.get("is_master", False), app_config=self.client.app_config,
                media_context=worker_ctx, media_service=self.client._get_media_service(), ai_client=self.client.ai)
            inputs = (await self._task_inputs(request, ctx, metadata, compatibility)
                      if request["kind"] in TASK_TYPES else [])
            if request["kind"] == "receive_file":
                from console_files import receive_qq_file
                received = await receive_qq_file(self.client, self.broker, request["token"], compatibility["file_id"], ctx)
                payload = {"success": True, "received_files": [received], "runtime_job_id": runtime_id,
                           "request_id": request["id"], "source_message_id": ctx.message_id}
                self._persist_result(request, metadata, payload, state="completed")
                return SkillResult(True, "文件已接收。", data=payload, reply_handled=True)
            elif request["kind"] == "singing":
                from singing_commands import execute_singing
                result = await execute_singing(self, request, ctx, metadata, compatibility, skill_context)
            elif request["kind"] == "send_files":
                result = await RunTaskSkill().deliver_files(compatibility["files"], skill_context,
                                                           roots=metadata["artifact_roots"])
            elif compatibility.get("legacy_skill") in ("group_slang", "send_file"):
                params = compatibility.get("params") or {}
                if compatibility["legacy_skill"] == "group_slang":
                    from skills.slang_skills import GroupSlangSkill
                    lookup = await GroupSlangSkill().execute(params, skill_context)
                    if not lookup.success:
                        raise ValueError(lookup.message)
                    answer = lookup.message
                else:
                    answer = str(params.get("content") or "")
                result = await RunTaskSkill().deliver_text(answer, skill_context,
                    task=request["task"], data={"defer_to_console": True})
            elif request["kind"] in ("bot_command", "command_error"):
                from console_tools.chat_commands import execute_control
                answer = execute_control(self, request, ctx, metadata) if request["kind"] == "bot_command" else compatibility["error"]
                # Reset/model/persona controls may change the receiving main
                # conversation. Bind their result to its new valid generation.
                from persona_isolation import effective_persona, scoped_session
                from conversation_routing import source_base
                source_key = source_base(self.client, ctx.conversation_id, ctx.is_group)
                key = scoped_session(self.client, source_key, effective_persona(self.client, key=source_key))
                ctx = self.client._make_media_context(session_key=key, user_id=ctx.sender_id,
                    conversation_id=ctx.conversation_id, message_id=ctx.message_id, text=ctx.text,
                    is_group=ctx.is_group, received_at=ctx.received_at, reply=ctx.send_text)
                metadata = {**metadata, "session_key": key, "generation": session_generation(self.client.ai, key)}
                self.remember_context(key, ctx)
                with self.broker.connect() as db:
                    db.execute("UPDATE contexts SET payload=? WHERE token=?", (json.dumps(metadata, ensure_ascii=False), request["token"]))
                result = SkillResult(request["kind"] != "command_error", answer,
                                     data={"answer": answer, "files": []}, reply_handled=True)
            elif request["kind"] == "media_command":
                from console_media import prepare_media_command
                result = await prepare_media_command(request, skill_context)
            elif request["kind"] in ("generate_image", "generate_video"):
                from skills.image_generation import GenerateImageSkill
                from skills.video_generation import GenerateVideoSkill
                try:
                    params = json.loads(request["task"])
                except ValueError:
                    params = {"prompt": request["task"]}
                if not isinstance(params, dict):
                    params = {"prompt": request["task"]}
                generation_skill = GenerateImageSkill() if request["kind"] == "generate_image" else GenerateVideoSkill()
                result = await generation_skill.execute({**params,
                    **{key: compatibility[key] for key in ("provider", "model") if key in compatibility}, "_input_files": inputs,
                    "_defer_delivery": True}, skill_context)
            elif compatibility.get("provider") == "gemini_web":
                from skills.gemini_web import GeminiWebTaskSkill
                result = await GeminiWebTaskSkill().execute({"task": request["task"],
                    "model": compatibility.get("model", ""), "_requires_network": request["kind"] == "search",
                    "_input_files": inputs, "_defer_delivery": True}, skill_context)
            elif request["kind"] in ("code", "process"):
                result = await RunTaskSkill().execute({"task": request["task"], "output_kind": "auto",
                    "_code": request["kind"] == "code", "_boost": request["kind"] == "code",
                    "_process": request["kind"] == "process", "_input_files": inputs,
                    "_requires_tools": True, "_defer_delivery": True}, skill_context)
            elif request["kind"] == "file" and worker_ctx.quoted_images:
                from media_router import parse_media_intent
                intent = parse_media_intent(request["task"])
                if intent and intent.operation == "quote" and intent.as_file:
                    from console_media import prepare_quoted_files
                    result = await prepare_quoted_files(skill_context)
                else:
                    result = await RunTaskSkill().execute({"task": request["task"], "output_kind": "file",
                        "_input_files": inputs, "_defer_delivery": True}, skill_context)
            else:
                result = await RunTaskSkill().execute({"task": request["task"],
                    "output_kind": request["kind"] if request["kind"] in ("image", "video", "file") else "auto",
                    "_requires_network": request["kind"] in ("search", "image", "video"),
                    "_requires_tools": request["kind"] == "command", "_input_files": inputs, "_defer_delivery": True},
                    skill_context)
            payload = {"type": "task_result", "request_id": request["id"], "source_message_id": ctx.message_id,
                "runtime_job_id": runtime_id,
                "task": request["task"], "success": result.success, "result": result.data or {},
                "error": "" if result.success else result.message,
                **({"delivery": compatibility["delivery"]} if compatibility.get("delivery") else {})}
            self._persist_result(request, metadata, payload)
            delivery = self._queue_result(request, ctx, metadata)
            if delivery:
                await delivery
            outcome = self.broker.record(request["id"])["state"]
            result.data = {**(result.data or {}), "state": "ready" if outcome == "ready" else
                           "unknown" if outcome == "unknown" else "completed"}
            return result
        except asyncio.CancelledError:
            if not self.closed and self.broker.record(request["id"])["state"] != "unknown":
                self.broker.update(request["id"], "cancelled")
            raise
        except Exception as error:
            if (request["kind"] in ("sticker", "sandbox_play")
                    or request["kind"] == "bot_command" and compatibility.get("command") == "emotion"):
                row = self.broker.record(request["id"])
                if row["state"] not in ("unknown", "cancelled", "failed"):
                    from messaging import SendUnknown
                    state = "failed"
                    if request["kind"] == "bot_command":
                        if isinstance(error, SessionInvalidatedError) or not ctx.guard():
                            state = "cancelled"
                        elif isinstance(error, SendUnknown):
                            state = "unknown"
                    self.broker.update(request["id"], state, {**json.loads(row["result"] or "{}"), "error": str(error)})
                logger.warning("%s 任务未完成，记录已保留：%s", request["kind"], error)
                return SkillResult(False, str(error), reply_handled=True)
            if request["kind"] in ("receive_file", "send_files"):
                from console_tools.files import file_error
                payload = {**file_error(error), "success": False, "runtime_job_id": runtime_id}
                self._persist_result(request, metadata, payload, state="failed")
                return SkillResult(False, str(error), data=payload, reply_handled=True)
            payload = {"type": "task_result", "request_id": request["id"], "source_message_id": ctx.message_id,
                "runtime_job_id": runtime_id,
                "task": request["task"], "success": False, "error": str(error)}
            self._persist_result(request, metadata, payload)
            self._queue_result(request, ctx, metadata)
            return SkillResult(False, "后台任务失败，结果已保存在控制台。", data=payload)

    async def _task_inputs(self, request, ctx, metadata, compatibility):
        from console_files import receive_qq_file
        from media_downloader import contained_path
        file_ids = compatibility.get("input_file_ids")
        if file_ids is None:  # queued records from older versions
            file_ids = self.broker.task_inputs(request["token"], request["task"], request["kind"])["input_file_ids"]
        inputs = [await receive_qq_file(self.client, self.broker, request["token"], identity, ctx)
                  for identity in file_ids]
        seen = {str(Path(item["local_path"]).resolve()) for item in inputs}
        for image in metadata.get("input_images") or []:
            path = image.get("local_path")
            if path and image.get("capture_mode") != "unavailable":
                verified = contained_path(path, ["data/cache/images", self.client.app_config.media.cache_dir])
                if str(verified) not in seen:
                    inputs.append({"local_path": str(verified), "name": verified.name})
                    seen.add(str(verified))
        return inputs

    def _persist_result(self, request, metadata, payload, *, state="ready"):
        path = self.directory(metadata["session_key"]) / ("result-" + request["id"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        payload["full_result_file"] = str(path)
        self.broker.update(request["id"], state, payload)

    def _queue_result(self, request, ctx, metadata):
        """Serialize completed workers back through the original main chat."""
        if request["id"] in self.result_pending or self.closed:
            return self.delivery_tasks.get(request["id"])
        runtime = self.client._get_runtime()
        self.result_pending.add(request["id"])

        async def deliver():
            review_task = None
            try:
                row = self.broker.record(request["id"])
                payload = json.loads(row["result"])
                if not (payload.get("delivery") or {}).get("approved_by"):
                    from conversation_runtime import EventContext
                    finished = asyncio.get_running_loop().create_future()
                    async def process_review():
                        try:
                            current = self.broker.record(request["id"])
                            if current["state"] != "ready" or not ctx.guard():
                                return
                            result = json.loads(current["result"])
                            detail = result.get("result") or {}
                            command_result = bool(self.broker.command_result_text(request["kind"], result))
                            event = {"type": "task_result", "request_id": request["id"],
                                "command_result": command_result,
                                "task": request["task"], "success": result.get("success", False),
                                "answer": detail.get("answer") or result.get("error", ""), "sources": detail.get("sources", []),
                                "files": detail.get("files", []), "error": result.get("error", ""),
                                "full_result_file": result.get("full_result_file", "")}
                            async with self.client._reply_turn(metadata["session_key"], ctx):
                                raw = await self.client._main_chat(metadata["session_key"], json.dumps(event, ensure_ascii=False),
                                                                   media_context=ctx)
                                from prompt_builder import parse_ai_decision_and_reply
                                decision = parse_ai_decision_and_reply(raw)
                                if decision.should_reply:
                                    persona = self.client.personas.get_active_persona()
                                    spoken = await self.client._send_decision_messages(decision, ctx, persona.name)
                                    if spoken:
                                        self.client._record_console_text(ctx, persona.name, spoken)
                                latest = self.broker.record(request["id"])
                                if latest["state"] == "ready":
                                    payload = json.loads(latest["result"])
                                    payload["main_reviewed"] = True
                                    self.broker.update(request["id"], "ready", payload)
                                    if (payload.get("delivery") or {}).get("approved_by"):
                                        await self.client._deliver_console_result(request["id"], ctx, metadata)
                        except BaseException as error:
                            if not finished.done():
                                finished.set_exception(error)
                            raise
                        finally:
                            if not finished.done():
                                finished.set_result(None)
                    async def review():
                        nonlocal review_task
                        if finished.cancelled():
                            return
                        review_task = asyncio.create_task(process_review())
                        try:
                            await review_task
                        except asyncio.CancelledError:
                            # Cancelling one delivery must not kill the inbox
                            # worker or discard the following user messages.
                            if asyncio.current_task().cancelling():
                                raise
                    runtime.submit_chat(EventContext(ctx.scope, ctx.message_id, ctx.received_at,
                        "result", ctx.owner), review, directed=True)
                    await finished
                    return  # Review and its approved delivery used one reply turn.
                latest = self.broker.record(request["id"])
                if latest["state"] == "ready" and (json.loads(latest["result"]).get("delivery") or {}).get("approved_by"):
                    await self.client._deliver_console_result(request["id"], ctx, metadata)
            except asyncio.CancelledError:
                if review_task and not review_task.done():
                    review_task.cancel()
                    await asyncio.gather(review_task, return_exceptions=True)
                raise
            except Exception:
                logger.exception("主代理结果交付暂不可用，成果保留供重试: %s", request["id"])
            finally:
                self.result_pending.discard(request["id"])
                self.delivery_tasks.pop(request["id"], None)
                if self.broker.record(request["id"])["state"] == "ready":
                    self.retry_after[request["id"]] = time.time() + 30
                else:
                    self.retry_after.pop(request["id"], None)
        task = asyncio.create_task(deliver())
        self.delivery_tasks[request["id"]] = task
        return task

    async def _watch(self):
        while not self.closed:
            try:
                await self.poll()
                await self.schedule_learning()
                await self.schedule_activity_reviews()
                await self.schedule_self_activity()
                self.publish_live_heartbeat()
                if self.live_schedule_task is None or self.live_schedule_task.done():
                    self.live_schedule_task = asyncio.create_task(self.schedule_live_events())
                await self.schedule_live_output()
                await self.schedule_identity_maintenance()
                await self.recall_idle()
                if hasattr(self.client, "sync_system_prompt"):
                    self.client.sync_system_prompt()
                from agent_core.runtime import for_client
                runtime = for_client(self.client)
                if runtime:
                    await runtime.tick()
                from broadcast_commands import tick_client
                await tick_client(self.client)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("控制台后台调度暂不可用，下轮重试")
            await asyncio.sleep(0.5)

    def publish_live_heartbeat(self):
        if not hasattr(self.client, "send_action") or time.monotonic() < self.live_heartbeat_after:
            return
        import psutil
        from osu_telemetry_worker import publish_latest
        self.live_heartbeat_after = time.monotonic() + 3
        directory = self.root.parent / "osu-live"
        directory.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(self.namespace.encode()).hexdigest()[:16] + ".json"
        publish_latest(directory / name, {"namespace":self.namespace,"database":str(self.broker.path),
            "pid":os.getpid(),"created":psutil.Process().create_time(),"heartbeat":time.time()})

    def live_run_current(self, run):
        try:
            control=json.loads((self.root.parent / "osu-live" / "control.json").read_text(encoding="utf8"))
            return control.get("enabled") is True and control.get("stop") is not True and (run == "idle" or control.get("run") == run)
        except (OSError, ValueError, TypeError):
            return False

    async def live_available(self):
        """Require a fresh room response and explicit local live enablement."""
        try:
            control = json.loads((self.root.parent / "osu-live" / "control.json").read_text(encoding="utf8"))
            if control.get("enabled") is not True or control.get("stop") is True or not self.client.app_config.bilibili.enabled:
                return False
            from bilibili_adapter import BilibiliAdapter
            from live_events import ROOM
            info = await asyncio.wait_for(BilibiliAdapter(self.client.app_config.bilibili.credentials_file).room_info(int(ROOM)), timeout=5)
            is_live = info.get("live_status") == 1
            from osu_telemetry_worker import publish_latest
            publish_latest(self.root.parent / "osu-live" / "room-status.json", {"state":"live" if is_live else "closed", "checked_at":time.time()})
            return is_live
        except (OSError, ValueError, TypeError, asyncio.TimeoutError):
            return False
        except Exception:
            logger.warning("直播状态不可确认，保持静默。")
            return False

    async def schedule_live_events(self):
        if self.live_task and not self.live_task.done():
            return
        from live_events import ROOT, claim
        if not hasattr(self.client, "send_action"):
            return
        with self.broker.connect() as db:
            pending = db.execute("SELECT 1 FROM live_events WHERE namespace=? AND state='queued' LIMIT 1", (self.namespace,)).fetchone()
        if not pending:
            control = self.root.parent / "osu-live" / "control.json"
            if not control.exists() or time.monotonic() < self.live_idle_check_after: return
            self.live_idle_check_after = time.monotonic() + 30
            if not await self.live_available(): return
            from live_events import enqueue_idle
            if not enqueue_idle(self.broker, self.namespace): return
        elif not await self.live_available():
            from live_events import cancel_pending
            cancel_pending(self.broker, self.namespace)
            return
        key = self.client._chat_session_key(ROOT)
        generation = session_generation(self.client.ai, key)
        event = claim(self.broker, self.namespace, generation)
        if not event:
            return
        async def process():
            outcome = "unknown"
            try:
                if not await self.live_available() or not self.live_run_current(event["run"]) or not generation_is_current(self.client.ai, key, generation):
                    outcome = "cancelled"
                    return
                prompt = json.dumps({"type":"live_map_analysis" if event["kind"] == "analysis" else "live_idle", "summary":json.loads(event["summary"]),
                    "instruction":"依据事实用自己的身份简短分析；闲时可依据自己的兴趣自然闲聊或静默。仅用 live-say 提交直播台词，或 wait 静默；最后 finish。禁止向 QQ 发送。"}, ensure_ascii=False)
                await self.client._main_chat(key, prompt, idle_event=True, activity_scope="live:"+event["room"], live_event={"event_id":event["id"]})
                outcome = "done"
                if event["kind"] == "idle":
                    from live_events import record_idle_result
                    record_idle_result(self.broker, self.namespace, event["id"])
            except asyncio.CancelledError:
                raise
            except LiveUnavailable:
                outcome = "cancelled"
            except Exception:
                logger.exception("直播主对话事件未确认完成，保留为 unknown。")
            finally:
                with self.broker.connect() as db:
                    db.execute("UPDATE live_events SET state=? WHERE id=? AND state='running'", (outcome,event["id"]))
                    if outcome != "done":
                        db.execute("UPDATE live_outgoing SET state='cancelled' WHERE event_id=? AND state='queued'", (event["id"],))
        self.live_task = asyncio.create_task(process())

    async def schedule_live_output(self):
        if self.live_output_task and not self.live_output_task.done():
            return
        with self.broker.connect() as db:
            pending = db.execute("SELECT 1 FROM live_outgoing o JOIN live_events e ON e.id=o.event_id WHERE e.namespace=? AND o.state='queued' LIMIT 1", (self.namespace,)).fetchone()
        if not pending:
            return
        from live_events import ROOT, ROOM
        from live_playback import OwnedAudio, drain_one
        def current(row):
            return (row['root'] == ROOT and row['room'] == ROOM and self.live_run_current(row['run']) and generation_is_current(
                self.client.ai, self.client._chat_session_key(ROOT), row['generation']))
        async def render(row):
            if self.live_audio is None:
                from runtime_paths import application_home
                self.live_audio = OwnedAudio(application_home(), self.client.app_config.broadcast_voice, self.broker.path)
            return await self.live_audio.render(row)
        async def play(row,path):
            return await self.live_audio.play(row,path)
        self.live_output_task = asyncio.create_task(drain_one(self.broker,self.namespace,self.live_available,current,render,play))

    @staticmethod
    def activity_signature(feedback):
        # Reading results and actionable failures wake the main conversation;
        # unchanged screenshots and repeated error statuses remain quiet.
        result = {}
        for item in feedback:
            repair = item.get("recovery", {})
            result[item["request_id"]] = [item.get("state"), item.get("direction_revision"), item.get("applied_revision"),
                item.get("goal"), item.get("page", {}), [note["id"] for note in item.get("notes", [])], item.get("checkpoint"),
                [repair.get(key) for key in ("revision", "state", "code")]]
        return result

    def activity_feedback(self, session_key, scope, controllers=False, only_ids=None):
        cfg = self.client.app_config.sandbox
        if not cfg.enabled or not (Path(cfg.directory) / "sandbox.sqlite3").is_file():
            return []
        from sandbox_runtime import SandboxRuntime
        from activity_control import snapshots
        runtime = SandboxRuntime(cfg.directory)
        with runtime.connect() as db:
            sessions = db.execute("SELECT request_id FROM sessions WHERE namespace=? AND scope=? "
                "AND (state IN ('running','paused','failed','interrupted') OR (state='completed' AND owner LIKE 'self:%')) "
                "ORDER BY created DESC LIMIT 8", (self.namespace, scope)).fetchall()
        feedback, owners = [], set()
        for row in sessions:
            request = self.broker.record(row["request_id"])
            if not request:
                continue
            original = self.broker.context(request["token"], allow_expired=True)
            if (original.get("session_key") != session_key or not (original.get("is_master") or original.get("self_directed"))
                    or not generation_is_current(self.client.ai, session_key, original.get("generation", 0))):
                continue
            owner = original["sender_id"]
            if owner in owners:
                continue
            owners.add(owner)
            metadata = {**original, "sandbox_config": {**cfg.model_dump(), "directory": str(runtime.root)}}
            for item in snapshots(self.broker, metadata):
                if only_ids is not None and item["request_id"] not in only_ids:
                    continue
                if controllers and item["state"] in ("running", "paused", "failed", "interrupted"):
                    controller = {**metadata, "sandbox_session_id": item["session_id"], "source_text": "",
                        "activity_controller": True, "activity_target_id": item["target_id"],
                        "parent_request_id": item["request_id"], "artifact_roots": [], "output_dir": ""}
                    item["controller_context_file"] = self.broker.issue_context(controller,
                        self.directory(session_key) / "activity_controls")
                    item["cli_steer"] = invocation("sandbox", "steer", context_file=item["controller_context_file"], text="替换为你决定的具体阅读方向")
                    item["cli_skip"] = invocation("sandbox", "skip", context_file=item["controller_context_file"], url="https://example.com/")
                    item["cli_restart"] = invocation("sandbox", "restart", context_file=item["controller_context_file"])
                feedback.append(item)
        return feedback

    async def schedule_activity_reviews(self):
        async with self.activity_review_scan_lock:
            await self._schedule_activity_reviews()

    async def _schedule_activity_reviews(self):
        now = time.monotonic()
        for key, ctx in list(self.contexts.items()):
            if not ctx.guard():
                continue
            pending = self.activity_review_tasks.get(key)
            if pending and not pending.done() or self.locks.get(key, asyncio.Lock()).locked():
                continue
            # Coalesce notes and leave ordinary chat priority, without limiting work.
            if now < self.activity_review_due.get(key, now):
                continue
            feedback = await finish_thread(self.activity_feedback, key, ctx.scope)
            signature = self.activity_signature(feedback)
            state = self.client.ai.get_or_create_session(key).chat_state
            if not signature or signature == state.get("activity_feedback_seen", {}):
                self.activity_review_due[key] = now + 5
                continue
            if key not in self.activity_review_due:
                self.activity_review_due[key] = now + 5
                continue
            self.activity_review_due[key] = now + 30
            generation = session_generation(self.client.ai, key)
            async def review(session_key=key, scope=ctx.scope, expected=generation):
                try:
                    if generation_is_current(self.client.ai, session_key, expected):
                        await self.chat(session_key, json.dumps({"type": "activity_observation"}, ensure_ascii=False),
                            activity_scope=scope, activity_review=True, idle_event=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("阅读反馈未送入主对话，后续继续接收")
            self.activity_review_tasks[key] = asyncio.create_task(review())

    async def schedule_self_activity(self):
        """Quiet personal time is independent of the permission to speak in a group."""
        cfg = self.client.app_config.bot
        enabled = cfg.self_activity_enabled and cfg.autonomous_enabled and not getattr(self.client, "_graceful_stopped", False)
        if not enabled:
            for task in self.self_activity_tasks.values():
                if not task.done():
                    task.cancel()
            if self.self_activity_was_enabled:
                with self.broker.connect() as db:
                    pending = db.execute("SELECT r.id FROM requests r JOIN contexts c ON r.token=c.token "
                        "WHERE r.namespace=? AND r.state IN ('submitted','queued','running') "
                        "AND json_extract(c.payload,'$.self_directed')=1", (self.namespace,)).fetchall()
                for row in pending:
                    self.broker.update(row["id"], "cancel_requested")
            self.self_activity_was_enabled = False
            return
        self.self_activity_was_enabled = True
        from persona_isolation import session_is_active
        now = time.time()
        for key, ctx in list(self.contexts.items()):
            task = self.self_activity_tasks.get(key)
            from conversation_routing import background_allowed
            if not background_allowed(self.client, ctx):
                continue
            if (not ctx.guard() or not session_is_active(self.client, key)
                    or task and not task.done() or self.locks.get(key, asyncio.Lock()).locked()):
                continue
            runtime = self.client._get_runtime()
            if runtime.closed or ctx.scope in runtime.chat_workers or runtime.inboxes.get(ctx.scope):
                continue
            state = self.client.ai.get_or_create_session(key).chat_state
            if state.get("self_activity_wait_count", 0) >= SELF_ACTIVITY_WAIT_LIMIT:
                continue
            last_input = state.get("last_input", state.get("last_event", ctx.received_at or now))
            if now - max(last_input, state.get("last_self_attempt", 0)) < cfg.self_activity_interval_minutes * 60:
                continue
            # Persist before invoking the model, so failure/restart cannot cause
            # a hot retry loop. A real incoming message always takes priority.
            state["last_self_attempt"] = now
            self.client.ai._save_sessions()
            generation = session_generation(self.client.ai, key)
            async def reflect(session_key=key, scope=ctx.scope, expected=generation):
                try:
                    if generation_is_current(self.client.ai, session_key, expected):
                        await self.chat(session_key, json.dumps({"type": "self_activity"}, ensure_ascii=False),
                            activity_scope=scope, self_activity=True, idle_event=True)
                        state = self.client.ai.get_or_create_session(session_key).chat_state
                        if state.get("self_activity_wait_count", 0) >= SELF_ACTIVITY_WAIT_LIMIT:
                            return
                        # A quiet finish must not turn every personal interval
                        # into another wait. Continue one evidence-backed goal.
                        from exploration import ensure_exploration
                        ensure_exploration(self, session_key)
                        await self.poll()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("独立活动本轮结束，下次空闲再决定")
            self.self_activity_tasks[key] = asyncio.create_task(reflect())

    async def schedule_learning(self):
        cfg = self.client.app_config.sandbox
        if not cfg.enabled or not cfg.autonomous_enabled or time.monotonic() < self.sandbox_schedule_after:
            return
        self.sandbox_schedule_after = time.monotonic() + 30
        from sandbox_runtime import SandboxRuntime
        runtime = SandboxRuntime(cfg.directory, max_actions=cfg.max_actions)
        with runtime.connect() as db:
            games = [dict(row) for row in db.execute("SELECT * FROM games WHERE namespace=? AND autoplay=1 AND last_run<?",
                (self.namespace, time.time() - cfg.interval_minutes * 60))]
        for game in games:
            if game["kind"] == "software" and cfg.mode != "background":
                continue
            try:
                metadata = self.broker.context(game["source_token"], allow_expired=True)
                if not metadata.get("is_master") or not generation_is_current(self.client.ai,
                        metadata["session_key"], metadata["generation"]):
                    continue
                from persona_isolation import session_is_active
                if not session_is_active(self.client, metadata["session_key"]):
                    continue
                with runtime.connect() as db:
                    if db.execute("SELECT 1 FROM sessions WHERE target=? AND state='running'", (game["id"],)).fetchone():
                        continue
                    with self.broker.connect() as queue:
                        if queue.execute("SELECT 1 FROM requests WHERE namespace=? AND scope=? AND kind='sandbox_play' "
                                "AND state IN ('submitted','queued','running') AND json_extract(result,'$.target_id')=?",
                                (self.namespace, game["scope"], game["id"])).fetchone():
                            continue
                    db.execute("UPDATE games SET last_run=? WHERE id=?", (time.time(), game["id"]))
                metadata["sandbox_config"] = {**cfg.model_dump(), "directory": str(runtime.root)}
                context_file = self.broker.issue_context(metadata, self.directory(metadata["session_key"]) / "contexts")
                token = json.loads(Path(context_file).read_text(encoding="utf-8"))["token"]
                goal = game["goal"] or ("持续浏览感兴趣的视频和帖子，记录真实观察与感受，直到主人停止。"
                                        if game["kind"] == "software" else "持续游玩已授权游戏，阅读剧情、记录感受，直到主人停止。")
                self.broker._enqueue(token, metadata, "sandbox_play", goal,
                                     {"target_id": game["id"], "quiet": True}, "autoplay:" + game["id"])
            except (ValueError, PermissionError):
                logger.warning("游戏 %s 自主续玩上下文不可用，等待主人重新授权。", game["id"])

    async def schedule_identity_maintenance(self):
        if self.closed or time.monotonic() < self.identity_scan_after:
            return
        self.identity_scan_after = time.monotonic() + 5
        from persona_isolation import identity, session_is_active
        owners = {identity(self.client, preset) for preset in self.client.personas.presets.values()}
        for row in self.identity.pending_sessions(owners):
            key = row["session_key"]
            signature = (row["updated"], tuple(item["archive_id"] for item in
                self.identity.pending(row["owner"], row["scope"])))
            if (not session_is_active(self.client, key) or self.locks.get(key, asyncio.Lock()).locked()
                    or key in self.identity_tasks and not self.identity_tasks[key].done()
                    or self.identity_attempted.get(key) == signature):
                continue
            # One automatic opportunity per new archive in this process.
            # Failure remains durable for the next user turn or restart.
            self.identity_attempted[key] = signature
            async def maintain(key=key, scope=row["scope"]):
                try:
                    await self.chat(key, json.dumps({"type": "memory_maintenance"}),
                        idle_event=True, activity_scope=scope, memory_maintenance=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("通讯录归档整理未完成，保留资料供下一轮继续")
            self.identity_tasks[key] = asyncio.create_task(maintain())

    async def recall_idle(self):
        for key, ctx in list(self.contexts.items()):
            if not ctx.guard():
                continue
            session = self.client.ai.get_or_create_session(key)
            state = session.chat_state
            if (key in self.recalling
                    or time.time() - state.get("last_event", time.time()) < self.client.app_config.bot.idle_recall_minutes * 60
                    or self.locks.get(key, asyncio.Lock()).locked()):
                continue
            from idle_memory import IdleMemory
            try:
                if not IdleMemory(self.directory(key), session.generation).pending():
                    continue
            except (OSError, ValueError):
                continue  # Unreadable progress must not trigger a paid retry loop.
            self.recalling.add(key)
            self.recall_tasks[key] = asyncio.create_task(self._recall(key, session.generation))

    async def _recall(self, key, generation):
        from idle_memory import IdleMemory
        from memory_digest import summarize_records
        memory, snapshot = IdleMemory(self.directory(key), generation), None
        try:
            async with self.locks.setdefault(key, asyncio.Lock()):
                if not generation_is_current(self.client.ai, key, generation):
                    return
                prepared = await finish_thread(memory.begin)
                if prepared is None:
                    return
                snapshot, previous, records = prepared
                if not records:
                    await finish_thread(memory.fail, snapshot, "empty")
                    return
                # A summary has its own bounded invocation; it cannot restart
                # the main persona's conversation or inherit its reply protocol.
                summary_key = "group_summary_idle_" + hashlib.sha256((self.namespace + key).encode()).hexdigest()[:24]
                result = await summarize_records(self.client.ai, summary_key, records, previous=previous,
                    source="main_chat", checkpoint_path=memory.root / "memory-work.json")
                if not generation_is_current(self.client.ai, key, generation):
                    return
                if not result["complete"] or not result["digest"]:
                    await finish_thread(memory.fail, snapshot)
                    return
                session = self.client.ai.get_or_create_session(key)
                await finish_thread(memory.finish, snapshot, result["digest"], session.conversation_id)
                from bot_identity import bind
                binding = bind(self.client, key)
                from structured_memory import timestamp
                times = [record.get("timestamp") for record in records if record.get("timestamp")]
                self.identity.enqueue_archive(binding["identity_owner"], self.contexts[key].scope, key,
                    {"path": str(memory.path), "version": hashlib.sha256(memory.path.read_bytes()).hexdigest(),
                     **result["digest"], "session_start": min(map(timestamp, times)) if times else "",
                     "session_end": max(map(timestamp, times)) if times else ""})
                session.chat_state.update(idle_archive=str(memory.path), last_recall=time.time())
                session.chat_state.pop("recall_digest", None)
                self.client.ai._save_sessions()
                # The digest is durable; keep its organizer for the next idle
                # batch instead of deleting and recreating a backend agent.
        except asyncio.CancelledError:
            if snapshot:
                await finish_thread(memory.fail, snapshot, "interrupted")
            raise
        except Exception:
            if snapshot:
                await finish_thread(memory.fail, snapshot)
            logger.exception("空闲整理已停止，源记录和检查点保留，不循环重试同一批内容")
        finally:
            self.recalling.discard(key)

    async def close(self):
        self.closed = True
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        for task in (self.live_task, self.live_schedule_task, self.live_output_task):
            if task and not task.done(): task.cancel()
        await asyncio.gather(*(task for task in (self.live_task, self.live_schedule_task, self.live_output_task) if task), return_exceptions=True)
        if self.live_audio: await self.live_audio.close()
        await self.agents.close()
        runtime = getattr(self.client, "_actor_runtime", None)
        if runtime:
            await runtime.close()
        from broadcast_host import stop_server as stop_broadcast
        from vtuber_host import stop_server as stop_avatar
        stop_broadcast(str(Path(getattr(self.client, "config_path", "config.yaml")).resolve()))
        if getattr(self.client.app_config, "vtuber", None) is not None:
            stop_avatar(self.client.app_config.vtuber)
        tasks = [*self.recall_tasks.values(), *self.startup_tasks.values(), *self.delivery_tasks.values(),
                 *self.activity_review_tasks.values(), *self.self_activity_tasks.values(), *self.identity_tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
