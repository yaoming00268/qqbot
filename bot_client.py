# -*- coding: utf-8 -*-
"""
QQ Bot Client using Tencent official qq-botpy SDK.
Handles Group, C2C (Private), and Guild/Channel messages,
forwarding queries to Antigravity Gemini Relay.
Supports autonomous group chat, per-group compressed memory,
action description toggle, and hard length limits.
"""

from __future__ import annotations

from persona_isolation import persona_bound, persona_for
import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from typing import Dict, List, Optional

import botpy
from botpy.message import C2CMessage, GroupMessage, Message, DirectMessage

from antigravity_client import AntigravityClient
from config import AppConfig
from group_memory import GroupMemoryManager, GroupContextBuffer
from persona_manager import PersonaManager, PersonaPreset
from prompt_builder import (
    compose_system_prompt,
    compose_user_prompt,
    compose_autonomous_decision_prompt,
    parse_ai_decision_and_reply,
    check_safety_policy_risk,
    strip_action_descriptions,
    deflect_preaching
)
from image_handler import process_multimodal_message, strip_multimodal_tags
from media_integration import MediaClientMixin
from runtime_control import session_generation, generation_is_current
from antigravity_client import AgentServiceError, SessionInvalidatedError
from messaging import is_master_user

logger = logging.getLogger("QQBot")


# 动态修补 botpy，使其支持全量群消息事件 group_message_create
def _patch_botpy_group_message_parser():
    import botpy.connection
    if not hasattr(botpy.connection.ConnectionState, "parse_group_message_create"):
        def parse_group_message_create(self, payload):
            _message = GroupMessage(self.api, payload.get("id", None), payload.get("d", {}))
            self._dispatch("group_message_create", _message)
        botpy.connection.ConnectionState.parse_group_message_create = parse_group_message_create

_patch_botpy_group_message_parser()


def split_message(
    text: str,
    max_length: int = 1200,
    first_chunk_prefix: str = ""
) -> List[str]:
    """
    智能分段长文本，尽量按段落或换行切分，确保符合 QQ 单条消息长度限制。
    边界防御规避 max_length <= 0 死循环，支持 first_chunk_prefix 确保附加 @mention 后首段依然不超长。
    """
    text = text.strip()
    if not text:
        return []

    # 边界防御：规避 max_length <= 0 死循环卡死
    safe_max = int(max_length) if (max_length is not None and max_length > 0) else 1200

    prefix = first_chunk_prefix.strip()
    prefix_str = f"{prefix} " if prefix else ""
    # 前缀若达到或超过 safe_max，则无论如何无法容纳正文字符，明确省略前缀以确保所有分段严格不超过 safe_max
    if len(prefix_str) >= safe_max:
        prefix_str = ""
    first_max = safe_max - len(prefix_str)

    # 若加了前缀依然整体短于限制，直接返回单个拼接段落
    if len(prefix_str + text) <= safe_max:
        return [f"{prefix_str}{text}"]

    chunks = []
    paragraphs = text.split("\n")
    current_chunk = ""
    is_first = True

    for para in paragraphs:
        target_limit = first_max if is_first else safe_max
        if len(current_chunk) + len(para) + (1 if current_chunk else 0) <= target_limit:
            current_chunk = f"{current_chunk}\n{para}" if current_chunk else para
        else:
            if current_chunk.strip():
                if is_first:
                    chunks.append(f"{prefix_str}{current_chunk.strip()}")
                    is_first = False
                else:
                    chunks.append(current_chunk.strip())
                current_chunk = ""

            # 如果单个段落本身就超过限制，强制按字符切分
            while len(para) > (first_max if is_first else safe_max):
                lim = first_max if is_first else safe_max
                part = para[:lim].strip()
                if part:
                    if is_first:
                        chunks.append(f"{prefix_str}{part}")
                        is_first = False
                    else:
                        chunks.append(part)
                para = para[lim:]
            current_chunk = para

    if current_chunk.strip():
        if is_first:
            chunks.append(f"{prefix_str}{current_chunk.strip()}")
        else:
            chunks.append(current_chunk.strip())

    return [c for c in chunks if c.strip()]


def clean_mention_text(content: str, bot_id: Optional[str] = None, bot_name: Optional[str] = None) -> str:
    """清理 QQ 消息中开头的全部 @ 标签与前后空白"""
    if not content:
        return ""
    cleaned = content.strip()
    while True:
        prev = cleaned
        if bot_id:
            cleaned = re.sub(rf"^\s*<@!?{re.escape(str(bot_id))}>\s*", "", cleaned)
        if bot_name:
            cleaned = re.sub(rf"^\s*@{re.escape(str(bot_name))}\s*", "", cleaned)

        # 兜底过滤开头的任意 QQ @ 标签
        cleaned = re.sub(r"^\s*<@!?[0-9a-zA-Z_\-]+>\s*", "", cleaned)
        cleaned = re.sub(r"^@\S+\s*", "", cleaned)
        if cleaned == prev:
            break
    return cleaned.strip()


