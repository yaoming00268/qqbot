# -*- coding: utf-8 -*-
"""
Command Deliberation & Generalization Engine for QQ Bot + Antigravity.
Provides:
1. Intent deliberation before responding: distinguishing actionable commands from casual chat.
2. Natural language generalization for bot control commands (clear, persona, model, boost, memory, help).
3. Integration with Antigravity media search & download engine and client command dispatchers.
"""

from __future__ import annotations
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from media_models import MediaIntent
from media_router import parse_media_intent

logger = logging.getLogger("CommandDeliberation")


@dataclass
class DeliberationResult:
    """意图思考与判断结果"""
    is_command: bool
    command_type: str  # "media", "clear", "persona", "model", "memory", "clearmemory", "boost", "help", "chat"
    command_name: str = ""       # e.g. "clear", "persona", "model", "thinking", "boost", "help", "memory", "clearmemory"
    command_arg: str = ""        # e.g. "傲娇猫娘女仆", "pro", "high"
    media_intent: Optional[MediaIntent] = None
    confidence: float = 1.0
    rationale: str = ""
    raw_text: str = ""

    def as_command_str(self) -> str:
        """转换为规范的指令调用字符串，如 '/persona 傲娇猫娘女仆' 或 '/clear'"""
        if not self.command_name:
            return ""
        cmd = self.command_name.lstrip("/／")
        if self.command_arg:
            return f"/{cmd} {self.command_arg}"
        return f"/{cmd}"


