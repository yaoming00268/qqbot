# -*- coding: utf-8 -*-
"""
OneBot v11 Client for Local QQNT Account (NapCatQQ / LiteLoaderQQNT / LLOneBot).
Enables operating the user's personal QQ account (小号) as the AI Bot,
sharing the exact same Gemini 3.8 engine, character personas, memory compression,
and autonomous group chatting logic.
"""

from __future__ import annotations

from persona_isolation import persona_bound, persona_for
import asyncio
import json
import logging
import os
import random
import re
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Set, Tuple

import websockets
from websockets.exceptions import ConnectionClosed

from antigravity_client import AntigravityClient
from config import AppConfig
from group_memory import GroupMemoryManager, GroupContextBuffer
from persona_manager import PersonaManager
from prompt_builder import (
    compose_system_prompt,
    compose_user_prompt,
    compose_autonomous_decision_prompt,
    parse_ai_decision_and_reply,
    check_safety_policy_risk,
    strip_action_descriptions,
    deflect_preaching,
    _extract_json_candidate,
    AIDecisionResult
)
from image_handler import has_qq_images, image_source_label, process_multimodal_message, strip_multimodal_tags
from messaging import (OutboundText, SendError, SendUnknown, split_text, is_master_user,
                       MESSAGE_ACTIONS, check_onebot_response)
from media_integration import MediaClientMixin
from runtime_control import generation_is_current, session_generation
from antigravity_client import AgentServiceError, SessionInvalidatedError

logger = logging.getLogger("OneBotClient")
logger.setLevel(logging.INFO)


def clean_cq_mentions(
    text: str,
    self_id: str = "",
    segments: Optional[list] = None,
    member_name_getter: Optional[Callable[[str], str]] = None,
    bot_names: Optional[List[str]] = None
) -> Tuple[str, bool, List[Dict[str, str]]]:
    """
    智能解析并清洗 CQ 码与 segments 中的 @ 标签：
    1. 判断是否 @ 了机器人自身 (根据 self_id 或机器人昵称/人设名)；
    2. 对提及的【其他群友】，保留并转换为可读的「@成员昵称」，绝不无脑删除；
    3. 提取所有被提及的第三方人员列表，供大模型识别交互对象；
    4. 清除指向机器人自身的 @ 前缀与回复引用 CQ 码。
    返回: (clean_text, is_at_bot, mentioned_users)
    """
    is_at_bot = False
    mentioned_users: List[Dict[str, str]] = []
    text = text or ""

    # 1. 结构化 segments 预提取
    if segments and isinstance(segments, list):
        for seg in segments:
            if isinstance(seg, dict) and seg.get("type") == "at":
                data = seg.get("data", {})
                target_qq = str(data.get("qq", "")).strip()
                name = str(data.get("name", "")).strip()
                if self_id and target_qq == str(self_id):
                    is_at_bot = True
                elif target_qq == "all":
                    if not any(u["user_id"] == "all" for u in mentioned_users):
                        mentioned_users.append({"user_id": "all", "nickname": "全体成员"})
                elif target_qq:
                    if not name and member_name_getter:
                        name = member_name_getter(target_qq)
                    if not name:
                        name = f"群友_{target_qq[-4:] if len(target_qq) >= 4 else target_qq}"
                    if not any(u["user_id"] == target_qq for u in mentioned_users):
                        mentioned_users.append({"user_id": target_qq, "nickname": name})

    # 2. 正则解析 CQ 码 [CQ:at,qq=...,name=...]
    def _replace_at(match):
        nonlocal is_at_bot
        body = match.group(1)
        qq_m = re.search(r"\bqq=([^,\]]+)", body)
        name_m = re.search(r"\bname=([^,\]]+)", body)
        if not qq_m:
            return ""
        target_qq = qq_m.group(1).strip()
        name = name_m.group(1).strip() if name_m else ""

        if self_id and target_qq == str(self_id):
            is_at_bot = True
            return ""  # 移除呼叫自身的 @ 标签

        if target_qq == "all":
            if not any(u["user_id"] == "all" for u in mentioned_users):
                mentioned_users.append({"user_id": "all", "nickname": "全体成员"})
            return "@全体成员 "

        if not name and member_name_getter:
            name = member_name_getter(target_qq)
        if not name:
            name = f"群友_{target_qq[-4:] if len(target_qq) >= 4 else target_qq}"

        if not any(u["user_id"] == target_qq for u in mentioned_users):
            mentioned_users.append({"user_id": target_qq, "nickname": name})

        return f"@{name} "

    # 替换 [CQ:at,...]
    cleaned = re.sub(r"\[CQ:at,([^\]]+)\]", _replace_at, text)

    # 剔除回复引用 CQ 码
    cleaned = re.sub(r"\[CQ:reply,id=[^\]]+\]", "", cleaned).strip()

    # 3. 检查明文中的 @ 机器人昵称 (如 @雪村ふぶき 或 @ヨンミン)
    if bot_names:
        for bname in bot_names:
            if not bname:
                continue
            bname_esc = re.escape(bname)
            # 开头呼叫机器人
            if re.search(rf"^\s*@{bname_esc}\b", cleaned):
                is_at_bot = True
                cleaned = re.sub(rf"^\s*@{bname_esc}\s*", "", cleaned)
            elif f"@{bname}" in cleaned:
                is_at_bot = True

    return cleaned.strip(), is_at_bot, mentioned_users


