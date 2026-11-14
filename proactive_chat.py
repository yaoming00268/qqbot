"""Idle topics use the existing chat lane and are invalidated by fresh input."""
from __future__ import annotations

from persona_isolation import persona_bound, persona_for

import asyncio
from dataclasses import dataclass
import json
import logging
import re
import time

from conversation_runtime import EventContext
from runtime_control import generation_is_current, session_generation

logger = logging.getLogger("GroupActivity")


@dataclass
class GroupActivity:
    message_id: str
    last_incoming: float
    last_reply: float = 0
    last_attempt: float = 0
    quiet: bool = False
    reply_context: object = None
    pending: object = None
    task: asyncio.Task | None = None


class ProactiveChat:
    def __init__(self, client):
        self.client = client
        self.groups: dict[str, GroupActivity] = {}
        self.tasks: set[asyncio.Task] = set()
        self.closed = False

    def observe(self, group, message_id, *, received_at=None):
        """Called at receipt, including messages coalesced by a busy chat queue."""
        group, message_id = str(group), str(message_id or "")
        state = self.groups.get(group)
        if state and message_id and state.message_id == message_id:
            return state
        now = time.time() if received_at is None else received_at
        if state is None:
            state = self.groups[group] = GroupActivity(message_id, now)
        else:
            if state.task and not state.task.done():
                state.task.cancel()
            state.message_id = message_id
            state.last_incoming = now
            state.pending = None
            state.quiet = False
            state.reply_context = None
        return state

    def note_reply(self, group):
        state = self.groups.get(str(group))
        if state:
            state.last_reply = time.time()

    def note_quiet(self, group, text, *, directed):
        state = self.groups.get(str(group))
        if state and directed and re.fullmatch(
                r"(?:你)?(?:先|暂时)?(?:闭嘴|别吵|安静点?|别说话|别说了|闭麦|退下)[吧啊呀。！!\s]*", str(text or "").strip()):
            state.quiet = True

    def clear(self, group=None):
        targets = list(self.groups) if group is None else [str(group)]
        for target in targets:
            state = self.groups.pop(target, None)
            if state and state.task and not state.task.done():
                state.task.cancel()

    def _enabled(self):
        cfg = self.client.app_config.bot
        return (not self.closed and not getattr(self.client, "_graceful_stopped", False)
                and cfg.autonomous_enabled and cfg.proactive_chat_enabled)

    def _available(self, state):
        client = self.client
        if hasattr(client, "send_action"):
            return client._is_running and client._is_ws_connected()
        if not client.safe_robot or state.reply_context is None:
            return False
        try:
            state.reply_context.check_reply_available()
        except Exception:
            return False
        return True

    def _awake(self, state, now):
        cfg = self.client.app_config.bot
        return cfg.never_sleep or now - state.last_incoming < cfg.idle_sleep_minutes * 60

    def tick(self):
        """Schedule without waiting for the model or blocking runtime controls."""
        if not self._enabled():
            for state in self.groups.values():
                state.pending = None
                if state.task and not state.task.done():
                    state.task.cancel()
            return
        now = time.time()
        client = self.client
        cfg = client.app_config.bot
        for group, state in self.groups.items():
            if (state.quiet or state.pending is not None or not self._awake(state, now)
                    or now - max(state.last_incoming, state.last_reply, state.last_attempt)
                    < cfg.proactive_interval_minutes * 60
                    or not self._available(state)):
                continue
            runtime = client._get_runtime()
            scope = client._image_scope(group)
            if runtime.closed or scope in runtime.chat_workers or runtime.inboxes.get(scope):
                continue
            token = object()
            state.pending = token
            state.last_attempt = now
            event = EventContext(scope, received_at=now, owner=scope + ":bot")
            runtime.submit_chat(event, lambda g=group, s=state, t=token: self._run(g, s, t))

    def _current(self, group, state, token):
        return (self._enabled() and self.groups.get(group) is state and state.pending is token
                and not state.quiet and self._awake(state, time.time()) and self._available(state))

    async def _run(self, group, state, token):
        if not self._current(group, state, token):
            if state.pending is token:
                state.pending = None
            return
        task = asyncio.create_task(self._generate_and_send(group, state, token))
        state.task = task
        self.tasks.add(task)
        try:
            await task
        except asyncio.CancelledError:
            # A new inbound cancels only this idle topic, never its chat worker.
            if asyncio.current_task().cancelling():
                raise
        except Exception:
            logger.exception("群 %s 主动话题未发送，本轮结束", group)
        finally:
            self.tasks.discard(task)
            if state.task is task:
                state.task = None
            if state.pending is token:
                state.pending = None

    @persona_bound("proactive")
    async def _generate_and_send(self, group, state, token):
        from prompt_builder import (compose_system_prompt, deflect_preaching,
                                    parse_ai_decision_and_reply, strip_action_descriptions)
        client = self.client
        cfg = client.app_config.bot
        onebot = hasattr(client, "send_action")
        session_key = client._chat_session_key(("onebot_group_" if onebot else "group_") + group)
        generation = session_generation(client.ai, session_key)
        persona = persona_for(client, group)
        system = compose_system_prompt(base_prompt=persona.prompt, max_reply_length=cfg.max_reply_length,
            enable_action_descriptions=cfg.enable_action_descriptions, bot_names=[persona.name],
            master_name=cfg.get_master_name(group))
        image_context, delivery = await client._image_context_for_chat(session_key, group, "")
        manifest = (await asyncio.to_thread(client.memory_mgr.get_memory_file_manifest, group,
                    history_count=cfg.memory_history_count) if cfg.memory_enabled else None)
        prompt = (
            "【群聊空闲定时事件，不是群友发言】\n"
            "现在有一个主动开口的机会，不必制造话题。想分享真实见闻、延续之前的讨论，"
            "或向主人提出具体想做的事、想玩的游戏、想要的能力时，按自己的语气自然说。"
            "参考 companion_state 和已验证的阅读笔记；已提过或被拒绝的愿望不反复问。"
            "没有值得说的内容，或主人不在、当前不适合打扰，就用本轮 context_file 调用 wait。"
            "不编造经历，不用重复问候、强行提问或陪伴保证凑消息，也不播报后台进度。\n"
            f"{image_context}"
        )
        if not self._current(group, state, token):
            return
        ctx = (client._make_media_context(session_key=session_key, user_id="", conversation_id=group,
            text="有想分享的见闻或愿望时自然开口", is_group=True) if onebot else state.reply_context)
        from dataclasses import replace
        original_guard = ctx.guard
        ctx = replace(ctx, proactive_event=True, guard=lambda: original_guard() and self._current(group, state, token))
        async with client._reply_turn(session_key, ctx):
            raw = await client._main_chat(session_key, prompt, media_context=ctx, system_prompt=system,
                                         memory_manifest=manifest, idle_event=True)
            from persona_isolation import session_is_active
            if not self._current(group, state, token) or not generation_is_current(client.ai, session_key, generation) or not session_is_active(client, session_key):
                return
            client._ack_image_context(session_key, delivery)
            decision = parse_ai_decision_and_reply(raw_response=raw, default_should_reply=False)
            if not decision.should_reply:
                return
            text = await client._send_decision_messages(decision, ctx, persona.name)
            if not text:
                return
            self.note_reply(group)
            client._last_autonomous_reply_time[group] = time.time()
            client.context_buffer.add_message(group, "bot", persona.name, text, is_bot=True)
            if cfg.memory_enabled:
                client.memory_mgr.record_session_message(group, "bot", persona.name, text, is_bot=True)
            print("[FEED_ACTION] " + json.dumps({"time": time.strftime("%H:%M:%S"), "group_id": group,
                "action": "none", "reason": "空闲主动话题", "reply": text}, ensure_ascii=False), flush=True)

    async def close(self):
        self.closed = True
        self.clear()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