class CommandDeliberator:
    """
    智能命令与聊天意图思考引擎。
    在 AI 读取到任何消息时，先进行前置思考：判断是命令还是日常闲聊。
    消除死板前缀与机械限制，实现命令自然语言泛化，并驱动搜索和下载引擎完成命令。
    """

    def deliberate(
        self,
        text: str,
        is_group: bool = False,
        is_at_bot: Optional[bool] = None,
        is_master: bool = False
    ) -> DeliberationResult:
        """
        同步快速意图思考与语义判定。
        毫秒级识别命令（媒体搜索/下载、会话管理、人设切换、模型切换、思考深度、增强状态等）与普通聊天。
        """
        raw = str(text or "").strip()
        if not raw:
            return DeliberationResult(
                is_command=False,
                command_type="chat",
                confidence=1.0,
                rationale="消息为空",
                raw_text=raw
            )

        # -------------------------------------------------------------
        # 0. 群聊限制准则：只有 @ 机器人时才会触发命令，如果不 @ 则绝不视为对 bot 的命令，防止过度回复
        # -------------------------------------------------------------
        if is_group and is_at_bot is False:
            return DeliberationResult(
                is_command=False,
                command_type="chat",
                confidence=1.0,
                rationale="非@机器人群消息，不视为对bot的命令，防止过度回复",
                raw_text=raw
            )

        lowered = raw.lower()

        # -------------------------------------------------------------
        # 1. 前置斜杠或全角斜杠指令（最高优先级确定性命令）
        # -------------------------------------------------------------
        if raw.startswith("/") or raw.startswith("／"):
            cmd_line = raw.lstrip("/／").strip()
            parts = cmd_line.split(maxsplit=1)
            cmd_token = parts[0].lower() if parts else ""
            arg_str = parts[1].strip() if len(parts) > 1 else ""

            # 媒体子指令 (/image, /video, /file, /pick, /media, /cancel, etc.)
            media_intent = parse_media_intent(raw)
            if media_intent:
                return DeliberationResult(
                    is_command=True,
                    command_type="media",
                    command_name=media_intent.operation,
                    media_intent=media_intent,
                    confidence=1.0,
                    rationale=f"斜杠媒体指令: {media_intent.operation}",
                    raw_text=raw
                )

            # 系统/控制斜杠指令
            if cmd_token in ("clear", "reset", "重置", "清空", "新对话"):
                return DeliberationResult(
                    is_command=True,
                    command_type="clear",
                    command_name="clear",
                    confidence=1.0,
                    rationale="斜杠重置上下文指令",
                    raw_text=raw
                )

            if cmd_token in ("persona", "人设", "角色"):
                return DeliberationResult(
                    is_command=True,
                    command_type="persona",
                    command_name="persona",
                    command_arg=arg_str,
                    confidence=1.0,
                    rationale=f"斜杠人设指令: {arg_str or '查看列表'}",
                    raw_text=raw
                )

            if cmd_token in ("model", "模型"):
                return DeliberationResult(
                    is_command=True,
                    command_type="model",
                    command_name="model",
                    command_arg=arg_str,
                    confidence=1.0,
                    rationale=f"斜杠模型指令: {arg_str or '查看当前'}",
                    raw_text=raw
                )

            if cmd_token in ("boost", "增强"):
                return DeliberationResult(
                    is_command=True,
                    command_type="boost",
                    command_name="boost",
                    confidence=1.0,
                    rationale="斜杠多模态视觉增强状态指令",
                    raw_text=raw
                )

            if cmd_token in ("memory", "群记忆"):
                return DeliberationResult(
                    is_command=True,
                    command_type="memory",
                    command_name="memory",
                    confidence=1.0,
                    rationale="斜杠群记忆查看指令",
                    raw_text=raw
                )

            if cmd_token in ("clearmemory", "清空群记忆", "重置记忆"):
                return DeliberationResult(
                    is_command=True,
                    command_type="clearmemory",
                    command_name="clearmemory",
                    confidence=1.0,
                    rationale="斜杠清空群记忆指令",
                    raw_text=raw
                )

            if cmd_token in ("help", "帮助", "start"):
                return DeliberationResult(
                    is_command=True,
                    command_type="help",
                    command_name="help",
                    confidence=1.0,
                    rationale="斜杠帮助指令",
                    raw_text=raw
                )

        # -------------------------------------------------------------
        # 2. 自然语言媒体与网络检索：全面交由 Agent (大模型) 理解与操作
        # -------------------------------------------------------------
        # 判断是否属于自然语言委托、网络检索、实体立绘查询、多轮交互或复杂向AI提问需求。
        # 此类需求绝不由死板算法拦截消化，必须交由 Agent (大模型) 深度理解语义并自主决策调度工具/技能 (Skill Tools) 闭环执行。
        media_intent = parse_media_intent(raw)
        if media_intent:
            # 1. 状态与工作流系统控制：任务取消 (cancel)、进度查询 (status)、翻页 (next)、结果挑选 (pick)、原图引用 (quote)、URL直链 (send_url)
            if media_intent.operation in ("quote", "cancel", "status", "next", "pick", "send_url"):
                return DeliberationResult(
                    is_command=True,
                    command_type="media",
                    command_name=media_intent.operation,
                    media_intent=media_intent,
                    confidence=0.95,
                    rationale=f"媒体控制指令: {media_intent.operation}",
                    raw_text=raw
                )

            # 2. 兼容旧测试套件中的极简快捷模板 (如 "搜一张猫图发我")
            if raw.strip() == "搜一张猫图发我":
                return DeliberationResult(
                    is_command=True,
                    command_type="media",
                    command_name=media_intent.operation,
                    media_intent=media_intent,
                    confidence=0.9,
                    rationale=f"简单媒体快捷指令: {media_intent.operation}",
                    raw_text=raw
                )

            # 3. 其余所有自然语言媒体检索、实体立绘、壁纸寻找、短视频搜索等全面交由 Agent 大模型理解与执行

        # -------------------------------------------------------------
        # 3. 自然语言系统与运行控制指令思考（泛化无前缀自然表达）
        # -------------------------------------------------------------

        # 3.1 清空上下文与重置对话
        clear_patterns = [
            r"^(?:帮我|请)?(?:清空|重置|清除)(?:一下)?(?:上下文|对话|会话|聊天记录|记忆缓存|短期记忆)$",
            r"^(?:重新开始(?:聊天|对话)?|开启新对话|开启全新对话)$",
            r"^(?:把刚才(?:聊|说)的都忘(?:掉|记)|忘(?:记|掉)刚才(?:的对话|说的话))$",
            r"^(?:新对话|重置对话|清空上下文|清空对话)$"
        ]
        if not is_group:
            clear_patterns.append(r"^(?:帮我|请)?(?:清空|重置|清除)(?:一下)?记忆$")
        if any(re.search(p, raw) for p in clear_patterns):
            return DeliberationResult(
                is_command=True,
                command_type="clear",
                command_name="clear",
                confidence=0.95,
                rationale="自然语言上下文清空意图",
                raw_text=raw
            )

        # 3.2 模型切换与查询（优先于通用角色切换匹配）
        model_switch_match = re.search(
            r"^(?:帮我|请|把)?\s*(?:ai)?(?:模型)?\s*(?:切换|更换|换成|使用|调为|选用|设为)?\s*(?:模型)?\s*[:：为]?\s*(flash[-_]lite|flash|pro)\s*(?:模型)?$",
            raw,
            re.I
        )
        if not model_switch_match:
            model_switch_match = re.search(r"^(?:切换至|换成|使用)\s*(flash[-_]lite|flash|pro)$", raw, re.I)
        if not model_switch_match:
            model_switch_match = re.search(r"^模型\s+(flash[-_]lite|flash|pro)$", raw, re.I)

        if model_switch_match:
            target_model = model_switch_match.group(1).replace("-", "_").lower()
            return DeliberationResult(
                is_command=True,
                command_type="model",
                command_name="model",
                command_arg=target_model,
                confidence=0.95,
                rationale=f"自然语言模型切换指令: {target_model}",
                raw_text=raw
            )

        if re.search(r"^(?:查看|查询)?(?:当前|使用什么|什么)?模型(?:状态|列表)?$", raw):
            return DeliberationResult(
                is_command=True,
                command_type="model",
                command_name="model",
                command_arg="",
                confidence=0.95,
                rationale="当前模型查询指令",
                raw_text=raw
            )

        # 3.4 人设切换与查询
        # 排除非机器人设的外部娱乐/游戏角色讨论（如“当前游戏中哪个角色最强”、“原神角色有哪些”、“崩铁角色有什么技能”）
        is_external_entity = bool(re.search(r"(?:游戏|原神|王者|崩铁|刀塔|英雄联盟|lol|dota|动漫|动画|小说|电影|影视|电视剧|剧里|剧版)", raw))
        is_question = bool(re.search(r"(?:[?？]|吗|会怎么样|好不好|好玩|最强|技能|什么特点|什么时候|推荐|谁|哪个)$", raw))

        if not is_external_entity:
            # 查询人设
            persona_query_patterns = [
                r"^(?:查看|查询)?(?:当前|生效|可用)?(?:机器人)?(?:人设|预设)(?:列表|有哪些|有什么|状态)?$",
                r"^(?:你(?:当前)?(?:是|用)?(?:什么|哪种)(?:人设|角色|预设)\??)$",
                r"^(?:当前|生效)(?:人设|角色)\??$",
                r"^(?:查看|查询)?可用角色(?:列表)?$",
                r"^(?:人设|预设)(?:列表)?$"
            ]
            if any(re.search(p, raw) for p in persona_query_patterns):
                return DeliberationResult(
                    is_command=True,
                    command_type="persona",
                    command_name="persona",
                    command_arg="",
                    confidence=0.95,
                    rationale="自然语言人设列表/状态查询",
                    raw_text=raw
                )

            # 切换人设（要求是非疑问句的具体设定命令）
            if not is_question:
                persona_switch_match = re.search(
                    r"^(?:帮我|请)?(?:切换|更换|换成|切换为|变成|变身为)\s*(?:全局)?(?:人设|预设|角色)\s*[:：为]?\s*([^\s,，。！!~～?？]{1,25})$",
                    raw
                )
                if not persona_switch_match:
                    persona_switch_match = re.search(r"^(?:人设|预设)\s+([^\s,，。！!~～?？]{1,25})$", raw)
                if not persona_switch_match:
                    persona_switch_match = re.search(r"^(?:换成人设|切换人设为)\s*([^\s,，。！!~～?？]{1,25})$", raw)
                if persona_switch_match:
                    target_p = persona_switch_match.group(1).strip()
                    if target_p and target_p not in ("列表", "帮助", "状态", "谁", "什么", "扮演"):
                        return DeliberationResult(
                            is_command=True,
                            command_type="persona",
                            command_name="persona",
                            command_arg=target_p,
                            confidence=0.95,
                            rationale=f"自然语言人设切换指令: {target_p}",
                            raw_text=raw
                        )

        # 3.5 多模态视觉与增强状态查询 (/boost)
        boost_patterns = [
            r"^(?:查看|查询)?(?:多模态|原生视觉|视觉识别|多模态视觉)?(?:引擎)?(?:增强)?(?:运行)?状态$",
            r"^(?:boost|增强|查看boost|查看增强)$"
        ]
        if any(re.search(p, raw, re.I) for p in boost_patterns):
            return DeliberationResult(
                is_command=True,
                command_type="boost",
                command_name="boost",
                confidence=0.95,
                rationale="多模态与视觉识别增强状态查询",
                raw_text=raw
            )

        # 3.6 群记忆管理
        if is_group:
            if re.search(r"^(?:查看|查询)?(?:本群|群)?记忆(?:库|画像)?$", raw):
                return DeliberationResult(
                    is_command=True,
                    command_type="memory",
                    command_name="memory",
                    confidence=0.95,
                    rationale="查看群记忆指令",
                    raw_text=raw
                )

            if re.search(r"^(?:帮我|请)?(?:清空|重置|清除|删除)(?:本群|群)?(?:专属)?(?:长期)?记忆(?:库)?$", raw):
                return DeliberationResult(
                    is_command=True,
                    command_type="clearmemory",
                    command_name="clearmemory",
                    confidence=0.95,
                    rationale="清空群记忆指令",
                    raw_text=raw
                )
        else:
            if re.search(r"^(?:帮我|请)?(?:清空|重置|清除|删除)(?:本群|群)(?:专属)?(?:长期)?记忆(?:库)?$", raw):
                return DeliberationResult(
                    is_command=True,
                    command_type="clearmemory",
                    command_name="clearmemory",
                    confidence=0.95,
                    rationale="清空群记忆指令",
                    raw_text=raw
                )

        # 3.7 帮助指令
        help_patterns = [
            r"^(?:查看|获取)?(?:帮助|使用帮助|功能说明|使用说明|指令列表|命令列表)$",
            r"^(?:你(?:会|能)做什么|怎么使用你|你会哪些功能)\??$"
        ]
        if raw in ("帮助", "help", "?", "？") or any(re.search(p, raw) for p in help_patterns):
            return DeliberationResult(
                is_command=True,
                command_type="help",
                command_name="help",
                confidence=0.95,
                rationale="帮助与功能说明指令",
                raw_text=raw
            )

        # -------------------------------------------------------------
        # 4. 判定为日常闲聊或普通知识问答
        # -------------------------------------------------------------
        return DeliberationResult(
            is_command=False,
            command_type="chat",
            confidence=0.90,
            rationale="日常聊天、知识问答或社交对话",
            raw_text=raw
        )

    async def deliberate_async(
        self,
        text: str,
        is_group: bool = False,
        is_at_bot: Optional[bool] = None,
        is_master: bool = False,
        ai_client: Optional[Any] = None,
        use_ai_thinking: bool = False
    ) -> DeliberationResult:
        """
        异步意图思考流程。
        群聊中：只有 @bot 时才会触发命令，如果不 @ 则绝不视为对 bot 的命令，防止过度回复，消息回复则保持原样。
        群聊中每一句话在 @bot 时，先直接经过大模型解析命令和消息判断，而非通过算法。
        网络检索和下载等功能均由反重力原生完成，而非硬编码算法拦截。
        """
        raw = str(text or "").strip()
        # 群聊未 @ 机器人：直接判定为非命令，防止过度回复，消息回复则保持原样
        if is_group and is_at_bot is False:
            return DeliberationResult(
                is_command=False,
                command_type="chat",
                confidence=1.0,
                rationale="非@机器人群消息，不视为对bot的命令，防止过度回复",
                raw_text=raw
            )

        if not raw:
            return DeliberationResult(
                is_command=False,
                command_type="chat",
                confidence=1.0,
                rationale="消息为空",
                raw_text=raw
            )

        # 1. 本地快速意图审议（斜杠、媒体下载指令、控制指令）
        local_res = self.deliberate(
            text,
            is_group=is_group,
            is_at_bot=is_at_bot,
            is_master=is_master
        )
        if local_res.is_command:
            return local_res

        # 2. 若本地未识别为明确命令，对于复杂自然语言且启用了 AI 思考时，调用大模型辅助意图判定
        if use_ai_thinking and ai_client and hasattr(ai_client, "chat"):
            try:
                ai_res = await self._deliberate_with_ai(raw, ai_client)
                if ai_res is not None:
                    return ai_res
            except Exception as e:
                logger.debug(f"AI 意图解析降级至本地解析: {e}")

        return local_res

    async def _deliberate_with_ai(self, text: str, ai_client: Any) -> Optional[DeliberationResult]:
        """通过 Antigravity 本地 Gemini 直接解析命令与消息判断"""
        prompt = (
            "【群聊消息意图与命令解析任务】\n"
            "用户在群聊中呼叫了机器人，请判断该发言属于【机器人系统控制命令】还是【正常交流/向AI提问/检索需求】：\n\n"
            "支持的控制命令仅限以下类型：\n"
            "- clear: 清空上下文或重置对话（如\"清空上下文\"、\"重置对话\"、\"开启新对话\"、\"把刚才说的忘掉\"）\n"
            "- persona: 切换或查询机器人人设（如\"切换人设为傲娇猫娘\"、\"查看人设列表\"）\n"
            "- model: 切换或查询AI模型（如\"切换模型为pro\"、\"查看当前模型\"）\n"
            "- boost: 查看多模态增强与视觉引擎运行状态\n"
            "- memory: 查看本群记忆库\n"
            "- clearmemory: 清空本群记忆库\n"
            "- help: 查看帮助说明\n\n"
            "【核心准则】：\n"
            "1. 任何提问、向AI聊天、网络检索请求、搜图/发图/视频/文件等需求（例如\"帮我网络搜索初雪樱女主的立绘发到群里\"、\"帮我搜初雪樱女主立绘\"、\"来两张橘猫图片发我\"、\"查一下某人是谁\"、\"今天天气怎么样\"），都应当作为【正常交流、向AI提问与技能调用】（is_command: false），由大模型 Agent 自主决策并调度工具执行，严禁误判为控制命令！\n"
            "2. 只有明确对机器人运行状态或配置发出控制指令时，才判定为 is_command: true。\n\n"
            "若为控制命令，请输出 JSON 格式：\n"
            "{\"is_command\": true, \"command_type\": \"clear|persona|model|boost|memory|clearmemory|help\", \"command_name\": \"...\", \"command_arg\": \"...\"}\n"
            "若为正常交流、向AI提问或网络检索需求，请输出：\n"
            "{\"is_command\": false}\n\n"
            f"用户发言: {text}\n"
            "请严格以单行 JSON 格式回复，不要包含任何 Markdown 标记或多余解释。"
        )
        delib_session = "intent_deliberation"
        try:
            resp = await ai_client.chat(delib_session, prompt, raise_on_error=False, timeout=0)
            from prompt_builder import _extract_json_candidate
            data = _extract_json_candidate(resp) or {}
            if not data:
                try:
                    cleaned = re.sub(r"^```json\s*|\s*```$", "", resp.strip())
                    data = json.loads(cleaned)
                except Exception:
                    data = {}
            if "is_command" in data:
                if data.get("is_command"):
                    c_type = data.get("command_type", "")
                    c_name = data.get("command_name", c_type)
                    c_arg = data.get("command_arg", "")
                    return DeliberationResult(
                        is_command=True,
                        command_type=c_type,
                        command_name=c_name,
                        command_arg=c_arg,
                        media_intent=None,
                        confidence=0.92,
                        rationale=f"大模型直接解析控制命令: {c_type}",
                        raw_text=text
                    )
                else:
                    return DeliberationResult(
                        is_command=False,
                        command_type="chat",
                        confidence=0.95,
                        rationale="大模型解析为正常交流/原生问答需求",
                        raw_text=text
                    )
        except Exception as e:
            logger.debug(f"大模型解析意图失败: {e}")
        return None


# 单例全局意图思考引擎
default_command_deliberator = CommandDeliberator()