class GeminiQQBot(MediaClientMixin, botpy.Client):
    """基于官方 qq-botpy 的 Gemini 智能客服机器人"""

    def __init__(
        self,
        config: AppConfig,
        antigravity_client: AntigravityClient,
        persona_manager: Optional[PersonaManager] = None,
        intents: Optional[botpy.Intents] = None,
        memory_manager: Optional[GroupMemoryManager] = None
    ):
        if intents is None:
            intents = botpy.Intents.default()
            intents.public_messages = True
            intents.direct_message = True
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
        super().__init__(intents=intents, is_sandbox=config.qq.is_sandbox)
        self.app_config = config
        self.ai = antigravity_client
        self.personas = persona_manager or PersonaManager()
        self.start_time = time.time()

        # 初始化群聊记忆库管理器与上下文滑动窗口
        self.memory_mgr = memory_manager or GroupMemoryManager(
            storage_dir="data/memories",
            max_entries=config.bot.memory_max_entries
        )
        self.context_buffer = GroupContextBuffer(max_buffer_size=50)

        # 自主插话冷却时间记录 (group_openid -> last_reply_timestamp)
        self._last_autonomous_reply_time: Dict[str, float] = {}

        # 消息去重队列，避免在多事件监听或重试时重复响应
        self._processed_msg_ids: deque[str] = deque(maxlen=400)

        # 交互轮次计数 (用于每10次交互提醒一次预设以减少token浪费)
        self._interaction_turn_counts: Dict[str, int] = {}

        # 消息引用与多模态图片溯源缓存 (message_id -> message details)
        self._recent_messages_cache: Dict[str, dict] = {}
        self._init_media_runtime()
        self._control_task = None

        # 动态组装当前生效人设的 Prompt 并注入到 AI 客户端
        self.sync_system_prompt()
        active_persona = self.personas.get_active_persona()
        logger.info(
            f"已加载生效人设: 【{active_persona.name}】 | "
            f"单条分段阈值: {self.app_config.bot.max_reply_length} 字 | "
            f"动作神态: {'开启' if self.app_config.bot.enable_action_descriptions else '关闭(纯台词)'} | "
            f"自主模式: {'已启用' if self.app_config.bot.autonomous_enabled else '未启用'}"
        )

    def sync_system_prompt(self, force_reload: bool = False) -> None:
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
                    logger.info("⚡ [QQBot] 检测到 config.yaml 变更，已热更新主人与群专属配置！")
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
                    skey = self._chat_session_key(f"group_{gid}")
                    self.ai.reset_session(skey)
                    self.ai.reset_session(f"__work__:{skey}")
                    self._cancel_media(self._make_media_context(
                        session_key=skey,
                        user_id="",
                        conversation_id=gid,
                        is_group=True
                    ))
                    logger.info(f"🔄 [QQBot] 群 {gid} 独立人设/主人配置变更({old_fp[0]} -> {new_fp[0]})，已失效并重置该群会话。")
            self._group_persona_fingerprints = dict(current_fps)

        if self._personas_changed():
            logger.info(f"人设配置已从磁盘热重载，当前生效人设: 【{self.personas.active_persona_name}】")

        active_persona = self.personas.get_active_persona()
        composed = compose_system_prompt(
            base_prompt=active_persona.prompt,
            max_reply_length=self.app_config.bot.max_reply_length,
            enable_action_descriptions=self.app_config.bot.enable_action_descriptions
        )
        self.ai.system_prompt = composed

    @property
    def safe_robot(self):
        """安全获取当前连接的 robot 信息，规避尚未连上 WebSocket 时 self.robot 抛出 AttributeError"""
        try:
            conn = getattr(self, "_connection", None)
            if conn and getattr(conn, "state", None):
                return getattr(conn.state, "robot", None)
        except Exception:
            pass
        return None

    def _get_help_message(self, session_key: str) -> str:
        session = self.ai.get_or_create_session(self._chat_session_key(session_key))
        active_persona = self.personas.get_active_persona()
        uptime = int(time.time() - self.start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)

        action_mode = "开启" if self.app_config.bot.enable_action_descriptions else "纯台词对话模式(已禁用括号动作)"
        auto_mode = "已开启 (AI 根据上下文自然接话)" if self.app_config.bot.autonomous_enabled else "未启用"
        mem_mode = f"已启用 (阈值 {self.app_config.bot.memory_max_entries}条)" if self.app_config.bot.memory_enabled else "未启用"

        help_text = (
            "✨ 【QQ 智能助手 - Gemini 3.8 驱动】 ✨\n\n"
            f"🎭 当前人设: {active_persona.name}\n"
            f"🤖 当前模型: {session.model.upper()}\n"
            f"📏 单条分段阈值: {self.app_config.bot.max_reply_length} 字（整轮可多条）\n"
            f"🎭 动作描写: {action_mode}\n"
            f"💬 群聊自主插话: {auto_mode}\n"
            f"🧠 群记忆库: {mem_mode}\n"
            f"⏱️ 运行时间: {hours}小时{minutes}分{seconds}秒\n\n"
            "📌 【常用指令】\n"
            "• 直接发送文字 / @我提问：开始与 Gemini 畅聊\n"
            "• /clear 或 重置：清空上下文记忆，开启新对话\n"
            "• /persona 或 人设：查看可用人设列表\n"
            "• /persona <名称>：切换当前人设预设\n"
            "• /model [flash|pro|flash_lite]：切换 AI 模型\n"
            "• /memory 或 群记忆：查看本群专属记忆摘要\n"
            "• /clearmemory 或 清空群记忆：清空本群记忆库\n"
            "• /status：查看机器人当前运行状态与会话信息\n"
            "• /help 或 帮助：查看本帮助菜单\n\n"
            "• /media：图片、视频与文件功能帮助\n\n"
            "💡 本机器人直接基于 Google Antigravity 本地账号配额运行，免 API Key 无限制畅享！"
        )
        return help_text

    def is_bot_mentioned(self, message: GroupMessage) -> bool:
        """精确判断机器人是否被群成员 @Mention"""
        raw_content = message.content or ""

        # 收集机器人的有效标识
        bot_ids = set()
        bot_names = set()

        robot = self.safe_robot
        if robot:
            if getattr(robot, "id", None):
                bot_ids.add(str(robot.id))
            if getattr(robot, "member_openid", None):
                bot_ids.add(str(robot.member_openid))
            if getattr(robot, "name", None):
                bot_names.add(str(robot.name).strip())

        if self.app_config.qq.appid:
            bot_ids.add(str(self.app_config.qq.appid).strip())

        # 1. 检查 message.mentions 结构体
        if hasattr(message, "mentions") and message.mentions:
            for m in message.mentions:
                m_id = str(getattr(m, "id", None) or getattr(m, "member_openid", "") or "")
                m_username = str(getattr(m, "username", "") or "").strip()
                if m_id and m_id in bot_ids:
                    return True
                if m_username and m_username in bot_names:
                    return True

        # 2. 检查消息正文中的 ID 提及
        for bid in bot_ids:
            if bid and (f"<@!{bid}>" in raw_content or f"<@{bid}>" in raw_content):
                return True

        # 3. 检查消息正文中的机器人名称提及
        for bname in bot_names:
            if bname and (f"@{bname}" in raw_content or raw_content.startswith(f"@{bname}")):
                return True

        return False

    def get_author_display_name(
        self,
        group_openid: str,
        user_openid: str,
        author_obj: Optional[object] = None
    ) -> str:
        """获取群成员的可读显示名称"""
        if is_master_user(self.app_config.bot.master_id, user_openid):
            return "主人"

        if author_obj:
            for attr in ("username", "nickname", "name"):
                val = getattr(author_obj, attr, None)
                if val and str(val).strip():
                    return str(val).strip()

        mem = self.memory_mgr.get_group_memory(group_openid)
        part = mem.participants.get(user_openid)
        if part and part.nickname:
            return part.nickname

        suffix = user_openid[-4:] if len(user_openid) >= 4 else user_openid
        return f"群友_{suffix}"

    @persona_bound("core")
    async def _handle_message_core(
        self,
        session_key: str,
        raw_content: str,
        reply_func,
        user_openid: Optional[str] = None,
        media_context=None,
        deliberation=None,
    ):
        """统一处理不同场景（私聊、频道等）的消息调度与基础命令"""
        self.sync_system_prompt()
        content = clean_mention_text(raw_content) if self.app_config.bot.strip_mentions else raw_content.strip()

        if not content:
            await reply_func("你好呀！请问有什么我可以帮你的？（输入 /help 查看帮助）")
            return

        # 前置意图思考：判断发言是属于命令而非闲聊，泛化识别自然语言指令与媒体需求
        from command_deliberation import default_command_deliberator
        is_grp = session_key.startswith("group_")
        is_master = is_master_user(self.app_config.bot.master_id, user_openid or "")
        clean_text_for_delib = strip_multimodal_tags(content)
        if deliberation is None:
            deliberation = await default_command_deliberator.deliberate_async(
                clean_text_for_delib,
                is_group=is_grp,
                is_at_bot=True,
                is_master=is_master,
                ai_client=self.ai
            )

        if media_context is None and session_key.startswith(("group_", "c2c_")):
            media_context = self._make_media_context(session_key=session_key, user_id=user_openid,
                conversation_id=session_key.split("_", 1)[1], text=clean_text_for_delib,
                is_group=is_grp, reply=reply_func)

        from request_routing import route_explicit_work
        if await route_explicit_work(self, media_context, clean_text_for_delib, is_master=is_master):
            return

        # 8. 常规 AI 对话
        session_key = self._chat_session_key(session_key)
        try:
            logger.info(f"[{session_key}] 收到用户提问: {content[:50]}...")
            is_master = is_master_user(self.app_config.bot.master_id, user_openid or "")
            # 交互轮次与每10次预设提醒
            turn = self._interaction_turn_counts.get(session_key, 0) + 1
            self._interaction_turn_counts[session_key] = turn
            reminder_interval = getattr(self.app_config.bot, "preset_reminder_interval", 10)
            include_preset = (turn == 1) or (turn % reminder_interval == 0)

            # 联网解析网络梗与生僻不明言语
            from request_routing import prepare_slang_context
            search_info = await prepare_slang_context(self, media_context, content)

            user_prompt = compose_user_prompt(
                user_text=content,
                author_name="主人" if is_master else "用户",
                is_master=is_master,
                max_reply_length=self.app_config.bot.max_reply_length,
                enable_action_descriptions=self.app_config.bot.enable_action_descriptions,
                is_autonomous=False,
                search_info=search_info,
                include_preset=include_preset,
                interaction_turn=turn,
                preset_reminder_interval=reminder_interval,
                incremental=True
            )
            generation = session_generation(self.ai, session_key)
            image_delivery = None
            if media_context:
                image_context, image_delivery = await self._image_context_for_chat(session_key,
                    media_context.conversation_id, strip_multimodal_tags(content), is_group=media_context.is_group,
                    message_id=media_context.quoted_message_id,
                    current_message_id=media_context.message_id if media_context.quoted_images else "")
                if image_context:
                    user_prompt = image_context + "\n\n" + user_prompt
            async with self._reply_turn(session_key, media_context):
                reply_text = await self._main_chat(session_key, user_prompt, media_context=media_context,
                                                   image_delivery=image_delivery, **self._chat_options())
                if not generation_is_current(self.ai, session_key, generation):
                    return
                self._ack_image_context(session_key, image_delivery)

                from prompt_builder import parse_ai_decision_and_reply
                initial_decision = parse_ai_decision_and_reply(reply_text, default_should_reply=True)
                if not initial_decision.should_reply:
                    return
                if media_context:
                    spoken = await self._send_decision_messages(initial_decision, media_context,
                        self.personas.get_active_persona().name)
                    if spoken:
                        self._remember_chat_reply(media_context, spoken)

        except SessionInvalidatedError:
            return
        except AgentServiceError as error:
            logger.warning("[%s] 本轮 AI 服务不可用，未记录机器人回复。", session_key)
            if generation_is_current(self.ai, session_key, generation):
                await reply_func(error.user_message)
        except Exception as e:
            logger.error(f"处理消息失败: {e}", exc_info=True)
            await reply_func("⚠️ 消息暂时处理失败，请稍后再试。")

    # ========================================================
    # 核心群聊调度器：自主插话、主仆特权、上下文关联与记忆库沉淀
    # ========================================================
    @persona_bound("group")
    async def _handle_group_message(self, message: GroupMessage, is_at_event: bool = True, _batch_record_only=False):
        from conversation_runtime import current_event
        event = current_event.get()
        received_at = (event.received_at if event else 0) or time.time()
        # 0. 检查并热重载人设配置
        self._control.poll(self)
        self.sync_system_prompt()

        # 1. 消息幂等去重
        if not message.id:
            return
        if message.id in self._processed_msg_ids:
            return
        self._processed_msg_ids.append(message.id)

        group_openid = message.group_openid
        user_openid = message.author.member_openid if hasattr(message.author, "member_openid") else "unknown"

        # 0. 忽略机器人自身发送的消息，杜绝自言自语死循环
        bot_ids = set()
        robot = self.safe_robot
        if robot:
            if getattr(robot, "id", None):
                bot_ids.add(str(robot.id))
            if getattr(robot, "member_openid", None):
                bot_ids.add(str(robot.member_openid))
        if self.app_config.qq.appid:
            bot_ids.add(str(self.app_config.qq.appid).strip())

        if str(user_openid) in bot_ids:
            return

        # 若作者被标记为机器人且未直接 @ 我，跳过自主互动，避免两机死循环互刷
        if getattr(message.author, "bot", None) is True and not (is_at_event or self.is_bot_mentioned(message)):
            return

        if event is None:
            self._proactive_chat.observe(group_openid, message.id, received_at=received_at)
        self._observe_chat(self._image_scope(group_openid), message.id, user_openid,
                           clean_mention_text(message.content or ""), received_at,
                           is_bot=getattr(message.author, "bot", False) is True)

        # 解析回复引用（若支持消息引用扩展）
        ref = getattr(message, "message_reference", None)
        ref_id = str(getattr(ref, "message_id", "")).strip() if ref else ""
        quoted_msg_info = None
        if ref_id and ref_id not in self._recent_messages_cache:
            stored = await self._stored_image_reference(group_openid, ref_id)
            if stored:
                self._recent_messages_cache[ref_id] = stored
        if ref_id and ref_id in self._recent_messages_cache:
            quoted_msg_info = self._recent_messages_cache[ref_id]
            if quoted_msg_info.get("private_user_id") or quoted_msg_info.get("group_id") not in (None, group_openid):
                quoted_msg_info = None

        raw_content = message.content or ""
        attachments = getattr(message, "attachments", None)
        if attachments:
            self._emit_group_feed(group_openid, user_openid,
                self.get_author_display_name(group_openid, user_openid, message.author), message.id,
                clean_mention_text(raw_content), is_at_event or self.is_bot_mentioned(message), pending=True)
        image_directed = bool(is_at_event or self.is_bot_mentioned(message) or clean_mention_text(raw_content).startswith(("/", "／"))
                              or self.app_config.bot.is_master(group_openid, user_openid) or self.app_config.bot.autonomous_enabled)
        from media_router import is_priority_control
        image_directed = image_directed and not is_priority_control(clean_mention_text(raw_content))

        # 多模态感知解析：提取并分析图片附件
        image_analyses = []
        if attachments:
            async def remember_image(image, index):
                await self._save_image_context(group_openid, message.id, [image],
                    sender_id=user_openid, timestamp=received_at, query=strip_multimodal_tags(message.content or ""), strict=True)
            enriched_content, image_analyses = await process_multimodal_message(
                content=raw_content,
                attachments=attachments, allow_download=image_directed,
                ai_client=self.ai if image_directed else None,
                vision_session_key=f"__work__:incoming:official:{group_openid}:{message.id}", on_recognized=remember_image,
            )
            raw_content = enriched_content
            await self._save_image_context(group_openid, message.id, image_analyses,
                sender_id=user_openid, timestamp=received_at, query=strip_multimodal_tags(message.content or ""))

        clean_text = clean_mention_text(raw_content) if self.app_config.bot.strip_mentions else raw_content.strip()
        user_command_text = strip_multimodal_tags(clean_text)

        # 判断是否显式 @ 了机器人（若为 @ 事件，则直接确认为被 @）
        is_direct_at = bool(is_at_event or self.is_bot_mentioned(message))
        if event and event.lane == "chat" and event.merged and event.directed and not event.batch:
            is_direct_at = True
        display_name = self.get_author_display_name(group_openid, user_openid, getattr(message, "author", None))
        is_master = self.app_config.bot.is_master(group_openid, user_openid, display_name)

        # 融合被引用消息的视觉数据与文字上下文
        if quoted_msg_info:
            if image_directed and quoted_msg_info.get("image_analyses"):
                from request_routing import recognize_cached_images
                quoted_msg_info["image_analyses"] = await recognize_cached_images(
                    quoted_msg_info["image_analyses"], self.ai,
                    f"__work__:incoming:official:{group_openid}:{message.id}:quoted")
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
                q_parts.append("[引用图片识别结果: " + image_reply_text(quoted_msg_info["image_analyses"]) + "]")

            quote_annotation = f"[引用回复 @{q_author} 发送的消息: {' '.join(q_parts)}]"
            if quoted_msg_info.get("image_analyses"):
                quote_annotation += "\n【引用图片识别结果】: 根据分支识别描述回答，普通聊天不重复打开图片或下载文件。"

            if clean_text:
                clean_text = f"{clean_text}\n{quote_annotation}"
            else:
                clean_text = quote_annotation

        msg_id = getattr(message, "id", "")
        if msg_id:
            self._recent_messages_cache[str(msg_id)] = {
                "message_id": str(msg_id),
                "group_id": group_openid,
                "user_id": user_openid,
                "nickname": display_name,
                "clean_text": clean_text,
                "image_analyses": list(image_analyses),
                "time": time.time()
            }
            if len(self._recent_messages_cache) > 120:
                oldest = next(iter(self._recent_messages_cache))
                del self._recent_messages_cache[oldest]

        self._emit_group_feed(group_openid, user_openid, display_name, msg_id, clean_text, is_direct_at)

        # 多模态语义过滤：若图片分析后意义不明且无实质提问，不随意回复
        from image_handler import evaluate_multimodal_intent
        img_intent = evaluate_multimodal_intent(clean_text, image_analyses)
        if getattr(self.app_config.bot, "image_filter_enabled", True) and img_intent.get("should_avoid_reply") and not (is_direct_at or is_master or _batch_record_only or event and event.batch):
            logger.info(f"[Group {group_openid}] 收到意义不明的图片/表情包且无实质提问，按规范不随意回复。")
            return

        # 2. 将消息压入群聊滑动上下文缓冲区与会话记忆池（无论是否回复，均记录上下文与说话人画像）
        if clean_text:
            self.context_buffer.add_message(
                group_openid=group_openid,
                author_openid=user_openid,
                author_name=display_name,
                content=clean_text,
                is_bot=False
            )
            if self.app_config.bot.memory_enabled:
                self.memory_mgr.record_session_message(
                    group_openid=group_openid,
                    author_id=user_openid,
                    author_name=display_name,
                    content=clean_text,
                    is_bot=False
                )
                if display_name and not display_name.startswith("群友_") and display_name != "用户":
                    self.memory_mgr.set_participant_nickname(group_openid, user_openid, display_name)

        # 实时广播群聊消息（供 GUI 侧边栏监控展示）
        msg_id = getattr(message, "id", "")
        time_now = time.strftime("%H:%M:%S")
        if event and event.batch:
            for row in event.batch:
                if row.get("id") == msg_id:
                    row["text"] = clean_text
        if _batch_record_only:
            if event:
                event.text = clean_text
            return

        # 安全政策前置规避
        is_risk, risk_reason = check_safety_policy_risk(clean_text)
        if is_risk:
            logger.warning(f"🛡️ [Group {group_openid}] 消息命中安全政策高危特征 ({risk_reason})，放弃参与回复。")
            feed_act = {
                "time": time_now,
                "group_id": group_openid,
                "action": "pass",
                "reason": f"安全政策风险规避: {risk_reason}",
                "reply": ""
            }
            print(f"[FEED_ACTION] {json.dumps(feed_act, ensure_ascii=False)}", flush=True)
            return

        # 3. 决策是否需要做出回复
        should_reply = False
        is_autonomous = False

        if is_direct_at or user_command_text.startswith(("/", "／")) or event and event.batch and event.directed:
            # 显式 @ 机器人：必定回复
            should_reply = True
            is_autonomous = False
        else:
            # 未 @ 机器人，判定是否触发自主回复或主人特权
            if is_master:
                # 【主人特权】：主人在群聊中说话，100% 必定回复！
                logger.info(f"[Group {group_openid}] 检测到主人({user_openid})发言，触发 100% 必定回复特权！")
                should_reply = True
                is_autonomous = True
            else:
                # 普通成员发言：检查自主插话总开关
                if not self.app_config.bot.autonomous_enabled:
                    return

                if not self._participation.should_evaluate(group_openid, user_command_text or clean_text,
                        enabled=self.app_config.bot.autonomous_enabled):
                    return

                should_reply = True
                is_autonomous = True

        if not should_reply:
            return

        # 引用和 @ 由主代理通过 CLI 显式指定。
        # 6. 回复闭包
        async def reply(content: str, msg_seq: int = 1, **reply_options):
            try:
                send_res = await message.reply(content=content, msg_seq=msg_seq, **reply_options)
                if isinstance(send_res, dict) and "id" in send_res:
                    self._processed_msg_ids.append(str(send_res["id"]))
            except Exception as e:
                logger.error(f"群消息回复发送失败: {e}")
                raise

        # 7. 检查是否为指令（只有 @ 机器人时才触发命令，如果不 @ 则不视为对 bot 的命令，防止过度回复）
        session_key = f"group_{group_openid}"
        media_ctx = self._make_media_context(session_key=session_key, user_id=user_openid,
            conversation_id=group_openid, message_id=message.id, text=user_command_text, is_group=True,
            reply=reply, quoted_images=image_analyses, received_at=received_at)
        media_ctx.quoted_message_id = ref_id
        media_ctx.mentioned_user_ids = [str(getattr(item, "member_openid", "") or getattr(item, "id", ""))
            for item in (getattr(message, "mentions", None) or [])
            if str(getattr(item, "member_openid", "") or getattr(item, "id", "")) not in ("", str(getattr(self, "self_id", "")))]
        self._record_official_attachments(message, media_ctx)
        activity = self._proactive_chat.groups.get(str(group_openid))
        if activity and activity.message_id == str(message.id):
            activity.reply_context = media_ctx
        self._proactive_chat.note_quiet(group_openid, user_command_text, directed=is_direct_at)

        from request_routing import route_explicit_work
        if await route_explicit_work(self, media_ctx, user_command_text, directed=is_direct_at or is_master,
                                     is_master=is_master, sender_name=display_name):
            return

        from command_deliberation import default_command_deliberator
        deliberation = await default_command_deliberator.deliberate_async(
            user_command_text,
            is_group=True,
            is_at_bot=is_direct_at,
            is_master=is_master,
            ai_client=self.ai
        )

        if is_direct_at and user_command_text.startswith(("/", "／")) and deliberation.is_command and deliberation.command_type == "media":
            if await self._route_media(media_ctx, intent=deliberation.media_intent):
                return
        elif is_direct_at and user_command_text.startswith(("/", "／")):
            slash_cmd = user_command_text.lstrip("/／").split()[0].lower() if user_command_text.lstrip("/／") else ""
            if slash_cmd in ("image", "video", "file", "media", "pick", "cancel"):
                if await self._route_media(media_ctx):
                    return

        if is_direct_at and deliberation.is_command and deliberation.command_type != "media":
            await self._handle_message_core(session_key, deliberation.as_command_str(), reply, user_openid=user_openid, media_context=media_ctx, deliberation=deliberation)
            return
        elif is_direct_at and (clean_text.startswith("/") or clean_text in ("帮助", "重置", "清空", "人设", "群记忆", "清空群记忆", "状态")):
            await self._handle_message_core(session_key, clean_text, reply, user_openid=user_openid, media_context=media_ctx, deliberation=deliberation)
            return

        if not clean_text:
            if is_direct_at:
                await reply("你好呀！请问有什么我可以帮你的？（输入 /help 查看帮助）")
            return

        session_key = self._chat_session_key(session_key)

        # 解析该群专属人设预设与主人称谓（群聊隔离）
        group_persona_name = self.app_config.bot.get_persona_name(group_openid) or self.personas.active_persona_name
        group_persona = persona_for(self, group_openid)
        group_master_name = self.app_config.bot.get_master_name(group_openid)
        robot = self.safe_robot
        bot_names = [group_persona.name]
        if robot and getattr(robot, "name", None):
            bot_names.append(robot.name)
        bot_nick = getattr(self.app_config.bot, "nickname", None)
        if bot_nick:
            bot_names.append(bot_nick)
        group_system_prompt = compose_system_prompt(
            base_prompt=group_persona.prompt,
            max_reply_length=self.app_config.bot.max_reply_length,
            enable_action_descriptions=self.app_config.bot.enable_action_descriptions,
            bot_names=bot_names,
            master_name=group_master_name
        )

        # 8. 组装增强型 Prompt（结合群近期上下文、专属记忆库、群聊节奏感知以及格式/长度限制）
        group_context_str = self.context_buffer.format_for_prompt(
            group_openid=group_openid,
            count=self.app_config.bot.context_message_count,
            exclude_last=True
        )
        image_context, image_delivery = await self._image_context_for_chat(session_key, group_openid,
            user_command_text, message_id=ref_id, current_message_id=msg_id if image_analyses else "")
        if image_context:
            group_context_str += "\n" + image_context
        memory_manifest = (
            await asyncio.to_thread(self.memory_mgr.get_memory_file_manifest, group_openid,
                                    history_count=self.app_config.bot.memory_history_count)
            if self.app_config.bot.memory_enabled else None
        )

        chat_rhythm = self.context_buffer.analyze_chat_rhythm(group_openid)

        # 记录交互轮次与每10次预设提醒
        if self.app_config.bot.memory_enabled:
            grp_mem = self.memory_mgr.get_group_memory(group_openid)
            turn = grp_mem.total_dialog_turns + 1
        else:
            turn = self._interaction_turn_counts.get(group_openid, 0) + 1
        self._interaction_turn_counts[group_openid] = turn
        reminder_interval = getattr(self.app_config.bot, "preset_reminder_interval", 10)
        include_preset = (turn == 1) or (turn % reminder_interval == 0)

        # 联网解析网络梗与生僻不明言语
        from request_routing import prepare_slang_context
        search_info = await prepare_slang_context(self, media_ctx, clean_text)

        composed_prompt = compose_user_prompt(
            user_text=clean_text,
            author_name=display_name,
            group_context=group_context_str,
            is_master=is_master,
            max_reply_length=self.app_config.bot.max_reply_length,
            enable_action_descriptions=self.app_config.bot.enable_action_descriptions,
            is_autonomous=is_autonomous,
            chat_rhythm=chat_rhythm,
            search_info=search_info,
            include_preset=include_preset,
            interaction_turn=turn,
            preset_reminder_interval=reminder_interval,
            incremental=True
        )

        try:
            logger.info(
                f"[Group {group_openid}] 发起 AI 生成: 发言者={display_name}, "
                f"自主={is_autonomous}, 消息={clean_text[:40]}..."
            )
            generation = session_generation(self.ai, session_key)
            async with self._reply_turn(session_key, media_ctx):
                composed_prompt += "\n" + image_context if image_context else ""
                reply_text = await self._main_chat(session_key, composed_prompt, system_prompt=group_system_prompt,
                                                media_context=media_ctx,
                                                image_delivery=image_delivery,
                                                memory_manifest=memory_manifest, **self._chat_options())
                if not generation_is_current(self.ai, session_key, generation):
                    return
                self._ack_image_context(session_key, image_delivery)

                # 解析大模型参聊决策与技能指令，避免向群内泄露原始 JSON
                from prompt_builder import parse_ai_decision_and_reply
                decision = parse_ai_decision_and_reply(
                    raw_response=reply_text,
                    default_should_reply=is_direct_at or is_master,
                    is_at_bot=is_direct_at,
                    is_master=is_master,
                    default_user_id=user_openid,
                    default_msg_id=""
                )
                if not decision.should_reply:
                    return

                spoken = await self._send_decision_messages(decision, media_ctx, group_persona.name)
                if spoken:
                    self._record_console_text(media_ctx, group_persona.name, spoken)
                    if self.app_config.bot.memory_enabled:
                        self.memory_mgr.record_interaction(group_openid, user_openid, clean_text, spoken, author_name=display_name)
                    self._last_autonomous_reply_time[group_openid] = time.time()
                    self._proactive_chat.note_reply(group_openid)

        except SessionInvalidatedError:
            return
        except AgentServiceError as error:
            logger.warning("[Group %s] 本轮 AI 服务不可用，未记录机器人回复。", group_openid)
            if (is_direct_at or is_master) and generation_is_current(self.ai, session_key, generation):
                await reply(error.user_message)
        except Exception as e:
            logger.error(f"[Group {group_openid}] 生成或发送群消息失败: {e}", exc_info=True)
            if is_direct_at or is_master:
                await reply("⚠️ 消息暂时处理失败，请稍后再试。")

    # ========================================================
    # 事件监听: QQ 机器人上线成功回调
    # ========================================================
    async def on_ready(self):
        self._get_main_coordinator().prepare_startup()
        if self._control_task is None or self._control_task.done():
            self._control_task = asyncio.create_task(self._control.watch(self))
        robot = self.safe_robot
        bot_name = getattr(robot, "name", "QQBot") if robot else "QQBot"
        logger.info("=" * 60)
        logger.info(f"🎉 QQ 官方开放平台机器人已成功建立连接并就绪！名称: 【{bot_name}】")
        logger.info("📌 【腾讯官方开放平台消息事件与权限重要说明】:")
        logger.info("1. 公域机器人(默认平台策略): 腾讯网关仅向机器人推送被 @ 的消息 (GROUP_AT_MESSAGE_CREATE)。")
        logger.info("   群成员普通发言不会触发事件，因此官方公域机器人无法检测非 @ 的群聊发言，自主插话受平台规则限制。")
        logger.info("2. 私域机器人: 需在腾讯 QQ 开放平台管理端申请并通过「私域」权限，方可监听全量群消息 (GROUP_MESSAGE_CREATE)。")
        logger.info("3. 免申请全量消息推荐: 建议在配置或 GUI 中切换运行模式为「本地 QQNT 个人小号 (OneBot)」，")
        logger.info("   基于本地 NapCat / LLOneBot 可无限制接收 100% 全量群聊发言与图片，完整激活自主接话互动！")
        logger.info("=" * 60)

    # ========================================================
    # 事件监听: QQ 群聊 @机器人 消息
    # ========================================================
    async def on_group_at_message_create(self, message: GroupMessage):
        self._queue_official_group(message, True)

    # ========================================================
    # 事件监听: QQ 群聊全量消息（公域/私域全量或无@插话）
    # ========================================================
    async def on_group_message_create(self, message: GroupMessage):
        self._queue_official_group(message, False)

    def _queue_official_group(self, message, is_at_event):
        from conversation_runtime import EventContext
        group = str(message.group_openid)
        user = str(getattr(message.author, "member_openid", "unknown"))
        robot = self.safe_robot
        own_ids = {str(self.app_config.qq.appid), str(getattr(robot, "id", "")),
                   str(getattr(robot, "member_openid", ""))}
        if user in own_ids or message.id in self._processed_msg_ids:
            return
        scope = f"official:{self.app_config.qq.appid}:group:{group}"
        event = EventContext(scope, str(message.id or ""), time.time(), owner=scope + ":" + user,
                             text=clean_mention_text(message.content or ""),
                             sender_name=self.get_author_display_name(group, user, message.author))
        from persona_isolation import effective_persona
        event.persona = effective_persona(self, group).model_copy(deep=True)
        self._observe_chat(scope, event.message_id, user, event.text, event.received_at,
                           is_bot=getattr(message.author, "bot", False) is True)
        if getattr(message.author, "bot", None) is not True:
            self._proactive_chat.observe(group, event.message_id, received_at=event.received_at)
        directed = bool(is_at_event or self.is_bot_mentioned(message) or self.app_config.bot.is_master(group, user)
                        or event.text.startswith(("/", "／")))
        if getattr(message, "attachments", None):
            self._emit_group_feed(group, user, self.get_author_display_name(group, user, message.author),
                message.id, clean_mention_text(message.content or ""), is_at_event or self.is_bot_mentioned(message), pending=True)
        def record():
            if message.id in self._processed_msg_ids:
                return
            self._processed_msg_ids.append(message.id)
            name = self.get_author_display_name(group, user, message.author)
            from persona_isolation import persona_scope
            with persona_scope(self, group, persona=event.persona):
                self.context_buffer.add_message(group, user, name, message.content or "[图片]", is_bot=False)
                if self.app_config.bot.memory_enabled:
                    self.memory_mgr.record_session_message(group, user, name, message.content or "[图片]", is_bot=False)
        from media_router import is_work_request, parse_media_intent, is_priority_control
        text = clean_mention_text(message.content or "")
        intent = parse_media_intent(text, self.app_config.media.default_image_count) if directed else None
        reference = getattr(message, "message_reference", None)
        reference_id = str(getattr(reference, "message_id", "") or "") if reference else ""
        cached_reference = self._recent_messages_cache.get(reference_id)
        has_reference = bool(reference and (not cached_reference or cached_reference.get("image_analyses")))
        runtime = self._get_runtime()
        if (runtime.buffer_seconds > 0 and not is_priority_control(text)
                and not (directed and is_work_request(text))):
            async def record_fragment():
                if getattr(message, "attachments", None) or has_reference:
                    await self._handle_group_message(message, is_at_event, _batch_record_only=True)
                else:
                    record()
            runtime.submit_chat(event, lambda: self._handle_group_message(message, is_at_event),
                                directed=directed, record_only=record_fragment, coalesce=True,
                                batchable=not text.startswith(("/", "／")))
            return
        if directed and is_priority_control(text):
            async def run_priority():
                event.lane = "chat"
                from conversation_runtime import current_event
                token = current_event.set(event)
                try:
                    await self._handle_group_message(message, is_at_event)
                finally:
                    current_event.reset(token)
            asyncio.create_task(run_priority())
            return
        if ((directed or getattr(message, "attachments", None) and self.app_config.bot.autonomous_enabled)
                and not is_priority_control(text) and (getattr(message, "attachments", None) or has_reference)):
            runtime.submit_work(event, text or "识别图片", lambda: self._handle_group_message(message, is_at_event))
        else:
            runtime.submit_chat(event, lambda: self._handle_group_message(message, is_at_event),
                                directed=directed, record_only=record,
                                batchable=not text.startswith(("/", "／")) and not is_work_request(text),
                                coalesce=not is_priority_control(text) and not (directed and is_work_request(text)))

    # ========================================================
    # 事件监听: QQ 好友 / 私聊 C2C 消息
    # ========================================================
    async def on_c2c_message_create(self, message: C2CMessage):
        from conversation_runtime import EventContext
        user = str(message.author.user_openid)
        scope = f"official:{self.app_config.qq.appid}:private:{user}"
        event = EventContext(scope, str(message.id or ""), time.time(), owner=scope + ":" + user,
                             text=clean_mention_text(message.content or ""))
        from persona_isolation import effective_persona
        event.persona = effective_persona(self, "").model_copy(deep=True)
        self._observe_chat(scope, event.message_id, user, clean_mention_text(message.content or ""), event.received_at,
                           is_bot=getattr(message.author, "bot", False) is True)
        from media_router import is_work_request, parse_media_intent, is_priority_control
        text = clean_mention_text(message.content or "")
        intent = parse_media_intent(text, self.app_config.media.default_image_count)
        runtime = self._get_runtime()
        if is_priority_control(text):
            async def run_priority_c2c():
                event.lane = "chat"
                from conversation_runtime import current_event
                token = current_event.set(event)
                try:
                    await self._process_c2c_message(message)
                finally:
                    current_event.reset(token)
            asyncio.create_task(run_priority_c2c())
            return
        reference = getattr(message, "message_reference", None)
        reference_id = str(getattr(reference, "message_id", "") or "") if reference else ""
        cached_reference = self._recent_messages_cache.get(reference_id)
        has_reference = bool(reference and (not cached_reference or cached_reference.get("image_analyses")))
        if not is_priority_control(text) and (getattr(message, "attachments", None) or has_reference):
            runtime.submit_work(event, text or "识别图片", lambda: self._process_c2c_message(message))
        else:
            runtime.submit_chat(event, lambda: self._process_c2c_message(message),
                                batchable=not text.startswith(("/", "／")) and not is_work_request(text))

    @persona_bound("private")
    async def _process_c2c_message(self, message: C2CMessage):
        from conversation_runtime import current_event
        event = current_event.get()
        received_at = (event.received_at if event else 0) or time.time()
        self._control.poll(self)
        self.sync_system_prompt()
        if getattr(message, "id", None):
            if message.id in self._processed_msg_ids:
                return
            self._processed_msg_ids.append(message.id)

        user_openid = message.author.user_openid
        session_key = f"c2c_{user_openid}"

        raw_content = message.content or ""
        self._observe_chat(self._image_scope(user_openid, False), message.id, user_openid,
                           clean_mention_text(raw_content), received_at,
                           is_bot=getattr(message.author, "bot", False) is True)
        user_command_text = strip_multimodal_tags(raw_content)
        image_analyses = []
        attachments = getattr(message, "attachments", None)
        if attachments:
            async def remember_image(image, index):
                await self._save_image_context(user_openid, message.id, [image], is_group=False,
                    sender_id=user_openid, timestamp=received_at, query=user_command_text, strict=True)
            enriched_content, image_analyses = await process_multimodal_message(
                content=raw_content,
                attachments=attachments, ai_client=self.ai, on_recognized=remember_image,
                vision_session_key=f"__work__:incoming:official:private:{user_openid}:{message.id}",
            )
            raw_content = enriched_content
            await self._save_image_context(user_openid, message.id, image_analyses, is_group=False,
                sender_id=user_openid, timestamp=received_at, query=user_command_text)

        ref = getattr(message, "message_reference", None)
        reference = str(getattr(ref, "message_id", "") or "") if ref else ""
        quoted = self._recent_messages_cache.get(reference, {})
        if reference and not quoted:
            quoted = await self._stored_image_reference(user_openid, reference, is_group=False) or {}
        if quoted.get("private_user_id") == user_openid:
            from request_routing import recognize_cached_images, image_reply_text
            quoted_images = await recognize_cached_images(quoted.get("image_analyses", []), self.ai,
                f"__work__:incoming:official:private:{user_openid}:{message.id}:quoted")
            image_analyses.extend(quoted_images)
            if quoted_images:
                raw_content += "\n[用户引用的图片，分支识别结果: " + image_reply_text(quoted_images) + "]"
        if getattr(message, "id", None):
            self._recent_messages_cache[str(message.id)] = {"private_user_id": user_openid,
                "image_analyses": list(image_analyses), "clean_text": raw_content}
            if len(self._recent_messages_cache) > 120:
                self._recent_messages_cache.pop(next(iter(self._recent_messages_cache)))

        async def reply(content: str, msg_seq: int = 1):
            try:
                await message.reply(content=content, msg_seq=msg_seq)
            except Exception as e:
                logger.error(f"私聊消息回复失败: {e}")
                raise

        media_ctx = self._make_media_context(session_key=session_key, user_id=user_openid,
            conversation_id=user_openid, message_id=message.id, text=user_command_text,
            reply=reply, quoted_images=image_analyses, received_at=received_at)
        media_ctx.quoted_message_id = reference
        self._record_official_attachments(message, media_ctx)
        await self._handle_message_core(session_key, raw_content, reply, user_openid=user_openid, media_context=media_ctx)

    # ========================================================
    # 事件监听: QQ 频道内 @机器人 消息
    # ========================================================
    async def on_at_message_create(self, message: Message):
        if getattr(message, "id", None):
            if message.id in self._processed_msg_ids:
                return
            self._processed_msg_ids.append(message.id)

        channel_id = message.channel_id
        user_id = message.author.id
        session_key = f"channel_{channel_id}_{user_id}"

        async def reply(content: str, msg_seq: int = 1):
            try:
                await message.reply(content=content)
            except Exception as e:
                logger.error(f"频道消息回复失败: {e}")
                raise

        await self._handle_message_core(session_key, message.content, reply, user_openid=str(user_id))

    # ========================================================
    # 事件监听: QQ 频道私信
    # ========================================================
    async def on_direct_message_create(self, message: DirectMessage):
        if getattr(message, "id", None):
            if message.id in self._processed_msg_ids:
                return
            self._processed_msg_ids.append(message.id)

        guild_id = message.guild_id
        user_id = message.author.id
        session_key = f"direct_{guild_id}_{user_id}"

        async def reply(content: str, msg_seq: int = 1):
            try:
                await message.reply(content=content)
            except Exception as e:
                logger.error(f"频道私信回复失败: {e}")
                raise

        await self._handle_message_core(session_key, message.content, reply, user_openid=str(user_id))

    async def graceful_stop(self) -> None:
        """机器人平滑停止退出流程，触发群记忆总结归档与多模态清理"""
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
        logger.info("🛑 [OfficialBot] 收到停止请求，正在执行退出清理...")
        self._cancel_all_media()
        await self._close_runtime()
        if self.app_config.bot.memory_enabled and hasattr(self, "memory_mgr"):
            try:
                logger.info("🛑 [OfficialBot] 机器人正在关闭，大模型开始整理本次会话群记忆与群人物刻写...")
                await self.memory_mgr.summarize_and_save_session(self.ai)
                self.memory_mgr.close_run()
            except Exception as me:
                logger.warning(f"关闭整理群记忆异常: {me}")
        try:
            await self.close()
        except Exception as ce:
            logger.warning(f"关闭 official client 异常: {ce}")