class OneBotClient(MediaClientMixin):
    """
    基于 OneBot v11 正向 WebSocket 的客户端。
    支持连接到本地 QQNT 客户端扩展 (NapCat / LLOneBot / Lagrange / GoCQ)。
    """

    def __init__(
        self,
        config: AppConfig,
        antigravity_client: AntigravityClient,
        persona_manager: Optional[PersonaManager] = None,
        memory_manager: Optional[GroupMemoryManager] = None
    ):
        self.app_config = config
        self.ai = antigravity_client
        self.personas = persona_manager or PersonaManager()
        self.start_time = time.time()

        # 群聊专属记忆库与上下文滑动窗口
        self.memory_mgr = memory_manager or GroupMemoryManager(
            storage_dir="data/memories",
            max_entries=config.bot.memory_max_entries
        )
        self.context_buffer = GroupContextBuffer(max_buffer_size=50)

        # 群成员昵称缓存
        self._group_members_cache: Dict[str, Dict[str, str]] = {}

        # 账号配置
        self.account_key = self.app_config.onebot.active_account
        self.account_config = self.app_config.onebot.get_active_account()
        if self.ai and hasattr(self.ai, "bind_account"):
            self.ai.bind_account(self.account_key)

        # 运行状态
        self.self_id: str = ""
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._is_running: bool = False
        self._echo_counter: int = 1
        self._pending_echoes: Dict[str, asyncio.Future] = {}
        self._echo_terminal_status: Dict[str, str] = {}

        # 冷却、去重与交互轮次计数
        self._last_autonomous_reply_time: Dict[str, float] = {}
        self._processed_msg_ids = deque(maxlen=2000)
        self._interaction_turn_counts: Dict[str, int] = {}
        self._recent_messages_cache: Dict[str, dict] = {}
        self._event_tasks = set()
        self._main_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._graceful_stopped = False
        self._identity_confirmed: bool = False
        self._handshake_event_buffer: list = []
        self._init_media_runtime()

        self.sync_system_prompt()

    def _cache_member_name(self, group_id: str, user_id: str, name: str) -> None:
        """记录群成员昵称缓存"""
        if not group_id or not user_id or not name:
            return
        if group_id not in self._group_members_cache:
            self._group_members_cache[group_id] = {}
        self._group_members_cache[group_id][user_id] = name

    def get_cached_member_name(self, group_id: str, user_id: str) -> str:
        """从内存缓存、群记忆库中获取群成员昵称，兜底返回 默认昵称"""
        from bot_identity import store_for, scope_for
        from persona_isolation import identity, persona_for
        contact = store_for(self).get(identity(self, persona_for(self, group_id)), scope_for(self, group_id), str(user_id))
        if contact.get("preferred_name") and not contact.get("deleted"):
            return contact["preferred_name"]
        if group_id and group_id in self._group_members_cache:
            name = self._group_members_cache[group_id].get(user_id)
            if name:
                return name
        if group_id and self.app_config.bot.memory_enabled:
            mem = self.memory_mgr.get_group_memory(group_id)
            p = mem.participants.get(user_id)
            if p and p.nickname:
                return p.nickname
        if contact.get("platform_name") and not contact.get("deleted"):
            return contact["platform_name"]
        return f"群友_{user_id[-4:] if len(user_id) >= 4 else user_id}"

    async def get_group_member_info(self, group_id: int | str, user_id: int | str, no_cache: bool = False) -> Optional[dict]:
        """获取群成员名片与昵称信息"""
        try:
            resp = await self.send_action("get_group_member_info", {
                "group_id": int(group_id),
                "user_id": int(user_id),
                "no_cache": no_cache
            })
            return resp.get("data")
        except Exception:
            return None

    def switch_account(self, account_key: str) -> bool:
        """切换激活的 QQ 小号"""
        if account_key == self.account_key:
            return True
        if self._runtime is not None or (self._main_coordinator is not None and not self._main_coordinator.closed):
            raise RuntimeError("请先停止机器人，再切换账号。")
        if self.app_config.onebot.switch_account(account_key):
            self.account_key = account_key
            self.account_config = self.app_config.onebot.get_active_account()
            if self.ai and hasattr(self.ai, "bind_account"):
                self.ai.bind_account(account_key)
            self._init_media_runtime()
            logger.info(f"🔄 [OneBot] 已切换当前账号为: {self.account_config.name} ({self.account_config.ws_url})")
            return True
        return False

    def sync_system_prompt(self) -> None:
        """根据当前人设预设与配置动态刷新全局 System Prompt"""
        # 实时检测 config.yaml 变更（主人设置、专属称谓等热更新）
        try:
            cfg_path = getattr(self, "config_path", "config.yaml")
            if os.path.exists(cfg_path):
                mtime = os.path.getmtime(cfg_path)
                if not hasattr(self, "_config_mtime"):
                    self._config_mtime = mtime
                elif mtime > self._config_mtime:
                    self._config_mtime = mtime
                    from config import AppConfig
                    fresh_cfg = AppConfig.load(cfg_path)
                    self.app_config.bot = fresh_cfg.bot
                    self.app_config.agent = fresh_cfg.agent
                    logger.info("⚡ [OneBot] 检测到 config.yaml 变更，已热更新主人与群专属配置！")
        except Exception as e:
            logger.debug(f"热重载配置异常: {e}")

        # 检查群专属人设或主人配置指纹变更
        current_settings = getattr(self.app_config.bot, "group_settings", {}) or {}
        current_fps = {
            str(gid): (
                getattr(grp, "persona", "") or "",
                getattr(grp, "master_name", "") or "",
                getattr(grp, "master_title", "") or "",
                getattr(grp, "master_id", "") or "",
            )
            for gid, grp in current_settings.items()
        }
        if not hasattr(self, "_group_persona_fingerprints"):
            self._group_persona_fingerprints = dict(current_fps)
        else:
            all_gids = set(self._group_persona_fingerprints.keys()) | set(current_fps.keys())
            for gid in all_gids:
                old_fp = self._group_persona_fingerprints.get(gid, ("", "", "", ""))
                new_fp = current_fps.get(gid, ("", "", "", ""))
                if old_fp[1:] != new_fp[1:]:
                    skey = self._chat_session_key(f"onebot_group_{gid}")
                    self.ai.reset_session(skey)
                    self.ai.reset_session(f"__work__:{skey}")
                    self._cancel_media(self._make_media_context(
                        session_key=skey,
                        user_id="",
                        conversation_id=gid,
                        is_group=True
                    ))
                    logger.info(f"🔄 [OneBot] 群 {gid} 独立人设/主人配置变更({old_fp[0]} -> {new_fp[0]})，已失效并重置该群会话。")
            self._group_persona_fingerprints = dict(current_fps)

        if self._personas_changed():
            logger.info("人设配置已更新，下一条消息增量载入；保留会话与后台任务。")
        active_persona = self.personas.get_active_persona()
        composed = compose_system_prompt(
            base_prompt=active_persona.prompt,
            max_reply_length=self.app_config.bot.max_reply_length,
            enable_action_descriptions=self.app_config.bot.enable_action_descriptions
        )
        self.ai.system_prompt = composed

    def _is_ws_connected(self) -> bool:
        """安全判断 WebSocket 是否处于连接状态，兼容 websockets v10~v14+"""
        if not self.ws:
            return False
        if hasattr(self.ws, "closed"):
            return not bool(self.ws.closed)
        if hasattr(self.ws, "state"):
            return getattr(self.ws.state, "name", "") == "OPEN"
        return True

    async def send_action(self, action: str, params: dict, timeout: Optional[float] = None, *, terminal_status: str = "") -> dict:
        """向 OneBot WebSocket 发送 JSON-RPC 指令并等待响应"""
        if not self._is_ws_connected():
            from messaging import SendUnavailable
            raise SendUnavailable("OneBot WebSocket 尚未连接")

        echo = f"ob_{self._echo_counter}_{int(time.time() * 1000)}"
        self._echo_counter += 1

        if timeout is None:
            timeout = self.app_config.onebot.message_send_timeout_seconds + 5 if action in MESSAGE_ACTIONS else 10
        if action in MESSAGE_ACTIONS:
            # NapCat accepts milliseconds here. Its listener must finish before
            # our RPC timer; otherwise the caller loses a valid late receipt.
            budget = max(.001, timeout - 5 if timeout > 5 else timeout * .8)
            params = {**params, "timeout": min(float(params.get("timeout") or budget * 1000), budget * 1000)}
        payload = {
            "action": action,
            "params": params,
            "echo": echo
        }

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_echoes[echo] = fut
        if terminal_status:
            self._echo_terminal_status[echo] = terminal_status

        try:
            try:
                async with asyncio.timeout(timeout):
                    await self.ws.send(json.dumps(payload, ensure_ascii=False))
                    resp = await fut
            except TimeoutError:
                raise SendUnknown("接口响应超时，发送状态未知，请核对聊天记录后再试。") from None
            except (ConnectionClosed, OSError):
                raise SendUnknown("连接已断开，未确认的发送状态未知，请核对聊天记录。") from None
            return check_onebot_response(action, resp)
        finally:
            self._pending_echoes.pop(echo, None)
            self._echo_terminal_status.pop(echo, None)

    async def send_group_message(self, group_id: int | str, message: str) -> dict:
        """发送群聊消息"""
        return await self._send_text("send_group_msg", "group_id", group_id, message)

    async def send_private_message(self, user_id: int | str, message: str) -> dict:
        """发送私聊消息"""
        return await self._send_text("send_private_msg", "user_id", user_id, message)

    async def _send_text(self, action, id_key, target, message):
        key = self._chat_session_key(f"onebot_{'group' if id_key == 'group_id' else 'private'}_{target}")
        async with self._reply_turn(key):
            return await self._send_text_parts(action, id_key, target, message)

    async def _send_text_parts(self, action, id_key, target, message):
        text = message if isinstance(message, OutboundText) else OutboundText(str(message))
        from conversation_runtime import current_event
        event = current_event.get()
        response = None
        receipts, outbox_ids, submitted_chunks = [], [], []
        # Every chunk belongs to the same original account/persona/generation.
        # A reset must never give remaining old text a fresh send capability.
        from persona_isolation import session_is_active, identity, effective_persona
        account_key = self.account_key
        scope = f"onebot:{account_key}:{'group' if id_key == 'group_id' else 'private'}:{target}"
        key = self._chat_session_key(f"onebot_{'group' if id_key == 'group_id' else 'private'}_{target}")
        generation = session_generation(self.ai, key)
        persona = effective_persona(self, str(target) if id_key == 'group_id' else "")
        persona_id = identity(self, persona)
        account_id = str(getattr(self, "self_id", "") or (self.account_config.account_id if self.account_config else ""))
        has_source = id_key == "group_id" and bool(text.reply_to)
        def current_reply():
            return (self.account_key == account_key and generation_is_current(self.ai, key, generation)
                    and session_is_active(self, key) and
                    (not has_source or not text.reply_supersedable or
                     not self._chat_turns.superseded(scope, text.reply_to, text.reply_sender)))
        for index, chunk in enumerate(split_text(text.body, self.app_config.bot.max_reply_length)):
            rebatch_check = getattr(text, "rebatch_check", None)
            if rebatch_check and rebatch_check():
                return {"status": "queued" if outbox_ids else "ok" if receipts else "cancelled",
                        "parts": receipts, "interrupted": True, "sent_text": "".join(submitted_chunks),
                        "data": {"outbox_ids": outbox_ids}}
            if not current_reply():
                return {"status": "cancelled", "parts": receipts,
                        "data": {"reason": "原回复上下文已失效", "outbox_ids": outbox_ids}}
            params = {id_key: int(target), "message": text.segments(chunk, index == 0)}
            auto_quote = False  # quote and @ are explicit CLI metadata
            if has_source and text.reply_supersedable and self._chat_turns.superseded(scope, text.reply_to, text.reply_sender):
                logger.info("来源 %s 已有同轮补充，取消尚未发送的正文。", text.reply_to)
                return {"status": "cancelled", "data": {"reason": "发言人已补充消息"}}
            reply_meta = None
            if has_source:
                source = self._chat_turns.source(scope, text.reply_to)
                reply_meta = {"group_id": target, "sender_id": text.reply_sender,
                    "source_message_id": text.reply_to, "source_text": source.get("text", ""), "reply": chunk,
                    "segment_index": getattr(text, "segment_index", 1), "segment_count": getattr(text, "segment_count", 1)}
            if self._text_outbox:
                guard_meta = {
                    "account_id": account_id,
                    "session_key": key,
                    "generation": generation,
                    "persona_id": persona_id,
                    "scope": scope,
                    "source_message_id": text.reply_to or (event.message_id if event else ""),
                    "sender_id": str(text.reply_sender or ""),
                    "reply_supersedable": bool(has_source and text.reply_supersedable),
                }
                response = await self._text_outbox.enqueue(scope, action, params,
                    source_message_id=text.reply_to or (event.message_id if event else ""), received_at=event.received_at if event else 0,
                    quote_after=None, quote_on_newer=auto_quote, reply_meta=reply_meta,
                    explicit_quote=bool(text.quote) and index == 0,
                    guard=current_reply,
                    guard_meta=guard_meta)
            else:
                quoted = bool(text.quote) and index == 0
                response = await self.send_action(action, params)
                if reply_meta:
                    self._chat_turns.mark_replied(scope, text.reply_to)
                    from chat_turns import emit_reply
                    emit_reply(**reply_meta, quoted=quoted)
                    response = {**response, "_reply_quoted": quoted}
            receipts.append(response)
            submitted_chunks.append(chunk)
            if isinstance(response, dict):
                data = response.get("data") or {}
                outbox_ids.extend(data.get("outbox_ids") or ([data["outbox_id"]] if data.get("outbox_id") else []))
                if response.get("status") in ("failed", "cancelled", "unknown") or response.get("retcode") not in (None, 0):
                    return {**response, "parts": receipts, "data": {**data, "outbox_ids": outbox_ids}}
        if outbox_ids:
            return {"status": "queued", "data": {"outbox_id": outbox_ids[0], "outbox_ids": outbox_ids}, "parts": receipts}
        if len(receipts) > 1 and isinstance(response, dict):
            return {**response, "parts": receipts}
        return response

    def _bot_names(self, group_id=""):
        persona_name = self.app_config.bot.get_persona_name(str(group_id)) or self.personas.active_persona_name
        persona = self.personas.get_preset(persona_name) or self.personas.get_active_persona()
        return list(dict.fromkeys(name for name in
            [self.app_config.bot.bot_name, self.account_config.name, persona.name, *persona.aliases] if name))

    def _validate_outbox_guard(self, guard_meta, row, metadata) -> bool:
        if not guard_meta or not isinstance(guard_meta, dict):
            return False
        target_account = str(guard_meta.get("account_id") or "").strip()
        current_account = str(getattr(self, "self_id", "") or (self.account_config.account_id if self.account_config else "")).strip()
        if target_account and target_account != current_account:
            logger.info("出站消息绑定的账号 %s 与当前账号 %s 不一致，已取消发送。", target_account, current_account)
            return False
        session_key = guard_meta.get("session_key", "")
        generation = guard_meta.get("generation")
        if session_key and generation is not None:
            if not generation_is_current(self.ai, session_key, generation):
                logger.info("出站消息会话代际已失效 (%s gen=%s)，已取消发送。", session_key, generation)
                return False
        from persona_isolation import session_is_active, identity, effective_persona
        if session_key and not session_is_active(self, session_key):
            logger.info("出站消息会话人设已不再活跃 (%s)，已取消发送。", session_key)
            return False
        persona_id = guard_meta.get("persona_id")
        if persona_id:
            scope = guard_meta.get("scope", "")
            target_group = scope.split(":")[-1] if ":group:" in scope else ""
            current_p = effective_persona(self, target_group)
            if identity(self, current_p) != persona_id:
                logger.info("出站消息人设身份已变更 (%s)，已取消发送。", persona_id)
                return False
        if guard_meta.get("reply_supersedable"):
            source_id = guard_meta.get("source_message_id")
            sender_id = guard_meta.get("sender_id")
            scope = guard_meta.get("scope", "")
            if source_id and self._chat_turns.superseded(scope, source_id, sender_id):
                logger.info("出站消息来源 %s 已被后续发言取代，已取消发送。", source_id)
                return False
        return True

    def _queue_event(self, data, *, _reference_resolved=False, _received_at=None):
        """Keep the websocket reader free to process receipts and new messages."""
        from conversation_runtime import EventContext
        if data.get("self_id") and not self.self_id:
            incoming_sid = str(data["self_id"]).strip()
            configured_uid = str(getattr(self.account_config, "account_id", "") or "").strip()
            if configured_uid and incoming_sid != configured_uid:
                logger.warning("忽略与配置不匹配的事件 self_id: %s (预期: %s)", incoming_sid, configured_uid)
            elif not configured_uid:
                self.self_id = incoming_sid
        data = self._prepare_file_event(data)
        if data.get("post_type") not in ("message", "notice"):
            return
        is_group = bool(data.get("group_id"))
        target = str(data.get("group_id") if is_group else data.get("user_id", ""))
        if not target or str(data.get("user_id", "")) == self.self_id:
            return
        if data.get("message_id") and str(data["message_id"]) in self._processed_msg_ids:
            return
        if data.get("post_type") == "notice":
            if not (data.get("notice_type") == "poke" or
                    data.get("notice_type") == "notify" and data.get("sub_type") == "poke"):
                return
            if not self.self_id or str(data.get("target_id", "")) != self.self_id:
                return
        segments = data.get("message") if isinstance(data.get("message"), list) else None
        raw = data.get("raw_message") or data.get("message") or ""
        clean, at_bot, _ = clean_cq_mentions(raw if isinstance(raw, str) else "", self.self_id,
            segments=segments, bot_names=self._bot_names(target if is_group else ""))
        sender = data.get("sender") or {}
        name = sender.get("card") or sender.get("nickname") or ""
        directed = (not is_group or at_bot or data.get("post_type") == "notice" or
                    clean.startswith(("/", "／")) or
                    self.app_config.bot.is_master(target, str(data.get("user_id", "")), name))
        scope = f"onebot:{self.account_key}:{'group' if is_group else 'private'}:{target}"
        event = EventContext(scope, str(data.get("message_id", "")), _received_at or time.time(),
                             owner=scope + ":" + str(data.get("user_id", "")), text=clean, sender_name=name)
        if data.get("post_type") == "message":
            self._observe_chat(scope, event.message_id, data.get("user_id", ""), clean, event.received_at)
        if is_group and data.get("post_type") == "message":
            self._proactive_chat.observe(target, event.message_id, received_at=event.received_at)
        from persona_isolation import effective_persona
        event.persona = effective_persona(self, str(target) if is_group else "").model_copy(deep=True)
        runtime = self._get_runtime()
        from media_router import parse_media_intent, is_work_request, is_priority_control
        intent = parse_media_intent(clean, self.app_config.media.default_image_count) if directed else None
        has_images = has_qq_images(raw, segments)
        if is_group and has_images:
            self._emit_group_feed(target, data.get("user_id", ""), name or "群友", event.message_id,
                                  clean, at_bot, pending=True)
        reply_id = ""
        for seg in (segments or []):
            if isinstance(seg, dict) and seg.get("type") == "reply":
                reply_id = str(seg.get("data", {}).get("id", ""))
                break
        if not reply_id and isinstance(raw, str) and "[CQ:reply," in raw:
            m_rep = re.search(r"\[CQ:reply,id=([^,\]]+)\]", raw)
            if m_rep:
                reply_id = m_rep.group(1)
        has_reference = bool(reply_id) or any(seg.get("type") == "reply" for seg in (segments or []) if isinstance(seg, dict)) or "[CQ:reply," in str(raw)
        cached_quoted = self._recent_messages_cache.get(reply_id) if reply_id else None
        has_quoted_images = bool(
            cached_quoted
            and (
                cached_quoted.get("image_analyses")
                or has_qq_images(cached_quoted.get("raw_message"), (cached_quoted.get("image_message") or {}).get("message"))
            )
        )
        is_image_inquiry = bool(
            has_reference and re.search(r"(?:(?<!意|企|蓝|试)图(?:片|像|文)?|照片|相片|插画|壁纸|截图|表情包?|image|pic|picture|photo|sticker)", clean, re.I)
        )
        if (directed and reply_id and not cached_quoted and not _reference_resolved
                and not has_images and not is_image_inquiry and not is_priority_control(clean)):
            self._track_task(asyncio.create_task(self._resolve_and_queue_reference(data, reply_id, event.received_at)))
            return
        unknown_reference = has_reference and not cached_quoted and not _reference_resolved
        if directed and is_priority_control(clean):
            async def run_priority():
                event.lane = "chat"
                from conversation_runtime import current_event
                token = current_event.set(event)
                try:
                    await self._dispatch_event(data)
                finally:
                    current_event.reset(token)
            self._track_task(asyncio.create_task(run_priority()))
            return
        if (is_group and runtime.buffer_seconds > 0 and not is_priority_control(clean)
                and not (directed and is_work_request(clean))):
            async def record_fragment():
                if has_images or has_quoted_images or is_image_inquiry or unknown_reference:
                    await self._handle_group_message(data, _batch_record_only=True)
                else:
                    self._record_passive_event(data, clean, name)
            runtime.submit_chat(event, lambda: self._dispatch_event(data), directed=directed,
                                record_only=record_fragment, coalesce=True,
                                batchable=not clean.startswith(("/", "／")))
        elif ((directed or has_images and self.app_config.bot.autonomous_enabled) and not is_priority_control(clean)
                and (has_images or unknown_reference or has_quoted_images or is_image_inquiry)):
            try:
                job_id = runtime.submit_work(event, clean or "查看 QQ 图片", lambda: self._dispatch_event(data))
                logger.info("消息 %s 已进入后台任务队列 %s", event.message_id, job_id)
            except SendError as se:
                logger.warning("消息 %s 进入后台任务队列失败: %s", event.message_id, se)
        else:
            runtime.submit_chat(event, lambda: self._dispatch_event(data), directed=directed,
                record_only=(lambda: self._record_passive_event(data, clean, name)) if is_group else None,
                batchable=data.get("post_type") == "message" and not clean.startswith(("/", "／")) and not is_work_request(clean),
                coalesce=is_group and data.get("post_type") == "message" and not is_priority_control(clean)
                         and not (directed and is_work_request(clean)))

    async def _resolve_and_queue_reference(self, data, reference, received_at):
        """Fetch metadata independently, then queue only actual image work."""
        group = str(data.get("group_id") or "")
        peer = str(data.get("user_id") or "")
        session_key = self._chat_session_key(f"onebot_group_{group}" if group else f"onebot_private_{peer}")
        generation = session_generation(self.ai, session_key)
        info = {"message_id": reference, "group_id": group or None,
                "private_user_id": None if group else peer, "image_analyses": [], "clean_text": ""}
        stored = await self._stored_image_reference(group or peer, reference, is_group=bool(group))
        if stored:
            self._recent_messages_cache[reference] = stored
            self._queue_event(data, _reference_resolved=True, _received_at=received_at)
            return
        try:
            response = await self.send_action("get_msg", {"message_id": int(reference) if reference.lstrip("-").isdigit() else reference}, timeout=3)
            source = dict(response.get("data") or {})
            if group:
                if source.get("message_type") == "private" or source.get("group_id") is not None and str(source["group_id"]) != group:
                    raise ValueError("Referenced message belongs to another conversation")
                source["group_id"] = group
            elif (source.get("group_id") or source.get("message_type") == "group"
                  or str(source.get("user_id") or "") != peer):
                raise ValueError("Referenced private message belongs to another peer")
            source["message_id"] = reference
            raw = source.get("raw_message") or ""
            segments = source.get("message") if isinstance(source.get("message"), list) else None
            clean, _, _ = clean_cq_mentions(raw, self.self_id, segments=segments)
            sender = source.get("sender") or {}
            info.update(user_id=str(source.get("user_id") or ""), nickname=sender.get("card") or sender.get("nickname") or "群友",
                        clean_text=clean, raw_message=raw, image_message=source, time=source.get("time") or received_at)
            if has_qq_images(raw, segments):
                info["image_analyses"] = [{"summary": "引用图片待识别"}]
        except Exception as error:
            logger.debug("引用消息元数据暂不可用: %s", error)
        if not generation_is_current(self.ai, session_key, generation):
            return
        self._recent_messages_cache[reference] = info
        if len(self._recent_messages_cache) > 120:
            self._recent_messages_cache.pop(next(iter(self._recent_messages_cache)))
        self._queue_event(data, _reference_resolved=True, _received_at=received_at)

    def _record_passive_event(self, data, clean, name):
        """Coalescing reply opportunities must not erase the group transcript."""
        group_id, user_id = str(data.get("group_id", "")), str(data.get("user_id", ""))
        message_id = str(data.get("message_id", ""))
        if message_id and message_id in self._processed_msg_ids:
            return
        if message_id:
            self._processed_msg_ids.append(message_id)
        self._cache_member_name(group_id, user_id, name)
        from persona_isolation import persona_scope
        from conversation_runtime import current_event
        event = current_event.get()
        with persona_scope(self, group_id, persona=getattr(event, "persona", None)):
            self.context_buffer.add_message(group_id, user_id, name, clean, is_bot=False)
            if self.app_config.bot.memory_enabled:
                self.memory_mgr.record_session_message(group_id, user_id, name, clean, is_bot=False,
                                                       timestamp=data.get("time"))

        print("[FEED_MSG] " + json.dumps({"time": time.strftime("%H:%M:%S"), "group_id": group_id,
            "sender": name, "user_id": user_id, "message_id": message_id, "content": clean,
            "is_at_bot": False}, ensure_ascii=False), flush=True)

    def _track_task(self, task):
        self._event_tasks.add(task)
        task.add_done_callback(self._event_done)

    def _event_done(self, task):
        self._event_tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.error("OneBot 事件处理失败", exc_info=task.exception())

    def _fail_pending(self):
        for future in self._pending_echoes.values():
            if not future.done():
                future.set_exception(SendUnknown("连接已断开，未确认的发送状态未知。"))

    async def start(self) -> None:
        """启动 OneBot 客户端连接并维持长连接"""
        self._is_running = True
        self._main_task = asyncio.current_task()
        self._stop_event.clear()
        watcher = asyncio.create_task(self._control.watch(self))
        self._track_task(watcher)
        active_acc = self.app_config.onebot.get_active_account()
        ws_url = active_acc.ws_url or self.app_config.onebot.ws_url or "ws://127.0.0.1:3001"
        headers = {}
        token = active_acc.access_token or self.app_config.onebot.access_token
        if token:
            headers["Authorization"] = f"Bearer {token}"

        logger.info(f"🚀 [OneBot] 正在连接本地 QQNT 账号【{active_acc.name}】(NapCat / OneBot v11) 网关: {ws_url} ...")

        try:
            while self._is_running:
                try:
                    connect_kwargs = {
                        "ping_interval": 20,
                        "ping_timeout": 10,
                        "max_size": 10 * 1024 * 1024
                    }
                    if headers:
                        # 兼容 websockets 新旧版本 headers 参数
                        import inspect
                        sig = inspect.signature(websockets.connect)
                        if "additional_headers" in sig.parameters:
                            connect_kwargs["additional_headers"] = headers
                        else:
                            connect_kwargs["extra_headers"] = headers

                    async with websockets.connect(ws_url, **connect_kwargs) as ws:
                        self.ws = ws
                        self._identity_confirmed = False
                        self._handshake_event_buffer = []
                        logger.info("🎉 [OneBot] 成功连接至本地 QQNT WebSocket 服务，正在验证账号身份握手...")

                        # 异步获取当前登录账号信息（在消息循环启动后执行，由 echo 接收回执，避免死锁等待）
                        self._track_task(asyncio.create_task(self._fetch_login_info()))

                        async for raw_msg in ws:
                            if not self._is_running:
                                break
                            try:
                                data = json.loads(raw_msg)
                                if data.get("echo"):
                                    await self._dispatch_event(data)
                                elif not self._identity_confirmed:
                                    self._handshake_event_buffer.append(data)
                                else:
                                    self._queue_event(data)
                            except Exception as e:
                                logger.error(f"[OneBot] 解析事件异常: {e}")

                except (ConnectionClosed, ConnectionRefusedError, OSError) as ce:
                    self._fail_pending()
                    if not self._is_running:
                        break
                    logger.warning(f"⚠️ [OneBot] 连接断开或无法连接 ({ce})，{self.app_config.onebot.reconnect_interval} 秒后重试...")
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=self.app_config.onebot.reconnect_interval)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self._fail_pending()
                    if not self._is_running:
                        break
                    logger.error(f"❌ [OneBot] 发生未预期网络异常: {e}，5 秒后重试...", exc_info=True)
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=5)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
        except asyncio.CancelledError:
            pass
        finally:
            self._is_running = False
            self.ws = None
            watcher.cancel()
            if self.media:
                self.media.cancel_all()
            try:
                await self.graceful_stop()
            except Exception as me:
                logger.warning(f"OneBot 关闭清理异常: {me}")

    def stop(self) -> None:
        """停止客户端运行（支持跨线程安全调用）"""
        self._is_running = False
        self._fail_pending()
        main_task = getattr(self, "_main_task", None)
        loop = main_task.get_loop() if (main_task and hasattr(main_task, "get_loop")) else None
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None

        if hasattr(self, "_stop_event") and self._stop_event:
            if loop and loop.is_running() and curr_loop != loop:
                loop.call_soon_threadsafe(self._stop_event.set)
            else:
                self._stop_event.set()

        if main_task and not main_task.done():
            if curr_loop == loop:
                try:
                    if asyncio.current_task() != main_task:
                        main_task.cancel()
                except RuntimeError:
                    main_task.cancel()
            elif loop and loop.is_running():
                loop.call_soon_threadsafe(main_task.cancel)
            else:
                main_task.cancel()

    async def graceful_stop(self) -> None:
        """优雅停止客户端并由大模型归档整理群记忆"""
        task = getattr(self, "_graceful_stop_task", None)
        if task is None or task.done():
            try:
                self._graceful_stop_task = asyncio.current_task()
            except RuntimeError:
                pass

        if getattr(self, "_graceful_stopped", False):
            if task and not task.done():
                try:
                    current = asyncio.current_task()
                except RuntimeError:
                    current = None
                if task is not current:
                    await task
            return
        self._graceful_stopped = True
        self.stop()
        await self._close_runtime()
        for task_item in list(self._event_tasks):
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if task_item is not current:
                task_item.cancel()
        if self.media:
            self.media.cancel_all()
        if self._is_ws_connected():
            try:
                await self.ws.close()
            except Exception:
                pass
        if hasattr(self, "memory_mgr") and hasattr(self, "ai") and self.app_config.bot.memory_enabled:
            try:
                logger.info("🛑 [OneBot] 客户端正在关闭，大模型开始整理本次会话群记忆与群人物刻写...")
                await self.memory_mgr.summarize_and_save_session(self.ai)
                self.memory_mgr.close_run()
            except Exception as me:
                logger.warning(f"优雅停止时整理群记忆异常: {me}")

    async def _fetch_login_info(self) -> None:
        """异步拉取当前登录 QQ 账号的昵称与号段信息并严格校验身份绑定"""
        try:
            await asyncio.sleep(0.2)
            info_resp = await self.send_action("get_login_info", {})
            data = info_resp.get("data", {}) if isinstance(info_resp, dict) else {}
            if "user_id" in data:
                gateway_uid = str(data["user_id"]).strip()
                configured_uid = str(getattr(self.account_config, "account_id", "") or "").strip()
                if configured_uid and gateway_uid != configured_uid:
                    logger.critical(
                        f"❌ [OneBot] 网关账号不匹配！配置要求 QQ: 【{configured_uid}】，实际网关返回: 【{gateway_uid}】。"
                        "为防止误发消息与状态串用，已拒绝绑定并中断连接！"
                    )
                    self._identity_confirmed = False
                    if self.ws:
                        await self.ws.close()
                    return
                self.self_id = gateway_uid
                if not configured_uid:
                    logger.info(f"ℹ️ [OneBot] 首次绑定网关账号 QQ: {self.self_id}")
                    self.account_config.account_id = self.self_id
            else:
                logger.error("❌ [OneBot] 获取登录信息失败，未返回有效 user_id！")
                self._identity_confirmed = False
                if self.ws:
                    await self.ws.close()
                return

            self._identity_confirmed = True
            # The account-key namespace is stable across gateway reconnects.
            # Initialization is idempotent: live coordinators and jobs survive.
            self._init_media_runtime()
            self._get_runtime()
            self._text_outbox.wake()
            self._get_main_coordinator().prepare_startup()

            # 释放握手期间缓存的事件
            for buffered in self._handshake_event_buffer:
                self._queue_event(buffered)
            self._handshake_event_buffer.clear()

            nickname = data.get("nickname", "个人小号")
            active_persona = self.personas.get_active_persona()
            logger.info("=" * 60)
            logger.info("🎉 [OneBot] 成功连接并登录本地 QQNT (NapCat) 服务！")
            logger.info(f"👤 当前登录账号: 【{nickname}】 (QQ: {self.self_id})")
            logger.info(f"🎭 当前激活人设: 【{active_persona.name}】")
            logger.info("🤖 运行模式: 本地 QQNT 个人小号 (OneBot v11) 全量消息监听")
            logger.info("⚡ 自主回复: " + ("已开启 (AI智能决策)" if self.app_config.bot.autonomous_enabled else "仅响应@与主人"))
            sessions = getattr(self.ai, "_sessions", {})
            if isinstance(sessions, dict):
                resumed = sum(bool(session.conversation_id) for key, session in sessions.items()
                              if key.startswith(("onebot_group_", "onebot_private_")))
                logger.info("已恢复 %s 个主聊天会话；停止归档不会清除这些会话 ID。", resumed)
            if self.app_config.bot.autonomous_enabled:
                logger.info("群聊新消息自动唤醒分析，无需 @；由 AI 结合上下文决定接话，连续聊天不受概率和冷却截断。")
            logger.info(f"💬 机器人「{active_persona.name}」已就绪，正在后台实时监听 QQ 消息中...")
            logger.info("👉 提示: 请在 QQ 中向该账号发送私聊，或在群聊中发消息进行互动！")
            logger.info("=" * 60)
        except Exception as e:
            logger.debug(f"异步获取登录信息略过: {e}")

    async def _dispatch_event(self, data: dict) -> None:
        """事件分发中心"""
        # 从任意事件中提取自身账号 self_id（仅在未配置或已确认匹配时）
        if "self_id" in data and not self.self_id:
            incoming_sid = str(data["self_id"]).strip()
            configured_uid = str(getattr(self.account_config, "account_id", "") or "").strip()
            if not configured_uid:
                self.self_id = incoming_sid
                logger.info(f"✅ [OneBot] 检测到当前登录账号 QQ: {self.self_id}")
            elif incoming_sid == configured_uid:
                self.self_id = incoming_sid
                logger.info(f"✅ [OneBot] 检测到当前登录账号 QQ: {self.self_id}")

        # 1. 响应 RPC Echo
        echo = data.get("echo")
        if echo and echo in self._pending_echoes:
            fut = self._pending_echoes[echo]
            terminal = self._echo_terminal_status.get(echo)
            if terminal and data.get("status") == "ok" and (data.get("data") or {}).get("status") != terminal:
                return  # Streaming progress does not confirm completion.
            if not fut.done():
                fut.set_result(data)
            return

        data = self._prepare_file_event(data)
        post_type = data.get("post_type")
        if post_type == "message":
            msg_type = data.get("message_type")
            if msg_type == "group":
                await self._handle_group_message(data)
            elif msg_type == "private":
                await self._handle_private_message(data)
        elif post_type == "notice":
            await self._handle_notice_event(data)

    def _prepare_file_event(self, data):
        """Persist upload notices and private file segments before chat routing."""
        if data.get("_console_files_recorded") or str(data.get("user_id") or "") == self.self_id:
            return data
        from console_tools.files import incoming_files, attachment_id
        files = incoming_files(data)
        if not files:
            return data
        group = str(data.get("group_id") or "")
        sender = str(data.get("user_id") or "")
        target = group or sender
        if not target or not sender:
            return data
        console = self._get_main_coordinator()
        scope = f"onebot:{self.account_key}:{'group' if group else 'private'}:{target}"
        message_id = str(data.get("message_id") or "file_" + attachment_id(console.namespace, scope, files[0]))
        ids = console.broker.record_files(console.namespace, scope, message_id, sender, files)
        text = data.get("raw_message") or data.get("message")
        if isinstance(text, list):
            text = "".join(str((s.get("data") or {}).get("text") or "") for s in text
                           if isinstance(s, dict) and s.get("type") == "text")
        text = re.sub(r"\[CQ:(?:file|video),[^\]]*\]", "", str(text or "")).strip()
        text = (text + "\n" if text else "") + "收到文件：" + "、".join(item["name"] for item in files)
        return {**data, "post_type": "message", "message_type": "group" if group else "private",
                "message_id": message_id, "raw_message": text,
                "_console_files_recorded": True, "_console_file_ids": ids}

    def _defer_direct_image_event(self, data):
        """Direct handler callers must use the same background route as websocket events."""
        from conversation_runtime import current_event
        event = current_event.get()
        if event is not None and (event.lane == "work" or self.app_config.bot.chat_buffer_seconds > 0):
            return False
        group = str(data.get("group_id") or "")
        raw = data.get("raw_message") or ""
        segments = data.get("message") if isinstance(data.get("message"), list) else None
        clean, at_bot, _ = clean_cq_mentions(raw, self.self_id, segments=segments,
                                            bot_names=self._bot_names(group))
        sender = data.get("sender") or {}
        directed = not group or at_bot or self.app_config.bot.is_master(
            group, str(data.get("user_id") or ""), sender.get("card") or sender.get("nickname") or "")
        from media_router import is_priority_control
        if not (directed or has_qq_images(raw, segments) and self.app_config.bot.autonomous_enabled) or is_priority_control(clean):
            return False
        reference = next((str((s.get("data") or {}).get("id") or "") for s in segments or []
                          if isinstance(s, dict) and s.get("type") == "reply"), "")
        if not reference:
            match = re.search(r"\[CQ:reply,id=([^,\]]+)\]", raw)
            reference = match.group(1) if match else ""
        cached = self._recent_messages_cache.get(reference) or {}
        if has_qq_images(raw, segments) or cached.get("image_analyses") or (reference and event is None):
            queued = dict(data)
            queued.setdefault("post_type", "message")
            queued.setdefault("message_type", "group" if group else "private")
            self._queue_event(queued)
            return True
        return False

    async def _process_qq_images(self, data, *, content, segments, directed=True):
        """QQBot prepares real files before any model is asked to identify them."""
        capture = None
        has_images = has_qq_images(content, segments)
        if directed and has_images and self.app_config.bot.qq_window_capture:
            from qq_window_capture import QQCaptureService
            if not hasattr(self, "_qq_capture"):
                self._qq_capture = QQCaptureService(self.ai)
            group = str(data.get("group_id", "") or "")
            target = group or str(data.get("user_id", ""))
            name = ""
            if group:
                if not hasattr(self, "_group_names"):
                    self._group_names = {}
                if group not in self._group_names:
                    try:
                        response = await self.send_action("get_group_info", {"group_id": int(group)}, timeout=3)
                        self._group_names[group] = str((response.get("data") or {}).get("group_name", ""))
                    except Exception:
                        pass
                name = self._group_names.get(group, "")
            sender = data.get("sender") or {}
            request = {"scope": f"onebot:{self.account_key}:{'group' if group else 'private'}:{target}",
                "account_id": str(self.self_id),
                "conversation_id": target, "conversation_name": name or (sender.get("nickname", "") if not group else ""),
                "message_id": str(data.get("message_id", "")), "sender_id": str(data.get("user_id", "")),
                "sender_name": sender.get("card") or sender.get("nickname") or "",
                "timestamp": data.get("time", time.time()),
                "time_display": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data.get("time") or time.time())),
                "text": re.sub(r"\[CQ:[^\]]*\]", "", str(content))[:200]}
            async def capture(item, index=0):
                return await self._qq_capture.capture_message(request, item, index=index)
        from qq_image_source import QQImageSourceResolver
        resolver = QQImageSourceResolver(self.send_action, data) if directed and has_images else None
        async def on_recognized(image, index):
            group = str(data.get("group_id") or "")
            sender = data.get("sender") or {}
            await self._save_image_context(group or str(data.get("user_id") or ""), data.get("message_id"), [image],
                is_group=bool(group), sender_id=str(data.get("user_id") or ""),
                sender_name=sender.get("card") or sender.get("nickname") or "", timestamp=data.get("time") or time.time(),
                query=strip_multimodal_tags(str(content)), strict=True)
        enriched, images = await process_multimodal_message(content=content, segments=segments, capture=capture,
            require_capture=self.app_config.bot.qq_window_capture,
            fallback_download=directed,
            allow_download=directed, ai_client=self.ai if directed else None,
            resolve_original=resolver.resolve if resolver else None, on_recognized=on_recognized,
            vision_session_key=f"__work__:incoming:{self.account_key}:{data.get('group_id') or data.get('user_id')}:{data.get('message_id', '')}")
        group = str(data.get("group_id") or "")
        sender = data.get("sender") or {}
        await self._save_image_context(group or str(data.get("user_id") or ""), data.get("message_id"), images,
            is_group=bool(group), sender_id=str(data.get("user_id") or ""),
            sender_name=sender.get("card") or sender.get("nickname") or "", timestamp=data.get("time") or time.time(),
            query=strip_multimodal_tags(str(content)))
        return enriched, images

    async def _quoted_qq_images(self, info, data, *, group_id="", user_id=""):
        """Refresh previously cached originals through QQ before a quoted reply."""
        images = list(info.get("image_analyses", []))
        if not images:
            return images
        if all(image.get("vision_verified") and image.get("visual_description") for image in images):
            return images
        if not self.app_config.bot.qq_window_capture and all(image.get("vision_verified") for image in images):
            return images
        from pathlib import Path
        scope = f"onebot:{self.account_key}:{'group' if group_id else 'private'}:{group_id or user_id}"
        if all(image.get("capture_mode") in ("qq_chat_screenshot", "qq_image_viewer_screenshot")
               and image.get("capture_provenance", {}).get("scope") == scope
               and Path(image.get("local_path") or "").is_file() for image in images):
            return images
        original = dict(info.get("image_message") or data)
        original["message_id"] = info.get("message_id") or original.get("message_id", "")
        if group_id:
            original["group_id"] = group_id
        else:
            original.pop("group_id", None)
            original["user_id"] = user_id
        original.setdefault("user_id", info.get("user_id", ""))
        original.setdefault("sender", {"nickname": info.get("nickname", "")})
        original.setdefault("time", info.get("time", time.time()))
        segments = original.get("message") if isinstance(original.get("message"), list) else None
        content = original.get("raw_message") or info.get("raw_message") or ""
        if not has_qq_images(content, segments):
            segments = [{"type": "mface" if image.get("is_sticker") else "image",
                         "data": {"subType": "1" if image.get("is_sticker") else "0",
                                  "file": image.get("source_file") or image.get("local_path") or "",
                                  "url": image.get("source_url") or image.get("url") or ""}}
                        for image in images]
        _, captured = await self._process_qq_images(original, content=content if isinstance(content, str) else "",
                                                   segments=segments)
        info["image_analyses"] = captured
        return captured

    async def _handle_notice_event(self, data: dict) -> None:
        """处理 OneBot v11 通知类事件 (群戳一戳/拍一拍/捏一捏等)"""
        notice_type = str(data.get("notice_type", "")).lower()
        sub_type = str(data.get("sub_type", "")).lower()

        if notice_type == "poke" or (notice_type == "notify" and sub_type == "poke"):
            await self._handle_poke_event(data)

    async def _handle_poke_event(self, data: dict) -> None:
        """处理戳一戳 / 拍一拍 / 捏一捏互动事件"""
        self._control.poll(self)
        self.sync_system_prompt()
        group_id = str(data.get("group_id", "") or "")
        user_id = str(data.get("user_id", "") or data.get("operator_id", "") or data.get("sender_id", "") or "")
        target_id = str(data.get("target_id", "") or "")
        raw_info = str(data.get("raw_info", "") or "")
        action = str(data.get("action", "") or "戳了戳")
        suffix = str(data.get("suffix", "") or "")

        # 仅响应针对机器人自身的互动事件（若 target_id 存在且不是机器人自身，忽略）
        if not self.self_id or not target_id or target_id != str(self.self_id):
            return

        # 过滤自身发出的事件
        if self.self_id and user_id == str(self.self_id):
            return

        poker_name = self.get_cached_member_name(group_id, user_id)
        if not poker_name or poker_name.startswith("群友_"):
            # 异步尝试通过 API 获取最新名片
            info = await self.get_group_member_info(group_id, user_id)
            if isinstance(info, dict):
                poker_name = info.get("card") or info.get("nickname") or poker_name
                self._cache_member_name(group_id, user_id, poker_name)

        # 提取互动行为描述
        action_desc = "戳了戳你"
        combined_text = f"{action} {suffix} {raw_info}"
        if "头" in combined_text or "摸" in combined_text or "拍" in combined_text:
            action_desc = "拍了拍你的头"
        elif "捏" in combined_text or "脸" in combined_text:
            action_desc = "捏了捏你的脸"
        elif "尾巴" in combined_text:
            action_desc = "碰了碰你的尾巴"
        elif "揉" in combined_text:
            action_desc = "揉了揉你"
        elif "抱" in combined_text:
            action_desc = "抱了抱你"
        elif action:
            action_desc = f"{action}了你"

        time_now = time.strftime("%H:%M:%S")
        logger.info(f"👉 [OneBot] 收到互动事件: [群 {group_id or '私聊'}] [{poker_name}({user_id})] {action_desc}！")

        # 向上层/GUI 广播互动事件
        feed_msg = {
            "time": time_now,
            "group_id": group_id,
            "sender": poker_name,
            "user_id": user_id,
            "message_id": f"poke_{int(time.time()*1000)}",
            "content": f"[{action_desc}]",
            "is_at_bot": True
        }
        print(f"[FEED_MSG] {json.dumps(feed_msg, ensure_ascii=False)}", flush=True)

        active_persona = self.personas.get_active_persona()
        session_key = self._chat_session_key(f"onebot_group_{group_id}" if group_id else f"onebot_private_{user_id}")
        generation = session_generation(self.ai, session_key)

        # Notices have no QQ message ID. Keep reactions on the same main
        # conversation and require the same explicit CLI send as ordinary chat.
        ctx = self._make_media_context(session_key=session_key, user_id=user_id,
            conversation_id=group_id or user_id, text=f"[{action_desc}]", is_group=bool(group_id))
        ctx.reply_supersedable = False
        poke_prompt = json.dumps({"type": "interaction", "sender_id": user_id,
            "sender_name": poker_name, "action": action_desc,
            "instruction": "结合角色与聊天作简短反应，需要说话直接执行本轮 reply_command；无须回应则 wait。这个通知没有可引用的消息 ID。"}, ensure_ascii=False)
        try:
            async with self._reply_turn(session_key, ctx):
                raw_reply = await self._main_chat(session_key, poke_prompt, media_context=ctx)
                decision = parse_ai_decision_and_reply(raw_reply)
                if not decision.should_reply or not generation_is_current(self.ai, session_key, generation):
                    return
                reply_text = await self._send_decision_messages(decision, ctx, active_persona.name)
                if not reply_text:
                    return
                self._record_console_text(ctx, active_persona.name, reply_text)
        except SessionInvalidatedError:
            return
        except Exception as error:
            logger.debug("互动事件未发送回复：%s", error)
            return

        logger.info(f"🤖 [OneBot] 响应互动事件 [{action_desc}]: {reply_text}")

        # 记录记忆
        if not generation_is_current(self.ai, session_key, generation):
            return
        if self.app_config.bot.memory_enabled and group_id:
            self.memory_mgr.record_interaction(
                group_openid=group_id,
                user_openid=user_id,
                user_msg=f"[{action_desc}]",
                bot_reply=reply_text,
                author_name=poker_name
            )

        feed_act = {
            "time": time.strftime("%H:%M:%S"),
            "group_id": group_id,
            "action": "poke_reply",
            "reply": reply_text,
            "target": user_id,
            "reason": f"响应互动: {action_desc}"
        }
        print(f"[FEED_ACTION] {json.dumps(feed_act, ensure_ascii=False)}", flush=True)

    @persona_bound("group")
    async def _handle_group_message(self, data: dict, _batch_record_only=False) -> None:
        """处理群聊消息"""
        data = self._prepare_file_event(data)
        self._control.poll(self)
        self.sync_system_prompt()
        group_id = str(data.get("group_id", ""))
        user_id = str(data.get("user_id", ""))
        message_id = str(data.get("message_id", ""))
        raw_message = data.get("raw_message", "") or data.get("message", "")
        message_segments = data.get("message") if isinstance(data.get("message"), list) else None

        # 过滤自身发出的消息
        if self.self_id and user_id == self.self_id:
            return

        if self._defer_direct_image_event(data):
            return

        # 去重
        if message_id and message_id in self._processed_msg_ids:
            return
        if message_id:
            self._processed_msg_ids.append(message_id)

        from conversation_runtime import current_event
        if current_event.get() is None:
            self._proactive_chat.observe(group_id, message_id)

        # 记录说话人名片与昵称缓存
        sender = data.get("sender", {})
        nickname = sender.get("card") or sender.get("nickname") or f"群友_{user_id[-4:] if len(user_id) >= 4 else user_id}"
        self._cache_member_name(group_id, user_id, nickname)
        raw_clean, raw_at_bot, _ = clean_cq_mentions(raw_message if isinstance(raw_message, str) else "", self.self_id,
            segments=message_segments, bot_names=self._bot_names(group_id))
        active_turn = current_event.get()
        self._observe_chat(self._image_scope(group_id), message_id, user_id, raw_clean,
                           active_turn.received_at if active_turn else time.time())
        if active_turn and active_turn.lane == "chat" and active_turn.merged and active_turn.directed and not active_turn.batch:
            raw_at_bot = True
        image_directed = (raw_at_bot or raw_clean.startswith(("/", "／")) or
            self.app_config.bot.is_master(group_id, user_id, nickname) or self.app_config.bot.autonomous_enabled)
        from media_router import is_priority_control
        image_directed = image_directed and not is_priority_control(raw_clean)

        # 解析回复引用：若用户引用了历史消息（如包含图片/成绩/文字），精准提取被引用消息详情
        reply_msg_id = None
        if message_segments and isinstance(message_segments, list):
            for seg in message_segments:
                if isinstance(seg, dict) and seg.get("type") == "reply":
                    reply_msg_id = str(seg.get("data", {}).get("id", "")).strip()
                    if reply_msg_id:
                        break
        if not reply_msg_id and raw_message:
            m = re.search(r"\[CQ:reply,id=([^,\]]+)\]", str(raw_message))
            if m:
                reply_msg_id = m.group(1).strip()

        quoted_msg_info = None
        quoted_was_cached = False
        if reply_msg_id:
            if reply_msg_id not in self._recent_messages_cache:
                stored = await self._stored_image_reference(group_id, reply_msg_id)
                if stored:
                    self._recent_messages_cache[reply_msg_id] = stored
            # 优先从本地近期消息缓存查找
            if reply_msg_id in self._recent_messages_cache:
                quoted_msg_info = self._recent_messages_cache[reply_msg_id]
                quoted_was_cached = True
                if quoted_msg_info.get("private_user_id") or quoted_msg_info.get("group_id") not in (None, group_id):
                    quoted_msg_info = None
            else:
                try:
                    msg_param = int(reply_msg_id) if reply_msg_id.lstrip("-").isdigit() else reply_msg_id
                    res = await self.send_action("get_msg", {"message_id": msg_param})
                    if res and res.get("status") == "ok" and res.get("data"):
                        msg_d = dict(res["data"])
                        if msg_d.get("message_type") == "private":
                            raise ValueError("Referenced message is private")
                        if msg_d.get("group_id") is not None and str(msg_d["group_id"]) != group_id:
                            raise ValueError("Referenced message belongs to a different group")
                        msg_d.update(group_id=group_id, message_id=reply_msg_id)
                        q_raw = msg_d.get("raw_message") or msg_d.get("message") or ""
                        q_sender_dict = msg_d.get("sender", {})
                        q_sender = (
                            q_sender_dict.get("card")
                            or q_sender_dict.get("nickname")
                            or self.get_cached_member_name(group_id, str(msg_d.get("user_id", "")))
                            or f"群友_{str(msg_d.get('user_id', ''))[-4:] if len(str(msg_d.get('user_id', ''))) >= 4 else str(msg_d.get('user_id', ''))}"
                        )
                        q_segments = msg_d.get("message") if isinstance(msg_d.get("message"), list) else None
                        q_enriched, q_img_analyses = await self._process_qq_images(msg_d,
                            content=q_raw if isinstance(q_raw, str) else "",
                            segments=q_segments, directed=image_directed,
                        )
                        quoted_msg_info = {
                            "message_id": reply_msg_id,
                            "group_id": group_id,
                            "user_id": str(msg_d.get("user_id", "")),
                            "nickname": q_sender,
                            "raw_message": q_raw,
                            "clean_text": q_enriched,
                            "image_analyses": q_img_analyses,
                            "image_message": msg_d,
                            "time": msg_d.get("time", time.time()),
                        }
                except Exception as q_err:
                    logger.debug(f"通过 get_msg 获取引用原消息 {reply_msg_id} 异常: {q_err}")

        # 多模态感知解析：将表情包与图片转为富文本描述
        raw_clean, raw_at_bot, _ = clean_cq_mentions(raw_message if isinstance(raw_message, str) else "", self.self_id,
            segments=message_segments, bot_names=self._bot_names(group_id))
        enriched_content, image_analyses = await self._process_qq_images(data,
            content=raw_message,
            segments=message_segments, directed=image_directed,
        )

        # 群聊人设隔离：获取当前群独立配置的人设预设
        group_persona_name = self.app_config.bot.get_persona_name(group_id) or self.personas.active_persona_name
        group_persona = persona_for(self, group_id)

        bot_names = self._bot_names(group_id)
        clean_text, is_at_bot, mentioned_users = clean_cq_mentions(
            text=enriched_content,
            self_id=self.self_id,
            segments=message_segments,
            member_name_getter=lambda q: self.get_cached_member_name(group_id, q),
            bot_names=bot_names
        )
        if active_turn and active_turn.lane == "chat" and active_turn.merged and active_turn.directed and not active_turn.batch:
            is_at_bot = True
        user_command_text = clean_text
        # Do not route injected visual descriptions or quoted messages as commands.
        from image_handler import strip_multimodal_tags
        user_command_text = strip_multimodal_tags(user_command_text)

        # 融合被引用消息的视觉数据与文字上下文
        if quoted_msg_info:
            from media_router import parse_media_intent
            quote_intent = parse_media_intent(user_command_text, self.app_config.media.default_image_count)
            forwarding_quote = bool(quote_intent and quote_intent.operation == "quote")
            if image_directed and not forwarding_quote and quoted_was_cached and quoted_msg_info.get("image_analyses"):
                source = {"group_id": group_id, "message_id": reply_msg_id,
                          "user_id": quoted_msg_info.get("user_id", ""),
                          "sender": {"nickname": quoted_msg_info.get("nickname", "")},
                          "time": quoted_msg_info.get("time", time.time())}
                await self._quoted_qq_images(quoted_msg_info, source, group_id=group_id)
            for q_img in quoted_msg_info.get("image_analyses", []):
                if not any(
                    (it.get("local_path") and it.get("local_path") == q_img.get("local_path"))
                    or (it.get("url") and it.get("url") == q_img.get("url"))
                    for it in image_analyses
                ):
                    image_analyses.append(q_img)

            q_author = quoted_msg_info.get("nickname") or "群友"
            q_text = quoted_msg_info.get("clean_text") or ""
            # 剥离已注入的多模态系统标记、指引及历史引用标签，提取用户实际纯文本对白
            q_clean = strip_multimodal_tags(q_text)

            q_parts = []
            if q_clean:
                q_sub = q_clean if len(q_clean) <= 120 else q_clean[:117] + "..."
                q_parts.append(f'"{q_sub}"')
            if quoted_msg_info.get("image_analyses"):
                from request_routing import image_reply_text
                for q_img in quoted_msg_info["image_analyses"]:
                    q_parts.append(f'[{image_source_label(q_img)}: {image_reply_text([q_img])}]')

            quote_annotation = f"[引用回复 @{q_author} 发送的消息: {' '.join(q_parts)}]"
            if quoted_msg_info.get("image_analyses"):
                quote_annotation += ("\n【引用图片识别结果】: 优先根据已完成的分支识别描述结合问题回答。"
                    "普通聊天不要重复打开图片或下载文件；缺少有效识别描述时如实说明。")

            if clean_text:
                clean_text = f"{clean_text}\n{quote_annotation}"
            else:
                clean_text = quote_annotation

        # 记录到最近消息缓存中供后续引用溯源
        if message_id:
            self._recent_messages_cache[str(message_id)] = {
                "message_id": str(message_id),
                "group_id": group_id,
                "user_id": user_id,
                "nickname": nickname,
                "raw_message": raw_message,
                "clean_text": clean_text,
                "image_analyses": list(image_analyses),
                "image_message": {key: data[key] for key in ("message_id", "group_id", "user_id", "sender", "time", "message", "raw_message") if key in data},
                "time": data.get("time") or time.time()
            }
            if len(self._recent_messages_cache) > 120:
                oldest = next(iter(self._recent_messages_cache))
                del self._recent_messages_cache[oldest]

        # Publish recognized content before any command path can return early.
        self._emit_group_feed(group_id, user_id, nickname, message_id, clean_text or raw_message, is_at_bot)

        # 检查是否为主人的 QQ 号或本群专属主人名称（群聊主人名称隔离）
        is_master = self.app_config.bot.is_master(group_id, user_id, nickname)

        # 记录本次打开bot到关闭bot的群聊全量消息（供关闭时大模型整理与刻写）
        if self.app_config.bot.memory_enabled:
            self.memory_mgr.record_session_message(
                group_openid=group_id,
                author_id=user_id,
                author_name=nickname,
                content=clean_text or raw_message,
                is_bot=False
            )

        # 前置意图思考：群聊每一句话在 @bot 时先直接经过大模型解析命令和消息判断
        if active_turn and active_turn.batch:
            for row in active_turn.batch:
                if row.get("id") == message_id:
                    row["text"] = clean_text
        if _batch_record_only:
            if active_turn:
                active_turn.text = clean_text
            self.context_buffer.add_message(group_id, user_id, nickname, clean_text, is_bot=False)
            return
        from command_deliberation import default_command_deliberator
        deliberation = await default_command_deliberator.deliberate_async(
            user_command_text,
            is_group=True,
            is_at_bot=is_at_bot,
            is_master=is_master,
            ai_client=self.ai
        )

        media_ctx = self._make_media_context(session_key=f"onebot_group_{group_id}", user_id=user_id,
            conversation_id=group_id, message_id=message_id, text=user_command_text, is_group=True,
            quoted_images=image_analyses)
        media_ctx.sender_role = str((data.get("sender") or {}).get("role") or "")
        media_ctx.quoted_message_id = reply_msg_id or ""
        if quoted_msg_info:
            if quoted_msg_info.get("image_message"):
                self._prepare_file_event(quoted_msg_info["image_message"])
        media_ctx.mentioned_user_ids = [str(item["user_id"]) for item in mentioned_users
            if str(item["user_id"]) not in (self.self_id, "all")]

        from request_routing import route_explicit_work
        if await route_explicit_work(self, media_ctx, user_command_text, directed=is_at_bot or is_master,
                                     is_master=is_master, sender_name=nickname):
            return

        # 只有 @ 机器人时且明确判定为媒体命令，才驱动反重力自带搜索与下载引擎执行
        if is_at_bot and user_command_text.startswith(("/", "／")) and deliberation.is_command and deliberation.command_type == "media":
            logger.info(f"🧠 [OneBot] 群 {group_id} 意图思考判定为媒体命令: {deliberation.rationale}")
            if await self._route_media(media_ctx, intent=deliberation.media_intent):
                return
        elif is_at_bot and user_command_text.startswith(("/", "／")):
            slash_cmd = user_command_text.lstrip("/／").split()[0].lower() if user_command_text.lstrip("/／") else ""
            if slash_cmd in ("image", "video", "file", "media", "pick", "cancel"):
                if await self._route_media(media_ctx):
                    return

        if clean_text:
            self.context_buffer.add_message(
                group_openid=group_id,
                author_openid=user_id,
                author_name=nickname,
                content=clean_text,
                is_bot=False
            )
            if self.app_config.bot.memory_enabled and nickname and not nickname.startswith("群友_"):
                self.memory_mgr.set_participant_nickname(group_id, user_id, nickname)

        # 实时向上层/控制台广播群聊消息（供 GUI 侧边栏实时监控展示）
        time_now = time.strftime("%H:%M:%S")
        at_flag = " [@我]" if is_at_bot else ""
        master_flag = " [主人👑]" if is_master else ""
        logger.info(f"📩 [OneBot] 收到群 {group_id} 消息 [{nickname}({user_id})]{at_flag}{master_flag}: {clean_text or raw_message}")

        # 指令检测（只有 @ 机器人时才触发命令，如果不 @ 则不视为对 bot 的命令，防止过度回复）
        if is_at_bot and deliberation.is_command and deliberation.command_type != "media":
            logger.info(f"🧠 [OneBot] 群 {group_id} 意图思考执行命令: {deliberation.as_command_str()} ({deliberation.rationale})")
            handled = await self._handle_command(deliberation.as_command_str(), group_id=group_id, user_id=user_id, is_group=True)
            if handled:
                return
        elif is_at_bot and (user_command_text.startswith("/") or user_command_text.startswith("／")):
            cmd_text = user_command_text.lstrip("/／").strip()
            handled = await self._handle_command(cmd_text, group_id=group_id, user_id=user_id, is_group=True)
            if handled:
                return

        # 只有明确对 bot 的安静请求才暂停空闲话题；下一条新消息仍重新分析。
        self._proactive_chat.note_quiet(group_id, user_command_text, directed=is_at_bot)

        # 安全政策风险优先前置预检（坚决不回复可能触发安全政策违规的内容）
        is_risk, risk_reason = check_safety_policy_risk(clean_text)
        if is_risk:
            logger.warning(f"🛡️ [OneBot] 群 {group_id} 消息触发安全政策风险预警 ({risk_reason})，放弃参与回复。")
            feed_act = {
                "time": time_now,
                "group_id": group_id,
                "action": "pass",
                "reason": f"安全政策风险规避: {risk_reason}",
                "reply": ""
            }
            print(f"[FEED_ACTION] {json.dumps(feed_act, ensure_ascii=False)}", flush=True)
            return

        must_reply = is_master or is_at_bot or bool(active_turn and active_turn.batch and active_turn.directed)
        if must_reply and image_analyses and not any(img.get("vision_verified") for img in image_analyses):
            await self._send_text("send_group_msg", "group_id", group_id,
                                  "这条图片的截图和原图获取或识别均未完成，暂时无法确认画面内容。")
            return

        # 检查是否包含图片或表情包，评估语义意图并进行过滤（意义不明则不随意回复）
        from image_handler import evaluate_multimodal_intent
        img_intent = evaluate_multimodal_intent(clean_text, image_analyses)
        if getattr(self.app_config.bot, "image_filter_enabled", True):
            if img_intent.get("should_avoid_reply") and not must_reply:
                logger.info(f"💤 [OneBot] 群 {group_id} 收到意义不明的图片/表情包且无实质提问，按规范不随意回复。")
                feed_act = {
                    "time": time_now,
                    "group_id": group_id,
                    "action": "pass",
                    "reason": "多模态过滤: 表情包/图片分析后意义不明，避免随意回复",
                    "reply": ""
                }
                print(f"[FEED_ACTION] {json.dumps(feed_act, ensure_ascii=False)}", flush=True)
                return

        if not must_reply:
            if not self.app_config.bot.autonomous_enabled:
                return
            if not self._participation.should_evaluate(group_id, user_command_text or clean_text,
                    enabled=self.app_config.bot.autonomous_enabled):
                return

        # 联网解析网络梗或生僻不明言语（辅助大模型理解并更加合理地用梗回复）
        from request_routing import prepare_slang_context
        search_info = await prepare_slang_context(self, media_ctx, clean_text)

        # 分析群聊近期发言节奏与大部分消息长度
        chat_rhythm = self.context_buffer.analyze_chat_rhythm(group_id)

        # 记录交互轮次，每10次交互提醒一次预设与长期记忆库，节约Token
        if self.app_config.bot.memory_enabled:
            grp_mem = self.memory_mgr.get_group_memory(group_id)
            turn = grp_mem.total_dialog_turns + 1
        else:
            turn = self._interaction_turn_counts.get(group_id, 0) + 1
        self._interaction_turn_counts[group_id] = turn
        reminder_interval = getattr(self.app_config.bot, "preset_reminder_interval", 10)
        include_preset = (turn == 1) or (turn % reminder_interval == 0)

        # 组装增强型 Prompt（结合群专属记忆、永久记忆预设、近期滑动上下文与节奏统计）
        session_key = self._chat_session_key(f"onebot_group_{group_id}")
        generation = session_generation(self.ai, session_key)
        memory_manifest = (
            await asyncio.to_thread(self.memory_mgr.get_memory_file_manifest, group_id,
                                    history_count=self.app_config.bot.memory_history_count)
            if self.app_config.bot.memory_enabled else None
        )
        recent_ctx = self.context_buffer.format_for_prompt(group_id, count=self.app_config.bot.context_message_count, exclude_last=True)
        image_context, image_delivery = await self._image_context_for_chat(session_key, group_id,
            user_command_text, message_id=reply_msg_id or "", current_message_id=message_id if image_analyses else "")
        if image_context:
            recent_ctx += "\n" + image_context

        group_master_name = self.app_config.bot.get_master_name(group_id)
        user_prompt = compose_autonomous_decision_prompt(
            user_text=clean_text or "你好",
            author_name=nickname,
            user_id=user_id,
            message_id=message_id,
            group_context=recent_ctx,
            chat_rhythm=chat_rhythm,
            max_reply_length=self.app_config.bot.max_reply_length,
            enable_action_descriptions=self.app_config.bot.enable_action_descriptions,
            is_master=is_master,
            is_at_bot=is_at_bot,
            must_reply=must_reply,
            mentioned_users=mentioned_users,
            search_info=search_info,
            include_preset=include_preset,
            interaction_turn=turn,
            preset_reminder_interval=reminder_interval,
            incremental=True,
            master_name=group_master_name
        )

        # 群聊人设隔离：生成针对当前群独立人设的 system_prompt
        group_system_prompt = compose_system_prompt(
            base_prompt=group_persona.prompt,
            max_reply_length=self.app_config.bot.max_reply_length,
            enable_action_descriptions=self.app_config.bot.enable_action_descriptions,
            bot_names=bot_names,
            master_name=group_master_name
        )

        try:
            async with self._reply_turn(session_key, media_ctx):
                user_prompt += "\n" + image_context if image_context else ""
                raw_reply = await self._main_chat(session_key, user_prompt, system_prompt=group_system_prompt,
                                               media_context=media_ctx,
                                               image_delivery=image_delivery,
                                               memory_manifest=memory_manifest, **self._chat_options())
                if not generation_is_current(self.ai, session_key, generation):
                    return
                self._ack_image_context(session_key, image_delivery)
                decision = parse_ai_decision_and_reply(
                    raw_response=raw_reply,
                    default_should_reply=must_reply,
                    is_at_bot=is_at_bot,
                    is_master=is_master,
                    default_user_id=user_id,
                    default_msg_id=message_id
                )

                # AI 自主决策不参与回复
                if not decision.should_reply:
                    logger.info(f"[OneBot] 群 {group_id} 本轮分析完成，暂不发言；新消息将重新分析 (原因: {decision.reason})")
                    feed_act = {
                        "time": time.strftime("%H:%M:%S"),
                        "group_id": group_id,
                        "action": "pass",
                        "reason": decision.reason,
                        "reply": ""
                    }
                    print(f"[FEED_ACTION] {json.dumps(feed_act, ensure_ascii=False)}", flush=True)
                    return

                final_reply = await self._send_decision_messages(decision, media_ctx, group_persona.name)
                if not final_reply or not generation_is_current(self.ai, session_key, generation):
                    return
                self._record_console_text(media_ctx, group_persona.name, final_reply)
                self._last_autonomous_reply_time[group_id] = time.time()
                self._proactive_chat.note_reply(group_id)
                if self.app_config.bot.memory_enabled:
                    self.memory_mgr.record_interaction(group_openid=group_id, user_openid=user_id,
                        user_msg=clean_text, bot_reply=final_reply, author_name=nickname)
                logger.info("🤖 [OneBot] 群 %s CLI 发送回复: %s", group_id, final_reply)

        except SessionInvalidatedError:
            return
        except SendUnknown as error:
            logger.warning("[OneBot] 群 %s 发送回执未知，未自动重发: %s", group_id, error)
        except AgentServiceError as error:
            logger.warning("[OneBot] 群 %s 本轮 AI 服务不可用，未记录机器人回复。", group_id)
            if must_reply and generation_is_current(self.ai, session_key, generation):
                await self.send_group_message(group_id, OutboundText(error.user_message))
        except Exception as e:
            logger.error(f"[OneBot] 群 {group_id} 生成回复异常: {e}", exc_info=True)

    async def _private_quoted_images(self, data, user_id):
        segments = data.get("message") if isinstance(data.get("message"), list) else []
        reference = next((str(seg.get("data", {}).get("id", "")) for seg in segments
                          if isinstance(seg, dict) and seg.get("type") == "reply"), "")
        if not reference:
            match = re.search(r"\[CQ:reply,id=([^,\]]+)", str(data.get("raw_message", "")))
            reference = match[1] if match else ""
        if not reference:
            return []
        cached = self._recent_messages_cache.get(reference)
        if cached is None:
            cached = await self._stored_image_reference(user_id, reference, is_group=False)
            if cached:
                self._recent_messages_cache[reference] = cached
        if cached is not None:
            if cached.get("private_user_id") != user_id:
                return []
            from media_router import parse_media_intent
            request = strip_multimodal_tags(str(data.get("raw_message") or ""))
            intent = parse_media_intent(request, self.app_config.media.default_image_count)
            if intent and intent.operation == "quote":
                return list(cached.get("image_analyses", []))
            return await self._quoted_qq_images(cached, {"message_id": reference, "user_id": user_id}, user_id=user_id)
        try:
            result = await self.send_action("get_msg", {"message_id": int(reference) if reference.lstrip("-").isdigit() else reference})
            quoted = dict(result.get("data") or {})
            # Inbound private messages must identify this peer. Never accept
            # a group message or an outgoing message with an unverified peer.
            if (quoted.get("group_id") or quoted.get("message_type") == "group"
                    or str(quoted.get("user_id", "")) != user_id):
                return []
            quoted["message_id"] = reference
            self._prepare_file_event(quoted)
            content = quoted.get("raw_message", "")
            _, images = await self._process_qq_images(quoted, content=content if isinstance(content, str) else "",
                segments=quoted.get("message") if isinstance(quoted.get("message"), list) else None)
            return images
        except Exception:
            logger.debug("Private referenced image is unavailable")
            return []

    @persona_bound("private")
    async def _handle_private_message(self, data: dict) -> None:
        """处理私聊消息"""
        data = self._prepare_file_event(data)
        self._control.poll(self)
        self.sync_system_prompt()
        user_id = str(data.get("user_id", ""))
        if self.self_id and user_id == self.self_id:
            return
        if self._defer_direct_image_event(data):
            return
        message_id = str(data.get("message_id", "") or "")
        if message_id:
            if message_id in self._processed_msg_ids:
                return
            self._processed_msg_ids.append(message_id)
        raw_message = data.get("raw_message", "") or data.get("message", "")
        message_segments = data.get("message") if isinstance(data.get("message"), list) else None

        from conversation_runtime import current_event
        event = current_event.get()
        original, _, _ = clean_cq_mentions(raw_message if isinstance(raw_message, str) else "", self.self_id,
                                          segments=message_segments, bot_names=self._bot_names(""))
        self._observe_chat(self._image_scope(user_id, False), message_id, user_id, original,
                           event.received_at if event else time.time())

        sender = data.get("sender", {})
        nickname = sender.get("nickname") or user_id
        is_master = is_master_user(self.app_config.bot.master_id, user_id)

        enriched_content, image_analyses = await self._process_qq_images(data,
            content=raw_message,
            segments=message_segments
        )
        clean_text, _, _ = clean_cq_mentions(enriched_content, self.self_id)
        user_command_text = strip_multimodal_tags(clean_text)
        quoted_images = await self._private_quoted_images(data, user_id)
        image_analyses.extend(quoted_images)
        if message_id:
            self._recent_messages_cache[message_id] = {"private_user_id": user_id,
                "message_id": message_id, "user_id": user_id, "nickname": nickname,
                "image_analyses": list(image_analyses), "clean_text": clean_text,
                "time": data.get("time") or time.time(),
                "image_message": {key: data[key] for key in ("message_id", "user_id", "sender", "time", "message", "raw_message") if key in data}}
            if len(self._recent_messages_cache) > 120:
                self._recent_messages_cache.pop(next(iter(self._recent_messages_cache)))

        if not clean_text:
            return

        master_flag = " [主人👑]" if is_master else ""
        logger.info(f"📩 [OneBot] 收到私聊消息 [{nickname}({user_id})]{master_flag}: {clean_text}")

        # 前置意图思考：判断私聊发言是属于命令还是日常闲聊
        from command_deliberation import default_command_deliberator
        deliberation = await default_command_deliberator.deliberate_async(
            user_command_text,
            is_group=False,
            is_at_bot=True,
            is_master=is_master,
            ai_client=self.ai
        )

        session_key = self._chat_session_key(f"onebot_private_{user_id}")
        media_ctx = self._make_media_context(session_key=session_key, user_id=user_id,
            conversation_id=user_id, message_id=message_id, text=user_command_text,
            quoted_images=image_analyses)
        reference = next((str((item.get("data") or {}).get("id") or "") for item in message_segments or []
                          if isinstance(item, dict) and item.get("type") == "reply"), "")
        if not reference:
            match = re.search(r"\[CQ:reply,id=([^,\]]+)\]", str(raw_message))
            reference = match[1] if match else ""
        media_ctx.quoted_message_id = reference

        from request_routing import route_explicit_work
        if await route_explicit_work(self, media_ctx, user_command_text, is_master=is_master, sender_name=nickname):
            return

        if deliberation.is_command:
            logger.info(f"🧠 [OneBot] 私聊意图思考判定为命令 [{deliberation.command_type}]: {deliberation.rationale}")
            if deliberation.command_type == "media" and user_command_text.startswith(("/", "／")):
                if await self._route_media(media_ctx, intent=deliberation.media_intent):
                    return
            elif deliberation.command_type != "media":
                cmd_str = deliberation.as_command_str()
                handled = await self._handle_command(cmd_str, user_id=user_id, is_group=False)
                if handled:
                    return

        if clean_text.startswith("/") or clean_text.startswith("／"):
            cmd_text = clean_text.lstrip("/／").strip()
            slash_cmd = cmd_text.split()[0].lower() if cmd_text else ""
            if slash_cmd in ("image", "video", "file", "media", "pick", "cancel"):
                if await self._route_media(media_ctx):
                    return
            handled = await self._handle_command(cmd_text, user_id=user_id, is_group=False)
            if handled:
                return

        generation = session_generation(self.ai, session_key)
        if image_analyses and not any(img.get("vision_verified") for img in image_analyses):
            await self._send_text("send_private_msg", "user_id", user_id,
                                  "这条图片的截图和原图获取或识别均未完成，暂时无法确认画面内容。")
            return
        if quoted_images:
            from request_routing import image_reply_text
            clean_text += "\n[用户引用的图片，分支识别结果: " + image_reply_text(quoted_images) + "]"

        # 记录私聊交互轮次与每10次预设提醒
        priv_key = f"private_{user_id}"
        turn = self._interaction_turn_counts.get(priv_key, 0) + 1
        self._interaction_turn_counts[priv_key] = turn
        reminder_interval = getattr(self.app_config.bot, "preset_reminder_interval", 10)
        include_preset = (turn == 1) or (turn % reminder_interval == 0)

        # 联网解析梗或生僻不明言语
        from request_routing import prepare_slang_context
        search_info = await prepare_slang_context(self, media_ctx, clean_text)

        user_prompt = compose_user_prompt(
            user_text=clean_text,
            author_name=nickname,
            is_master=is_master,
            max_reply_length=self.app_config.bot.max_reply_length,
            enable_action_descriptions=self.app_config.bot.enable_action_descriptions,
            is_autonomous=False,
            search_info=search_info,
            include_preset=include_preset,
            interaction_turn=turn,
            preset_reminder_interval=reminder_interval
            , incremental=True
        )

        image_context, image_delivery = await self._image_context_for_chat(session_key, user_id,
            user_command_text, is_group=False, message_id=media_ctx.quoted_message_id,
            current_message_id=message_id if image_analyses else "")
        if image_context:
            user_prompt = image_context + "\n\n" + user_prompt

        try:
            async with self._reply_turn(session_key, media_ctx):
                raw_reply = await self._main_chat(session_key, user_prompt, media_context=media_ctx,
                                                 image_delivery=image_delivery, **self._chat_options())
                if not generation_is_current(self.ai, session_key, generation):
                    return
                self._ack_image_context(session_key, image_delivery)

                decision = parse_ai_decision_and_reply(
                    raw_response=raw_reply,
                    default_should_reply=True,
                    is_at_bot=True,
                    is_master=is_master,
                    default_user_id=user_id,
                    default_msg_id=message_id
                )
                if not decision.should_reply:
                    return

                active_persona = self.personas.get_active_persona()
                final_reply = await self._send_decision_messages(decision, media_ctx, active_persona.name)
                if not final_reply or not generation_is_current(self.ai, session_key, generation):
                    return
                self._remember_chat_reply(media_ctx, final_reply)
                logger.info("🤖 [OneBot] 私聊 %s CLI 发送回复: %s", user_id, final_reply)
        except SessionInvalidatedError:
            return
        except SendUnknown as error:
            logger.warning("[OneBot] 私聊 %s 发送回执未知，未自动重发: %s", user_id, error)
        except AgentServiceError as error:
            logger.warning("[OneBot] 私聊 %s 本轮 AI 服务不可用，未记录机器人回复。", user_id)
            if generation_is_current(self.ai, session_key, generation):
                await self.send_private_message(user_id, error.user_message)
        except Exception as e:
            logger.error(f"[OneBot] 私聊 {user_id} 回复异常: {e}", exc_info=True)

    async def _handle_command(self, cmd_text: str, group_id: str = "", user_id: str = "", is_group: bool = True) -> bool:
        """Compatibility entry; execution and delivery belong to the console."""
        from conversation_runtime import current_event
        event = current_event.get()
        from console_tools.chat_commands import parse_command
        text = cmd_text.strip()
        if not text.startswith(("/", "／")):
            try:
                command = parse_command(text, natural=True, is_group=is_group)
            except ValueError:
                command = None
            text = command.canonical if command else "/" + text
        ctx = self._make_media_context(session_key=f"onebot_group_{group_id}" if is_group else f"onebot_private_{user_id}",
            user_id=user_id, conversation_id=group_id if is_group else user_id,
            message_id=event.message_id if event else "", text=text, is_group=is_group)
        await self._get_main_coordinator().submit_chat_command(ctx, text)
        return True
