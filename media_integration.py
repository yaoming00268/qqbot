"""Shared runtime and media hooks without creating storage for ordinary chat."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import asyncio
from dataclasses import replace
from contextlib import asynccontextmanager
from pathlib import Path

from media_models import MessageEnvelope
from media_router import parse_media_intent
from runtime_control import RuntimeControl, generation_is_current, session_generation


class MediaClientMixin:
    def _emit_group_feed(self, group_id, user_id, sender, message_id, content, is_at_bot=False, *, pending=False):
        import json
        if pending:
            content = (str(content or "").strip() + "\n[图片/表情包识别中]").strip()
        print("[FEED_MSG] " + json.dumps({"time": time.strftime("%H:%M:%S"),
            "group_id": str(group_id), "user_id": str(user_id), "sender": sender,
            "message_id": str(message_id or ""), "content": content, "is_at_bot": bool(is_at_bot)},
            ensure_ascii=False), flush=True)

    def _init_media_runtime(self):
        namespace = f"onebot:{self.account_key}" if hasattr(self, "send_action") else f"official:{self.app_config.qq.appid}"
        if getattr(self, "_media_runtime_namespace", None) == namespace:
            return
        if getattr(self, "_runtime", None) is not None or (
                getattr(self, "_main_coordinator", None) is not None and not self._main_coordinator.closed):
            raise RuntimeError("请先停止当前运行时，再切换账号。")
        from persona_isolation import PersonaMemory, PersonaBuffer
        if not isinstance(self.memory_mgr, PersonaMemory):
            self.memory_mgr = PersonaMemory(self, self.memory_mgr)
        else:
            self.memory_mgr.client = self
        if not isinstance(self.context_buffer, PersonaBuffer):
            self.context_buffer = PersonaBuffer(self, self.context_buffer)
        else:
            self.context_buffer.client = self
        if getattr(self, "_control", None) is None:
            self._control = RuntimeControl(namespace)
        else:
            self._control.namespace = namespace
        self._media_service = None
        self._last_chat_replies = {}
        if not hasattr(self, "_runtime"):
            self._runtime = None
        if not hasattr(self, "_text_outbox"):
            self._text_outbox = None
        from passive_participation import PassiveParticipation
        self._participation = PassiveParticipation()
        from proactive_chat import ProactiveChat
        self._proactive_chat = ProactiveChat(self)
        self._image_context_store = None
        self._image_context_seen = {}
        self._main_coordinator = None
        from chat_turns import ChatTurns
        self._chat_turns = ChatTurns()
        self._emotion_history = None
        self._media_runtime_namespace = namespace

    def _get_emotion_history(self):
        from emotion_analysis import EmotionHistory
        if getattr(self, "_emotion_history", None) is None:
            self._emotion_history = EmotionHistory(Path(self.app_config.bot.runtime_database_path).resolve().parent / "emotion_history.sqlite3")
        return self._emotion_history

    def _observe_chat(self, scope, message_id, sender_id, text, received_at=None, *, is_bot=False):
        """Observe raw ingress once, before coalescing, vision or AI participation."""
        received_at = time.time() if received_at is None else received_at
        self._chat_turns.observe(scope, message_id, sender_id, text, received_at)
        if is_bot or str(sender_id) == str(getattr(self, "self_id", "")):
            return
        try:
            from persona_isolation import namespace as account_namespace
            _, kind, target = scope.rsplit(":", 2)
            namespace = account_namespace(self)
            prefix = "onebot_" if hasattr(self, "send_action") else ""
            base = prefix + ("group_" if kind == "group" else "private_" if prefix else "c2c_") + target
            key = self._chat_session_key(base)
            self._get_emotion_history().observe(namespace, scope, key, message_id, sender_id, text, received_at)
        except (OSError, ValueError, sqlite3.Error):
            logging.getLogger("MainChat").exception("今日发言原话保存失败，普通聊天继续；情绪分析可能缺少本条依据")

    def _get_main_coordinator(self):
        if self._main_coordinator is None:
            from main_chat import MainChat
            self._main_coordinator = MainChat(self)
        return self._main_coordinator

    @asynccontextmanager
    async def _reply_turn(self, session_key, ctx=None):
        """Reserve a full reply; reject contexts invalidated while waiting."""
        from antigravity_client import SessionInvalidatedError
        from persona_isolation import session_is_active
        session_key = self._chat_session_key(session_key.removeprefix("__work__:"))
        generation = session_generation(self.ai, session_key)
        console = self._get_main_coordinator()
        if ctx:
            await console.interrupt_background(session_key, foreground=True)
        async with console.replies.turn(session_key):
            if (not generation_is_current(self.ai, session_key, generation)
                    or not session_is_active(self, session_key)):
                raise SessionInvalidatedError("排队期间会话已重置或人设已切换，旧回复已取消。")
            if ctx:
                ctx.check_current()
            yield

    async def _main_chat(self, session_key, prompt, *, media_context=None, image_delivery=None, **options):
        from antigravity_client import ContentBlockedError
        from persona_isolation import persona_scope, session_group
        session_key = self._chat_session_key(session_key)
        generation = session_generation(self.ai, session_key)
        if image_delivery is not None and image_delivery[1]:
            identity, versions = image_delivery
            store = self._get_image_context_store()
            def references():
                return [{"role": "image_fact", "path": fact.artifact_path, "version": updated}
                    for message_id, index, updated in versions
                    for fact in store.get(identity[0], message_id, index) if fact.updated_at == updated]
            files = await asyncio.to_thread(references)
            if files:
                manifest = options.get("memory_manifest") or {}
                options["memory_manifest"] = {**manifest, "files": [*manifest.get("files", []), *files]}
        with persona_scope(self, session_group(session_key), key=session_key):
            try:
                return await self._get_main_coordinator().chat(session_key, prompt, ctx=media_context, **options)
            except ContentBlockedError:
                if generation_is_current(self.ai, session_key, generation) and image_delivery is not None:
                    _, versions = image_delivery
                    state = self.ai.get_or_create_session(session_key).chat_state
                    skipped = state.setdefault("blocked_image_versions", {})
                    skipped.update({f"{message_id}:{index}": updated for message_id, index, updated in versions})
                    state["blocked_image_versions"] = dict(list(skipped.items())[-128:])
                    self.ai._save_sessions()
                raise

    async def _send_decision_messages(self, decision, ctx, persona_name):
        async with self._reply_turn(ctx.ai_session_key, ctx):
            earlier, ctx.cli_sent_texts = ctx.cli_sent_texts, []
            remaining = await self._send_decision_batch(decision, ctx, persona_name) if ctx.cli_messages else ""
            return "\n".join([*earlier, *([remaining] if remaining else [])])

    async def _send_decision_batch(self, decision, ctx, persona_name):
        from prompt_builder import deflect_preaching, strip_action_descriptions
        from messaging import OutboundText, SendUnknown
        from chat_turns import emit_reply
        console = self._get_main_coordinator()
        delivered, queued, sent_count = [], 0, 0
        batches = []
        for item in ctx.cli_messages:
            text = deflect_preaching(item["text"], persona_name)
            text = text.strip() if self.app_config.bot.enable_action_descriptions else strip_action_descriptions(text)
            if text:
                batches.append({**item, "text": text, "command_ids": [item["command_id"]]})
            else:
                console.broker.message_state(item["command_id"], "cancelled")
        if not hasattr(self, "send_action") and batches:
            ctx.check_reply_available()
            # The configured length is a preferred bubble size. Fit the full
            # content into the platform quota, merging only adjacent commands
            # with the same explicit targets.
            slots = 5 - ctx.sequence
            while len(batches) > slots:
                join = next((index for index in range(len(batches) - 1) if
                    (batches[index].get("reply_to"), batches[index].get("mention")) ==
                    (batches[index + 1].get("reply_to"), batches[index + 1].get("mention"))), None)
                if join is None:
                    from media_models import ReplyWindowExpired
                    for item in batches:
                        for identity in item["command_ids"]:
                            console.broker.message_state(identity, "failed", {"error": "本轮平台回复额度不足。"})
                    raise ReplyWindowExpired("本轮平台回复额度不足，请在新消息继续。")
                following = batches.pop(join + 1)
                batches[join]["text"] += "\n\n" + following["text"]
                batches[join]["command_ids"].extend(following["command_ids"])
        # Only immutable CLI commands can reach the transport. Neither a model's
        # final text nor a caller-supplied action/label is an outbound capability.
        for batch_index, item in enumerate(batches):
            ctx.check_current()
            if self._rebatch_reply_if_needed(ctx):
                break
            identities = item["command_ids"]
            if ctx.is_group and ctx.reply_supersedable and self._chat_turns.superseded(ctx.scope, ctx.message_id, ctx.sender_id):
                for identity in identities:
                    console.broker.message_state(identity, "cancelled")
                continue
            text = item["text"]
            if not hasattr(self, "send_action"):
                ctx.check_reply_available()
            if not console.broker.claim_messages(ctx.console_token, identities):
                continue
            reply_to, mention = item.get("reply_to", ""), item.get("mention", "")
            source_id = reply_to or ctx.message_id
            source = self._chat_turns.source(ctx.scope, source_id)
            sender_id = source.get("sender_id", ctx.sender_id)
            try:
                if hasattr(self, "send_action"):
                    outbound = OutboundText(text, mention=mention, quote=reply_to,
                        reply_to=source_id if ctx.is_group else "", reply_sender=sender_id,
                        reply_supersedable=ctx.reply_supersedable and not reply_to, reply_quote=False)
                    outbound.rebatch_check = lambda: self._rebatch_reply_if_needed(ctx)
                    if ctx.proactive_event:
                        receipt = await self.send_action("send_group_msg", {
                            "group_id": int(ctx.conversation_id), "message": outbound.segments()})
                    else:
                        receipt = await (self.send_group_message(ctx.conversation_id, outbound) if ctx.is_group
                                         else self.send_private_message(ctx.conversation_id, outbound))
                else:
                    from messaging import split_text
                    chunks = split_text(text, self.app_config.bot.max_reply_length)
                    slots = 5 - ctx.sequence - (len(batches) - batch_index - 1)
                    if len(chunks) > slots:
                        chunks = chunks[:slots - 1] + ["".join(chunks[slots - 1:])]
                    receipts, submitted_chunks = [], []
                    for index, chunk in enumerate(chunks):
                        ctx.check_current()
                        if self._rebatch_reply_if_needed(ctx):
                            break
                        options = {"message_reference": {"message_id": reply_to, "ignore_get_message_error": True}} if reply_to and index == 0 else {}
                        body = (f"<@{mention}> " if mention and index == 0 else "") + chunk
                        receipts.append(await ctx.send_text(body, msg_seq=ctx.allocate_sequence(), **options))
                        submitted_chunks.append(chunk)
                        if ctx.is_group:
                            self._chat_turns.mark_replied(ctx.scope, source_id)
                            emit_reply(group_id=ctx.conversation_id, sender_id=sender_id, source_message_id=source_id,
                                source_text=source.get("text", ctx.text), reply=chunk, quoted=bool(reply_to and index == 0))
                    receipt = {"status": "ok", "parts": receipts}
                    if len(submitted_chunks) != len(chunks):
                        receipt.update(status="ok" if receipts else "cancelled", interrupted=True,
                                       sent_text="".join(submitted_chunks))
                if isinstance(receipt, dict) and receipt.get("interrupted"):
                    text = receipt["sent_text"]
                state = "accepted"
                if isinstance(receipt, dict):
                    if receipt.get("status") == "queued":
                        data = receipt.get("data") or {}
                        queued += len(data.get("outbox_ids") or [data.get("outbox_id")])
                        state = "queued"
                    elif receipt.get("status") in ("failed", "cancelled") or receipt.get("retcode") not in (None, 0):
                        state = "failed" if receipt.get("status") != "cancelled" else "cancelled"
                    elif receipt.get("status") == "unknown":
                        state = "unknown"
                for identity in identities:
                    console.broker.message_state(identity, state, receipt)
                if state == "accepted":
                    delivered.append(text)
                    sent_count += len(receipt.get("parts") or [receipt]) if isinstance(receipt, dict) else 1
                    self._record_dialogue_reply(ctx, persona_name, text,
                        message_id="bot:" + ":".join(identities), reply_to=source_id)
            except BaseException as error:
                # Once submission began, cancellation or a lost receipt cannot
                # prove that QQ rejected it. Persist uncertainty without replay.
                for identity in identities:
                    console.broker.message_state(identity, "unknown" if isinstance(error, (SendUnknown, asyncio.CancelledError)) else "failed",
                                                 {"error": str(error)})
                raise
        if ctx.ai_session_key and ctx.guard():
            session = self.ai.get_or_create_session(ctx.ai_session_key.removeprefix("__work__:"))
            session.chat_state["last_delivery"] = {"source_message_id": ctx.message_id,
                "state": "queued" if queued else "sent" if delivered else "suppressed",
                "segments_sent": sent_count, "segments_queued": queued}
            self.ai._save_sessions()
        return "\n".join(delivered)

    def _rebatch_reply_if_needed(self, ctx):
        """Stop between sends, preserving any request already at the transport."""
        from conversation_runtime import current_event
        event = current_event.get()
        runtime = getattr(self, "_runtime", None)
        if (runtime is None or event is None or event.scope != ctx.scope
                or event.message_id != ctx.message_id or not event.reply_generating):
            return False
        if event.rebatch_requested:
            return True
        if not runtime.request_rebatch(event):
            return False
        self._get_main_coordinator().broker.seal_messages(ctx.console_token, cancel=True)
        return True

    async def _deliver_console_result(self, request_id, ctx, metadata):
        from console_delivery import deliver_console_result
        async with self._reply_turn(metadata["session_key"], ctx):
            await deliver_console_result(self, request_id, ctx, metadata)

    def _finish_console_work(self, payload):
        if self._runtime and payload.get("runtime_job_id"):
            self._runtime.store.work_state(payload["runtime_job_id"], "completed" if payload.get("success") else "failed")

    def _record_dialogue_reply(self, ctx, persona_name, text, *, message_id, reply_to=""):
        """Persist confirmed speech, including private and partially sent turns."""
        console = getattr(self, "_main_coordinator", None)
        runtime = getattr(self, "_actor_runtime", None)
        if runtime and console and ctx.console_token:
            try:
                binding = console.broker.context(ctx.console_token, allow_expired=True)
                if binding.get("actor_account_id"):
                    runtime.record_sent(binding, ctx, text, message_id)
            except Exception:
                logging.getLogger("MainChat").exception("已确认的回复写入角色记忆失败，发送回执仍保留")
        if self.app_config.bot.memory_enabled and console is not None and ctx.ai_session_key:
            from idle_memory import IdleMemory
            key = self._chat_session_key(ctx.ai_session_key)
            try:
                IdleMemory(console.directory(key), session_generation(self.ai, key)).append([
                    {"message_id": message_id, "author_id": "bot", "author_name": persona_name, "is_bot": True,
                     "content": text, "timestamp": time.time(), "source_message_id": ctx.message_id,
                     "reply_to": reply_to or ctx.message_id, "delivery_state": "accepted"}])
            except (OSError, ValueError):
                # A local archive failure cannot turn a confirmed QQ receipt
                # into a failed send or authorize a replay.
                logging.getLogger("MainChat").exception("已发送的回复写入对话记忆失败，发送状态仍保留")

    def _record_console_text(self, ctx, persona_name, text):
        self._remember_chat_reply(ctx, text)
        if ctx.is_group:
            self.context_buffer.add_message(ctx.conversation_id, "bot", persona_name, text, is_bot=True)
            if self.app_config.bot.memory_enabled:
                self.memory_mgr.record_session_message(ctx.conversation_id, "bot", persona_name, text, is_bot=True)

    def _image_scope(self, target, is_group=True):
        namespace = f"onebot:{self.account_key}" if hasattr(self, "send_action") else f"official:{self.app_config.qq.appid or 'default'}"
        return f"{namespace}:{'group' if is_group else 'private'}:{target}"

    def _get_image_context_store(self):
        if self._image_context_store is None:
            from image_context import ImageContextStore
            root = Path(self.app_config.bot.runtime_database_path).parent
            self._image_context_store = ImageContextStore(root / "image_context.sqlite3")
        return self._image_context_store

    async def _save_image_context(self, target, message_id, images, *, is_group=True,
                                  sender_id="", sender_name="", timestamp=0, query="", strict=False):
        if not message_id or not any(image.get("vision_verified") for image in images):
            return
        import logging
        for image in images:
            image.setdefault("origin_message_id", str(message_id))
        try:
            await self._get_image_context_store().save_images_async(self._image_scope(target, is_group),
                str(message_id), images, sender_id=sender_id, sender_name=sender_name,
                timestamp=timestamp, query=query)
        except (OSError, ValueError) as error:
            logging.getLogger("ImageContext").warning("识图结果保存失败: %s", error)
            if strict:
                raise

    async def _stored_image_reference(self, target, message_id, *, is_group=True):
        if not message_id:
            return None
        store = self._get_image_context_store()
        facts = await asyncio.to_thread(store.get, self._image_scope(target, is_group), str(message_id))
        if not facts:
            return None
        first = facts[0]
        return {"message_id": str(message_id), "group_id": str(target) if is_group else None,
                "private_user_id": None if is_group else str(target), "user_id": first.sender_id,
                "nickname": first.sender_name, "time": first.timestamp,
                "clean_text": first.query, "image_analyses": [fact.to_analysis() for fact in facts]}

    async def _image_context_for_chat(self, session_key, target, query, *, is_group=True, message_id="", current_message_id=""):
        """Relay branch facts to the main conversation on its next actual turn."""
        store = self._get_image_context_store()
        scope = self._image_scope(target, is_group)
        session = self.ai.get_or_create_session(session_key)
        identity = (scope, session_key, str(getattr(session, "conversation_id", "") or ""))
        state = session.chat_state
        known = state.get("image_versions", {}) if state.get("image_cid") == identity[2] else {}
        skipped = state.get("blocked_image_versions", {})
        included = {}
        targets = [message_id] if message_id else []
        if current_message_id and await asyncio.to_thread(store.get, scope, str(current_message_id)):
            targets = list(dict.fromkeys([str(current_message_id), *targets]))
        pieces = [await store.build_context_async(scope, message_id=source, query=query,
            max_chars=3600 // max(1, len(targets)) - 1, known_versions=known,
            excluded_versions=skipped, included_versions=included) for source in targets or [""]]
        context = "\n".join(piece for piece in pieces if piece)
        versions = tuple((message_id, index, updated) for (message_id, index), updated in included.items())
        return context, (identity, versions)

    def _ack_image_context(self, session_key, delivery):
        if delivery is None:
            return
        identity, versions = delivery
        # Bind the acknowledgement to the actual CID, including newly-created chats.
        session = self.ai.get_or_create_session(session_key)
        identity = (identity[0], session_key, str(getattr(session, "conversation_id", "") or ""))
        self._image_context_seen[identity] = versions
        state = session.chat_state
        known = state.get("image_versions", {}) if state.get("image_cid") == identity[2] else {}
        known.update({f"{message_id}:{index}": updated for message_id, index, updated in versions})
        state.update(image_cid=identity[2], image_versions=dict(list(known.items())[-128:]))
        self.ai._save_sessions()

    def _clear_image_context(self, target=None, *, is_group=True):
        root = Path(self.app_config.bot.runtime_database_path).parent
        if self._image_context_store is not None or (root / "image_context.sqlite3").is_file():
            store = self._get_image_context_store()
            if target is None:
                store.clear_namespace(self._image_scope("placeholder").rsplit(":", 2)[0])
            else:
                store.clear_scope(self._image_scope(target, is_group))
        self._image_context_seen.clear()

    def _personas_changed(self):
        """File timestamps alone must not destroy restored conversations."""
        import json
        def signature():
            return json.dumps({"active": self.personas.active_persona_name,
                "presets": {key: value.model_dump() for key, value in self.personas.presets.items()}},
                ensure_ascii=False, sort_keys=True)
        before = getattr(self, "_personas_signature", None) or signature()
        self.personas.check_reload()
        after = signature()
        self._personas_signature = after
        return after != before
    def _get_runtime(self):
        if self._runtime is None:
            from conversation_runtime import ConversationRuntime, RuntimeStore, TextOutbox
            namespace = self._control.namespace if hasattr(self._control, "namespace") else (
                f"onebot:{self.account_key}" if hasattr(self, "send_action") else f"official:{self.app_config.qq.appid}")
            store = RuntimeStore(self.app_config.bot.runtime_database_path, namespace)
            media_cfg = getattr(self.app_config, "media", None)
            max_pending = getattr(media_cfg, "max_pending_jobs", 32) if media_cfg else 32
            conv_limit = getattr(media_cfg, "per_conversation_queue_limit", 3) if media_cfg else 3
            self._runtime = ConversationRuntime(store, passive_backlog=self.app_config.bot.passive_backlog,
                                                 on_work_done=self._archive_work,
                                                 max_pending_jobs=max_pending,
                                                 per_conversation_queue_limit=conv_limit,
                                                 settle_seconds=self.app_config.bot.chat_settle_seconds,
                                                 max_wait_seconds=self.app_config.bot.chat_max_wait_seconds,
                                                 buffer_seconds=self.app_config.bot.chat_buffer_seconds,
                                                 backlog_interrupt_messages=self.app_config.bot.backlog_interrupt_messages)
            if hasattr(self, "send_action"):
                self._text_outbox = TextOutbox(store, self._is_ws_connected, self.send_action,
                                              quote_resolver=self._chat_turns.newer,
                                              on_reply_sent=self._chat_turns.mark_replied,
                                              guard_validator=getattr(self, "_validate_outbox_guard", None))
        else:
            self._runtime.buffer_seconds = self.app_config.bot.chat_buffer_seconds
            self._runtime.settle_seconds = self.app_config.bot.chat_settle_seconds
            self._runtime.max_wait_seconds = self.app_config.bot.chat_max_wait_seconds
            self._runtime.backlog_interrupt_messages = self.app_config.bot.backlog_interrupt_messages
            media_cfg = getattr(self.app_config, "media", None)
            if media_cfg:
                self._runtime.max_pending_jobs = getattr(media_cfg, "max_pending_jobs", self._runtime.max_pending_jobs)
                self._runtime.per_conversation_queue_limit = getattr(media_cfg, "per_conversation_queue_limit", self._runtime.per_conversation_queue_limit)
        return self._runtime

    async def _close_runtime(self):
        if self._main_coordinator:
            await self._main_coordinator.close()
        await self._proactive_chat.close()
        if self._runtime:
            await self._runtime.close()
        if self._main_coordinator:
            from activity_memory import flush_activity_memories
            await flush_activity_memories(self._main_coordinator)
        if self._text_outbox:
            await self._text_outbox.close()
        if self._runtime:
            self._runtime.store.db.close()
            self._runtime = None
        self._text_outbox = None
        task = getattr(self, "_memory_archive_task", None)
        if task and not task.done():
            await task
        self._main_coordinator = None
        self._media_service = None
        self._media_runtime_namespace = None

    async def _archive_work(self, job_id, event, result):
        import json
        import os
        sessions = getattr(self.ai, "_sessions", None)
        if not isinstance(sessions, dict) or not hasattr(self.ai, "archive_session"):
            return
        directory = Path(self.app_config.media.cache_dir).parent / "work_archives" / job_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "result.json"
        payload = {"job_id": job_id, "scope": event.scope, "source_message_id": event.message_id,
                   "message": getattr(result, "message", "任务处理结束"), "data": getattr(result, "data", None),
                   "archived_at": time.time()}
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        # An incomplete/failed task needs its existing conversation and files
        # to continue. Delete server conversations only after completed work.
        if not getattr(result, "success", True):
            return
        # Only archive the unique branch owned by this job. Other work and
        # image recognition may still be using this group's shared keys.
        keys = []
        data = getattr(result, "data", None)
        agent_key = data.get("agent_session_key", "") if isinstance(data, dict) else ""
        if isinstance(agent_key, str) and agent_key.startswith("__work__:task:"):
            keys.extend([agent_key, "__vision__:" + agent_key])
            # A view has a unique suffix. Exact-key cleanup misses its terminal
            # descendants when an earlier cleanup attempt could not reach LS.
            prefix = "__vision__:" + agent_key + ":view:"
            keys.extend(key for key in list(sessions) if key.startswith(prefix))
        for key in keys:
            if key in sessions:
                try:
                    await self.ai.archive_session(key, str(path))
                except Exception:
                    import logging
                    logging.getLogger("ConversationRuntime").exception("后台结果已保存，会话清理失败")

    def _tick_memory_archive(self):
        if not self.app_config.bot.memory_enabled:
            return
        now = time.time()
        if not hasattr(self, "_last_memory_archive"):
            self._last_memory_archive = now
        task = getattr(self, "_memory_archive_task", None)
        if now - self._last_memory_archive >= self.app_config.bot.memory_archive_interval_minutes * 60 and (task is None or task.done()):
            self._last_memory_archive = now
            if self.memory_mgr._session_messages or self.memory_mgr.has_pending():
                self._memory_archive_task = asyncio.create_task(self.memory_mgr.summarize_and_save_session(self.ai, cleanup_chats=False))

    @staticmethod
    def _chat_options():
        from conversation_runtime import current_event
        event = current_event.get()
        return {"timeout": 0} if event and event.lane == "work" else {}

    def _background_context(self, ctx):
        # Work, native searches and vision never hold the group chat lock.
        return replace(ctx, ai_session_key=self._work_session_key(ctx.ai_session_key))

    @staticmethod
    def _work_session_key(key):
        return key if key.startswith("__work__:") else "__work__:" + key

    def _chat_session_key(self, key):
        # A work-lane receipt may carry an image result; its conversational
        # interpretation always belongs to the durable chat, never a branch.
        from persona_isolation import scoped_session
        return scoped_session(self, key)

    async def _enqueue_work(self, ctx, label, callback):
        from conversation_runtime import EventContext
        runtime = self._get_runtime()
        event = EventContext(ctx.scope, ctx.message_id, ctx.received_at, "work", ctx.owner)
        job_id = runtime.submit_work(event, label, callback, ctx.guard)
        # Acknowledgement is truthful: completion prose is emitted by the worker.
        await self._get_media_service().emit(ctx, f"已加入任务队列（{job_id}），完成后会引用这条消息发送结果。")
        return job_id

    @property
    def media(self):
        return self._media_service

    def _get_media_service(self):
        if self._media_service is None:
            from media_jobs import MediaService
            namespace = f"onebot:{self.account_key}" if hasattr(self, "send_action") else f"official:{self.app_config.qq.appid}"
            self._media_service = MediaService(self.app_config.media, namespace=namespace)
        # MediaService owns a namespace-specific copy of the configuration.
        # Delivery controls are live; refreshing them must retain its paths,
        # durable jobs and sender receipts.
        self._media_service.config.delivery_batch_size = self.app_config.media.delivery_batch_size
        self._media_service.config.delivery_max_file_mb = self.app_config.media.delivery_max_file_mb
        return self._media_service

    def _record_official_attachments(self, message, ctx):
        """Keep SDK attachment identity so /process can receive originals."""
        from console_tools.files import safe_file_name
        from urllib.parse import unquote, urlsplit
        files = []
        for item in getattr(message, "attachments", None) or []:
            def field(name, default=""):
                return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)
            url = str(field("url") or "")
            if url.startswith("//"):
                url = "https:" + url
            if not url:
                continue
            try:
                size = max(0, int(field("size", 0) or 0))
            except (TypeError, ValueError):
                size = 0
            files.append({"native_id": str(field("id") or ""), "url": url, "busid": 0, "size": size,
                "name": safe_file_name(field("filename") or field("file_name") or field("name") or
                                        Path(unquote(urlsplit(url).path)).name)})
        if files:
            console = self._get_main_coordinator()
            console.broker.record_files(console.namespace, ctx.scope, ctx.message_id, ctx.sender_id, files)

    def _remember_chat_reply(self, ctx, text):
        self._last_chat_replies[ctx.media_owner] = text
        if len(self._last_chat_replies) > 500:
            self._last_chat_replies.pop(next(iter(self._last_chat_replies)))
        if self.media:
            self.media.remember_reply(ctx, text)

    async def _route_media(self, ctx, intent=None):
        if intent is None:
            intent = parse_media_intent(ctx.text, self.app_config.media.default_image_count)
        if intent is None:
            return False
        from conversation_runtime import current_event
        event = current_event.get()
        if self._runtime and intent.operation in ("status", "cancel"):
            if intent.operation == "cancel":
                cancelled_ids = set()
                if intent.job_id:
                    self._runtime.cancel(job_id=intent.job_id, owner=ctx.owner)
                    cancelled_ids.add(intent.job_id)
                else:
                    jobs = self._runtime.store.jobs(ctx.owner)
                    recent_pending = [j for j in jobs if j.get("state") in ("queued", "running", "ready")]
                    if recent_pending:
                        target_id = recent_pending[0]["id"]
                        self._runtime.cancel(job_id=target_id, owner=ctx.owner)
                        intent = intent.model_copy(update={"job_id": target_id})
                        cancelled_ids.add(target_id)
                    else:
                        self._runtime.cancel(owner=ctx.owner)
                if hasattr(self, "_get_main_coordinator"):
                    coordinator = self._get_main_coordinator()
                    if coordinator and hasattr(coordinator, "broker"):
                        with coordinator.broker.connect() as db:
                            rows = db.execute("SELECT id, result, token, state FROM requests WHERE namespace=?", (coordinator.namespace,)).fetchall()
                            for r in rows:
                                if r["state"] in ("cancelled", "delivered"):
                                    continue
                                r_res = json.loads(r["result"] or "{}")
                                r_rt_id = r_res.get("runtime_job_id")
                                r_media_id = (r_res.get("result") or {}).get("job_id") or r_res.get("job_id")
                                matched = False
                                if cancelled_ids:
                                    if any(cid in (r_rt_id, r_media_id, r["id"]) for cid in cancelled_ids):
                                        matched = True
                                else:
                                    try:
                                        c_meta = coordinator.broker.context(r["token"], allow_expired=True)
                                        if c_meta.get("sender_id") == ctx.sender_id or c_meta.get("session_key") == ctx.ai_session_key:
                                            matched = True
                                    except Exception:
                                        pass
                                if matched:
                                    coordinator.broker.update(r["id"], "cancelled")

            jobs = self._runtime.store.jobs(ctx.owner)
            if jobs:
                names = {"queued": "排队中", "running": "执行中", "completed": "已完成", "failed": "失败",
                         "cancelled": "已取消", "interrupted": "已中断", "ready": "结果待领取", "unknown": "发送状态未知"}
                await self._get_media_service().emit(ctx, "\n".join(
                    f"任务 {j['id']}：{names.get(j['state'], j['state'])}，{j['label']}" for j in jobs[:5]))
                if intent.job_id and any(job["id"] == intent.job_id for job in jobs):
                    result = self._runtime.store.result(intent.job_id)
                    if not result.get("job_id"):
                        return True
                    intent = intent.model_copy(update={"job_id": result["job_id"]})
                # Media status also collects prepared resources using this fresh reply window.
                from media_downloader import MediaError
                try:
                    self.media.store.job(ctx.media_owner, intent.job_id)
                except MediaError:
                    return True
        if event and event.lane == "chat" and intent.operation not in ("status", "cancel", "help"):
            work_ctx = self._background_context(ctx)
            async def run():
                await self._route_media(work_ctx, intent=intent)
                return current_event.get().result
            await self._enqueue_work(ctx, intent.query or ctx.text, run)
            return True
        service = self._get_media_service()
        if ctx.media_owner in self._last_chat_replies:
            service.remember_reply(ctx, self._last_chat_replies[ctx.media_owner])
        ctx.last_reply = service.store.last_reply(ctx.media_owner)
        outcome = {}
        handled = await service.handle(ctx, intent=intent, outcome=outcome)
        if event and event.lane == "work":
            from skills.base import SkillResult
            event.result = SkillResult(bool(outcome.get("success")),
                outcome.get("reply_text", "") if outcome.get("reply_handled") else outcome.get("message", ""),
                action_type="pending" if outcome.get("state") == "ready" else "media", data=outcome,
                reply_handled=bool(outcome.get("reply_handled")))
        if ctx.guard():
            latest = service.store.last_reply(ctx.media_owner)
            if latest:
                self._last_chat_replies[ctx.media_owner] = latest
                if len(self._last_chat_replies) > 500:
                    self._last_chat_replies.pop(next(iter(self._last_chat_replies)))
        return handled

    async def _execute_skill(self, name, params, context):
        from skills.manager import default_skill_manager
        from skills.base import SkillResult
        from conversation_runtime import current_event
        context.ai_client = self.ai
        context.media_service = self._get_media_service()
        event = current_event.get()
        if event and event.lane == "chat" and context.media_context:
            work_ctx = self._background_context(context.media_context)
            work_context = replace(context, media_context=work_ctx, session_key=work_ctx.ai_session_key)
            async def run():
                return await self._execute_skill(name, params, work_context)
            job_id = await self._enqueue_work(context.media_context, params.get("query") or name, run)
            return SkillResult(False, "任务已排队", action_type="queued", data={"job_id": job_id})
        result = await default_skill_manager.execute_skill(name, params, context)
        if (result.success and result.action_type == "text" and not result.reply_handled
                and context.media_context):
            result = await self._deliver_work_text(context.media_context, result.message,
                                                   label=name, data=result.data)
        if (context.media_context and context.media_context.guard()
                and not result.reply_handled):
            text = ""
            if result.action_type in ("text", "error"):
                text = result.message
            elif result.success and result.action_type in ("file", "media"):
                text = "任务完成，结果已发送 (´▽`)"
            if text:
                from media_models import ReplyWindowExpired
                try:
                    result.message = await context.media_service.emit(context.media_context, text)
                except ReplyWindowExpired:
                    # Successful file/media delivery still owns the reply when
                    # the official quota leaves no room for a caption.
                    if not (result.success and result.action_type in ("file", "media")):
                        raise
                    result.message = ""
                result.reply_handled = True
        if result.reply_handled and result.message and context.media_context:
            self._remember_chat_reply(context.media_context, result.message)
        if event and event.lane == "work":
            event.result = result
        return result

    async def _deliver_work_text(self, ctx, text, *, label="任务结果", data=None):
        """Persist and send a command answer as a file, with one short caption."""
        from conversation_runtime import current_event
        from skills.base import SkillContext
        from skills.task_skills import RunTaskSkill
        service = self._get_media_service()
        context = SkillContext(ctx.ai_session_key, ctx.conversation_id, ctx.sender_id,
            is_group=ctx.is_group, app_config=self.app_config, ai_client=self.ai,
            media_context=ctx, media_service=service)
        result = await RunTaskSkill().deliver_text(text, context, task=label, data=data)
        if result.reply_handled and result.message:
            self._remember_chat_reply(ctx, result.message)
        event = current_event.get()
        if event and event.lane == "work":
            event.result = result
        return result

    def _cancel_media(self, ctx):
        if ctx.is_group:
            self._proactive_chat.clear(ctx.conversation_id)
        if self._runtime:
            self._runtime.cancel(scope=ctx.scope)
        if self._text_outbox:
            self._text_outbox.cancel(ctx.scope)
        if self.media:
            self.media.cancel_scope(ctx.media_scope)
        elif Path(self.app_config.media.database_path).is_file():
            from media_store import MediaStore
            MediaStore(self.app_config.media.database_path).clear_scope(ctx.media_scope)
        self._last_chat_replies = {k: v for k, v in self._last_chat_replies.items() if not k.startswith(ctx.media_scope + ":")}

    def _cancel_all_media(self):
        self._proactive_chat.clear()
        if self._runtime:
            self._runtime.cancel()
        if self._text_outbox:
            self._text_outbox.cancel()
        self._last_chat_replies.clear()
        if self.media:
            self.media.cancel_all()
        elif Path(self.app_config.media.database_path).is_file():
            from media_store import MediaStore
            from persona_isolation import namespace as get_namespace
            MediaStore(self.app_config.media.database_path).clear_namespace(get_namespace(self))

    def _make_media_context(self, *, session_key, user_id, conversation_id, message_id="", text="",
                            is_group=False, reply=None, quoted_images=(), received_at=None):
        from persona_isolation import session_is_active
        session_key = self._chat_session_key(session_key)
        is_onebot = hasattr(self, "send_action")
        generation = session_generation(self.ai, session_key)
        from conversation_runtime import current_event
        event = current_event.get()
        received_at = received_at or (event.received_at if event else None) or time.time()
        if is_onebot:
            async def wait_transport():
                while not self._is_ws_connected():
                    if not generation_is_current(self.ai, session_key, generation):
                        from media_downloader import MediaError
                        raise MediaError("会话已重置，待发送资源已取消。")
                    await asyncio.sleep(0.5)
            async def send_text(body):
                from messaging import OutboundText
                body = body if isinstance(body, OutboundText) else OutboundText(str(body))
                if is_group:
                    return await self.send_group_message(conversation_id, body)
                return await self.send_private_message(conversation_id, body)
            account = self.account_key
        else:
            send_text = reply
            account = self.app_config.qq.appid
        def cache_sent(asset, receipt):
            if not receipt.message_id or asset.kind != "image":
                return
            self._recent_messages_cache[receipt.message_id] = {
                "message_id": receipt.message_id, "user_id": str(getattr(self, "self_id", "")), "time": time.time(),
                "group_id": str(conversation_id) if is_group else None,
                "private_user_id": None if is_group else str(conversation_id),
                "nickname": "机器人", "clean_text": asset.title,
                "image_analyses": [{"local_path": asset.local_path, "url": asset.url,
                                    "summary": "已发送图片", "file_url": ""}],
            }
            if len(self._recent_messages_cache) > 120:
                self._recent_messages_cache.pop(next(iter(self._recent_messages_cache)))
        return MessageEnvelope(transport="onebot" if is_onebot else "official", account_id=account,
            conversation_id=str(conversation_id), sender_id=str(user_id or ""), message_id=str(message_id or ""),
            text=text, send_text=send_text, is_group=is_group,
            api=None if is_onebot else self.api, send_action=self.send_action if is_onebot else None,
            max_text_length=self.app_config.bot.max_reply_length, quoted_images=list(quoted_images),
            received_at=received_at,
            cache_sent=cache_sent,
            ai_client=self.ai, ai_session_key=session_key,
            wait_transport=wait_transport if is_onebot and self._runtime else None,
            guard=lambda: generation_is_current(self.ai, session_key, generation) and session_is_active(self, session_key))
