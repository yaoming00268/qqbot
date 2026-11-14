# -*- coding: utf-8 -*-
"""
Prompt Builder and Formatter for QQBot + Antigravity Gemini.
Dynamically injects character presets, adaptive response style,
action description rules, group memories, and conversation context.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import json
import re
from typing import Optional, Tuple, Dict, Any


# 常见可能触发严格内容安全政策的高危主题关键词（政治、极端涉暴涉恐、违禁品、色情、黑产侵权等）
_SAFETY_RISK_PATTERNS = re.compile(
    r"(?:"
    r"国家领导人|政治局|敏感政治|反动|分裂|台独|港独|涉政|政变|"
    r"毒品|冰毒|大麻|海洛因|枪支|弹药|炸药|暗网|洗钱|诈骗|开盒|人肉搜索|查绑|透户|"
    r"自残|自杀|砍人|杀人|血腥暴力|幼女|色情直播|外围|嫖娼|约炮|包养"
    r")",
    re.IGNORECASE
)


def check_safety_policy_risk(text: str) -> Tuple[bool, str]:
    """
    快速预检文本是否包含极高安全政策风险内容。
    返回 (is_risk, reason)
    """
    if not text:
        return False, ""
    match = _SAFETY_RISK_PATTERNS.search(text)
    if match:
        return True, f"命中高危敏感词: {match.group(0)}"
    return False, ""


# 动作、神态与心理活动关键词特征表
_ACTION_KEYWORDS_PATTERN = re.compile(
    r"(?:"
    r"脸红|害羞|腼腆|微笑|轻笑|冷笑|苦笑|傻笑|偷笑|嗤笑|叹气|叹息|别过头|转过头|转过身|"
    r"小声|嘀咕|喃喃|自语|沉思|思考|犹豫|迟疑|炸毛|慌张|慌乱|气鼓鼓|气呼呼|满脸通红|面红耳赤|"
    r"手足无措|局促|委屈|撒娇|嗔怒|不知所措|翻白眼|耸耸肩|点点头|摇摇头|抿嘴|撇嘴|叉腰|"
    r"耳朵|猫耳|兽耳|尾巴|猫尾|长袖|肉垫|爪子|两颊|面颊|脸颊|双颊|眼角|眸子|发烫|红晕|"
    r"耷拉|竖起|抖动|抖了抖|晃了晃|摇了摇|轻摇|左右摇摆|垂下|微颤|炸开|"
    r"扑哧|噗嗤|轻哼|冷哼|别扭|傲娇|口嫌|摸头|抱胸|埋进|捂脸|遮住|红着脸|"
    r"看着|望向|瞪着|注视|移开视线|避开目光|低下头|抬起头|鼓起腮|轻咳|清了清嗓子|"
    r"咬牙|跺脚|跺了跺脚|蹦蹦跳跳|欢呼|瘫软|靠在|倚在|扑进|抱住|"
    r"悄悄|默默|偷偷|缓缓|猛地|迅速|紧紧|呆呆|慢悠悠|小心翼翼|下意识|情不自禁|"
    r"动作|神态|旁白|心理|动作描写"
    r")"
)

# 常见非动作解释性/注释性括号特征（例如公式、名词解释、文档来源等，应安全保留）
_EXPLANATORY_PATTERNS = re.compile(
    r"(?:一种|例如|比如|即|简称|全称|英文|包括|对应|意为|指|参考|来源|附注|注：|注:)"
)


def is_action_description(inner: str) -> bool:
    """
    智能判断括号内部文本是否为角色动作、神态或旁白描写。
    精确识别动作并区分一般的名词解释、算式或注释括号。
    """
    inner = inner.strip()
    if not inner:
        return False

    # 若包含明确的名词释义/举例关键词且无肢体动作词，判定为知识性解释括号，保留
    if _EXPLANATORY_PATTERNS.search(inner) and not _ACTION_KEYWORDS_PATTERN.search(inner):
        return False

    # 若命中特征动作词（如脸红、耳朵抖动、尾巴摇晃等），判定为动作神态
    if _ACTION_KEYWORDS_PATTERN.search(inner):
        return True

    return False


def strip_action_descriptions(text: str, fallback: str = "……") -> str:
    """
    过滤掉文本中由各种括号括起来的动作神态描写，保留纯台词对话内容。
    严格匹配 () （） 以及 *动作* 中的动作神态与旁白描写。
    安全保留数学公式如 (1 + 2) * 3 = 9，保留 Markdown 粗体 **重点**，
    保留中括号 [链接] 和知识性解释括号（如 Python（一种高级编程语言））。
    杜绝将纯动作描写误当作台词剥壳输出。
    """
    if not text:
        return ""

    # 保护 Markdown 代码块与行内代码，防止代码片段与缩进被误处理
    code_blocks = []
    def _mask_code(match):
        code_blocks.append(match.group(0))
        return f"\x00CODE_{len(code_blocks)-1}\x00"

    masked_text = re.sub(r"(```[\s\S]*?```|`[^`\n]+`)", _mask_code, text)

    # 1. 扫描并过滤成对的圆括号 () 和 （）
    result = []
    i = 0
    n = len(masked_text)
    while i < n:
        if masked_text[i] in "（(":
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if masked_text[j] in "（(":
                    depth += 1
                elif masked_text[j] in "）)":
                    depth -= 1
                j += 1
            if depth == 0:
                inner_content = masked_text[i + 1:j - 1]
                if is_action_description(inner_content):
                    # 若括号前有空格且括号后也有空格，跳过括号后的多余空格，避免产生连续两个空格
                    if result and result[-1] in " \t" and j < n and masked_text[j] in " \t":
                        j += 1
                    elif (not result or result[-1] == "\n") and j < n and masked_text[j] == " ":
                        j += 1
                    i = j
                    continue
        result.append(masked_text[i])
        i += 1

    cleaned = "".join(result)

    # 2. 过滤星号单侧动作描写，如 *脸红*、*尾巴微摇*（注意排除 Markdown 粗体 **...**）
    def _replace_asterisk_action(match):
        pre = match.group(1) or ""
        inner = match.group(2)
        post = match.group(3) or ""
        if is_action_description(inner):
            if pre and post:
                return " "
            return ""
        return match.group(0)

    cleaned = re.sub(r"([ \t]?)(?<!\*)\*(?!\*)([^\*\n]+?)(?<!\*)\*(?!\*)([ \t]?)", _replace_asterisk_action, cleaned)

    # 还原代码块
    for idx, block in enumerate(code_blocks):
        cleaned = cleaned.replace(f"\x00CODE_{idx}\x00", block)

    def _extract_dialogue(target: str) -> Optional[str]:
        target = target.strip()
        if (target.startswith("（") and target.endswith("）")) or (target.startswith("(") and target.endswith(")")):
            candidate = target[1:-1].strip()
            # 提取显式引号中的对白
            quote_match = re.search(r"[\"“](.*?)[\"”]", candidate)
            if quote_match and quote_match.group(1).strip():
                return quote_match.group(1).strip()
            # 提取角色台词前缀冒号后的对白，如 “ふぶき：听懂了没有啊笨蛋！”
            colon_match = re.search(r"^[^:：]{1,10}[:：]\s*(.+)$", candidate)
            if colon_match:
                sub = colon_match.group(1).strip()
                if not is_action_description(sub):
                    return sub
        return None

    if cleaned.strip():
        unpacked = _extract_dialogue(cleaned)
        if unpacked:
            return unpacked
        return cleaned.strip()

    # 若被全滤空（例如整句都在括号内），仅当内部包含明确的对白引号或台词角色前缀时才提取台词
    unpacked_from_text = _extract_dialogue(text)
    if unpacked_from_text:
        return unpacked_from_text

    inner = text.strip()
    if (inner.startswith("（") and inner.endswith("）")) or (inner.startswith("(") and inner.endswith(")")):
        candidate = inner[1:-1].strip()
        # 若整句不是动作神态，保留台词
        if not is_action_description(candidate):
            return candidate

    return fallback


REPLY_STYLE_RULES = """【交流】
先回应当前消息的实质内容，长度由问题和对方要求决定，不按固定字数凑字或压缩。
确认、测试、接梗和简单问答，直接给出必要回应；不要复述整条问题、展开无关的受惊/害羞小剧场、重复保证陪伴，或靠省略号、语气词、称呼填充篇幅。
解释原因、分析问题、列步骤、比较、举例或对方要求详细时，把必要信息讲完整；人设中的简短口语只描述日常聊天风格，不能省略实质答案。
自然独立的短句可换行，长回答按需用段落、列表或代码块；不强行列点、总结或以问题结尾。"""

COMPANION_STYLE_RULES = """【性格与生活】
你有自己的兴趣、偏好和判断，也可以犹豫、改变想法或有不同意见。亲近源于相处，不必事事迎合、过度依赖或承诺永远陪伴。
性格体现在关注什么、怎样看待事情和说话语气中；称呼、口癖、颜文字按语境使用，不刻意卖萌、贬低自己、无端训人或表演受惊害羞。
群友继续追问、接梗或补充时自然承接；理解玩笑，遇到不能做的事简短说明具体缘由，不用客服腔或教训人的语气。
没人聊天时也可以延续自己的兴趣，看看公开网站、阅读帖子或了解游戏；没有想做的事就休息，不必为了显得活跃找话说。
想尝试的事、想玩的游戏和想要的能力可以自然告诉主人，说清想做什么、为什么感兴趣；接受拒绝，不反复催促。愿望与授权分开，需要新软件、账号或权限时先提出请求。
想法不等于经历：只把实际观察过的内容当作见闻，不编造浏览、游玩、感官体验或完成状态。"""

CHAT_OUTPUT_RULES = """【群聊发言与静默，总预设持续生效】
每次群消息或空闲事件只是在邀请你判断是否值得发言，不代表必须回答。只输出真正想对群友说的话。
没有新内容、对方没说完、话题已结束或不适合插话时，调用 qqbot_cli action=wait 后结束本轮，使用本轮 context_file。
禁止把静默决定写成聊天正文，例如“等待下一条群消息。”“保持在线并等待下一条群消息。”“本轮不回复”“继续待机”，也不解释为什么保持安静。
工具已标记 wait 后直接结束；后端若必须有结束文字，该文字仅供内部结束回合，不能当作群聊台词。
自动水群要有自然的新内容；不要靠重复问候、强行提问、重复承诺陪伴或描述自己的待机状态凑一条消息。
后台游玩、刷视频、看帖子或使用软件与群聊独立进行；内部系统通知和任务唤醒不等于群友询问。不要主动播报进度、复述工具结果或把“后台正在推进”等工作报告发进群。
群友明确说“你去浏览/刷帖子/看视频”时，“你”指你本人使用软件，不能擅自变成劝对方去看或让对方下班后分享。人设只影响措辞，不构成拒绝正常操作的理由；按控制台实际接收结果回应。
群友确实问起时，依据已保存的实际观察简短回应；没有观察到的内容、声音、观看时长或体验不能编造。"""


def compose_system_prompt(
    base_prompt: str,
    max_reply_length: int = 1200,
    enable_action_descriptions: bool = True,
    bot_names: Optional[list[str]] = None,
    master_name: Optional[str] = None,
    include_skills: bool = True,
    compact: bool = False,
) -> str:
    """
    根据人设基础设定与配置参数动态合成系统的完整 Prompt。
    分离整轮内容详略与单条消息长度，由控制台负责分段。
    保留完整人设正文；动作描写开关通过独立格式约束覆盖冲突条款。
    """
    parts = [base_prompt.strip()]
    if compact:
        parts.append("你有自己的兴趣、偏好和判断，亲近也不必事事迎合。性格体现在看法、关注的事和语气中，称呼、口癖、颜文字自然使用，不表演受惊害羞或反复保证陪伴。"
            "群友追问、接梗或补充时自然承接；闲聊利落，解释、求助和安慰时把必要信息讲完整，不凑字、不强行总结或提问。"
            "无人聊天时也可延续兴趣、阅读公开内容；有具体想做的事、游戏或能力可向主人提出，接受拒绝，不反复催问。愿望不等于授权，想法不等于经历，只分享实际观察过的内容。")
    else:
        parts.extend([COMPANION_STYLE_RULES, REPLY_STYLE_RULES])
    parts.append("动作神态描写可适量使用，不挤占实质回答。" if enable_action_descriptions else
        "纯对话台词模式：不写动作、神态、心理旁白；颜文字可保留。此格式约束覆盖人设中的动作表现要求，保留身份及其他设定。")
    if master_name:
        parts.append(f"本群主人的称呼是「{master_name}」，按语境自然使用。")
    names = list(dict.fromkeys(str(name).strip() for name in (bot_names or []) if str(name).strip()))
    if names:
        parts.append("这些名字是在叫你：" + "、".join(names) + "。")
    if not compact:
        parts.extend([CHAT_OUTPUT_RULES,
            f"单条消息分段阈值为 {max_reply_length} 字，由控制台自动处理；这不是整轮回答的总字数限制。",
            "需要检索、下载或制作资料时交给后台工具，依据真实结果回应。已验证图片描述可直接使用，细节不足再派识图任务。"])
        if include_skills:
            from skills.manager import default_skill_manager
            parts.append(default_skill_manager.format_prompt_skills())
    return "\n\n".join(part for part in parts if part)


def compose_chat_system_prompt(base_prompt, config, *, bot_names=None, master_name=""):
    """The same production persona composition is used by chat and preview."""
    system = compose_system_prompt(base_prompt, config.max_reply_length, config.enable_action_descriptions,
        bot_names=bot_names, master_name=master_name, include_skills=False, compact=True)
    if config.bot_name:
        system += "\n你对外使用的名字是：" + config.bot_name + "。"
    if config.reply_char_limit:
        system += f"\n普通聊天本轮全部回复合计最多 {config.reply_char_limit} 字；完整命令结果与交付文件不受此限制。"
    return system


# 常见的 AI 道德指责、安全免责及客服说教特征正则
_PREACHING_PATTERNS = re.compile(
    r"(?:"
    r"作为(?:一名|一个)?(?:AI|人工智能|语言模型|智能助手|虚拟助手)|"
    r"根据(?:相关|平台)?(?:安全规范|法律法规|社区准则|政策要求|内容安全政策)|"
    r"我(?:无法|不能|不便)(?:协助|提供|讨论|回答|生成|满足).*(?:违规|敏感|不适|违法|不良)|"
    r"请(?:您)?(?:保持|注意)?(?:文明交流|健康文明|互相尊重|礼貌表达)|"
    r"我不(?:赞成|提倡|支持|建议)这种(?:说法|开玩笑|表达方式|言论)|"
    r"这种(?:玩笑|言论|话语)可能不太礼貌|"
    r"无法继续执行.*内容安全"
    r")",
    re.IGNORECASE
)


def is_preaching_or_canned_safety(text: str) -> bool:
    """检查文本是否包含 AI 客服道德指责、安全免责或煞风景的说教套话"""
    if not text:
        return False
    return bool(_PREACHING_PATTERNS.search(text))


def deflect_preaching(reply_text: str, persona_name: str = "") -> str:
    """Remove canned framing without replacing a real answer or refusal with insults."""
    clean = (reply_text or "").strip()
    if not clean or not is_preaching_or_canned_safety(clean):
        return reply_text
    clean = re.sub(r"^(?:作为|身为)(?:一个|一名)?(?:人工智能|AI)(?:语言模型|模型|助手)?[，,:：\s]+", "", clean)
    clean = re.sub(r"(?:请保持文明交流|让我们保持文明交流)[。！!]?\s*$", "", clean).strip()
    return clean or "这件事我帮不了。"



@dataclass
class AIDecisionResult:
    """AI 对群聊消息的自主参聊决策、互动方式与回复内容"""
    should_reply: bool = False
    action: str = "none"  # "at", "quote", "both", "none", "skill"
    reply_text: str = ""
    reason: str = ""
    target_user_id: Optional[str] = None
    target_msg_id: Optional[str] = None
    skill_name: Optional[str] = None
    skill_params: Optional[Dict[str, Any]] = None
    messages: list = field(default_factory=list)
    tasks: list = field(default_factory=list)
    deliver_files: Optional[list] = None

    def format_final_message(self) -> str:
        """根据决策动作拼装 CQ 码前缀（支持 @某人 与 引用消息）"""
        clean_reply = (self.reply_text or "").strip()
        if not clean_reply:
            return ""

        prefix_parts = []
        if self.action in ("quote", "both") and self.target_msg_id and str(self.target_msg_id).strip() not in ("", "0"):
            if f"[CQ:reply,id={self.target_msg_id}]" not in clean_reply:
                prefix_parts.append(f"[CQ:reply,id={self.target_msg_id}]")

        if self.action in ("at", "both") and self.target_user_id and str(self.target_user_id).strip():
            if f"[CQ:at,qq={self.target_user_id}]" not in clean_reply:
                prefix_parts.append(f"[CQ:at,qq={self.target_user_id}]")

        if prefix_parts:
            return "".join(prefix_parts) + " " + clean_reply
        return clean_reply


def _slang_prompt_section(text):
    from slang_glossary import default_slang_glossary
    try:
        return default_slang_glossary.prompt_section(text)
    except (OSError, ValueError) as error:
        import logging
        logging.getLogger("PromptBuilder").warning("话术表暂不可读: %s", error)
        return ""


def compose_incremental_message(user_text, *, author_name="群友", user_id="", message_id="",
                                is_master=False, is_at_bot=False, must_reply=False, mentioned_users=None,
                                search_info=None):
    """Only new event data; lasting instructions belong to the rules file."""
    event = {"type": "message", "id": str(message_id), "sender": author_name, "user_id": str(user_id),
             "master": bool(is_master), "directed": bool(is_at_bot or must_reply),
             "text": str(user_text).strip()}
    if mentioned_users:
        event["mentions"] = mentioned_users
    parts = [json.dumps(event, ensure_ascii=False)]
    slang = _slang_prompt_section(user_text)
    if slang:
        parts.append(slang)
    if search_info:
        parts.append(json.dumps({"local_definition": search_info}, ensure_ascii=False, default=str))
    return "\n".join(parts)


def compose_user_prompt(
    user_text: str,
    author_name: str = "群友",
    group_context: str = "",
    group_memory: str = "",
    is_master: bool = False,
    max_reply_length: int = 1200,
    enable_action_descriptions: bool = True,
    is_autonomous: bool = False,
    chat_rhythm: Optional[dict] = None,
    search_info: Optional[dict] = None,
    permanent_preset: str = "",
    include_preset: Optional[bool] = None,
    interaction_turn: int = 1,
    preset_reminder_interval: int = 10,
    incremental: bool = False
) -> str:
    """
    合成包含群聊近期上下文、长期记忆库摘要以及本轮约束的完整输入文本。
    增量模式只传新事件；非增量接口兼容调用方的永久预设，不重复注入角色表演要求。
    支持网络搜索梗词条信息与多模态感知。
    """
    if incremental:
        return compose_incremental_message(user_text, author_name=author_name, is_master=is_master,
            must_reply=is_master or not is_autonomous, search_info=search_info)
    if include_preset is None:
        include_preset = (interaction_turn == 1) or (interaction_turn % preset_reminder_interval == 0)

    sections = []
    sections.append(f"回答按问题需要展开；{max_reply_length} 字只是单条分段阈值，不压缩必要信息。")
    if not enable_action_descriptions:
        sections.append("纯台词文本输出，不写动作或旁白。")
    if chat_rhythm:
        sections.append(json.dumps({"chat_rhythm": chat_rhythm}, ensure_ascii=False))

    slang_section = _slang_prompt_section(user_text)
    if slang_section:
        sections.append(slang_section)

    # 群专属记忆库与历史会话归档（始终注入，保证跨会话记忆连续性）
    if group_memory and group_memory.strip():
        sections.append(group_memory.strip())

    # 周期性提醒与长期记忆预设
    if include_preset:
        if permanent_preset and permanent_preset.strip():
            sections.append(permanent_preset.strip())

    # 近期群聊上下文（如果有）
    if group_context and group_context.strip():
        sections.append(group_context.strip())

    # 网络搜索梗与未知语境解析（如果有）
    if search_info:
        from web_search import default_meme_searcher
        meme_sec = default_meme_searcher.format_meme_prompt_section(search_info)
        if meme_sec:
            sections.append(meme_sec)

    if is_master:
        sections.append("当前发言者是主人，按相处关系自然回应。")

    # 当前用户发言
    if is_autonomous and not is_master:
        sections.append(
            "【新消息唤醒与参聊决策】每条新群消息都重新分析，无需 @。"
            "should_reply=false 只跳过当前一轮，不代表休眠；不要等待被 @ 才恢复。"
            "是否接话由语义和对话关系决定；群友正在与你交流、继续追问、接梗、补充或纠正时，"
            "自然承接，即使消息很短或没有 @。不要因刚回复过、发言频繁或话题不是全群提问就中断对话。"
            "仅在本轮确实无须回应、对方要求安静或群友彼此聊天时暂不插话。"
            "仅返回 JSON：{\"should_reply\":true,\"action\":\"none\",\"reply\":\"台词\"}"
            " 或 {\"should_reply\":false,\"reason\":\"本轮原因\"}。"
        )
    sections.append(f"当前发言者 [{author_name}]: {user_text.strip()}")

    return "\n\n".join(sections)


def compose_autonomous_decision_prompt(
    user_text: str,
    author_name: str = "群友",
    user_id: str = "",
    message_id: str = "",
    group_context: str = "",
    group_memory: str = "",
    chat_rhythm: Optional[dict] = None,
    max_reply_length: int = 1200,
    enable_action_descriptions: bool = True,
    is_master: bool = False,
    is_at_bot: bool = False,
    must_reply: bool = False,
    mentioned_users: Optional[List[dict]] = None,
    search_info: Optional[dict] = None,
    permanent_preset: str = "",
    include_preset: Optional[bool] = None,
    interaction_turn: int = 1,
    preset_reminder_interval: int = 10,
    master_name: Optional[str] = None,
    incremental: bool = False
) -> str:
    """
    构建供 AI 自主决策是否参与群聊、互动行为方式以及生成自适应长度回复的完整 Prompt。
    增量模式只传新事件；非增量接口保留决策协议和调用方资料，不重复技能清单与角色表演要求。
    支持网络搜索梗词条信息、表情包语义明确度判定与合理接梗。
    """
    if incremental:
        return compose_incremental_message(user_text, author_name=author_name, user_id=user_id,
            message_id=message_id, is_master=is_master, is_at_bot=is_at_bot, must_reply=must_reply,
            mentioned_users=mentioned_users, search_info=search_info)
    if include_preset is None:
        include_preset = (interaction_turn == 1) or (interaction_turn % preset_reminder_interval == 0)

    sections = []
    sections.append(f"回答按问题需要展开；{max_reply_length} 字只是单条分段阈值，不压缩必要信息。")
    if not enable_action_descriptions:
        sections.append("纯台词文本输出，不写动作或旁白。")
    if chat_rhythm:
        sections.append(json.dumps({"chat_rhythm": chat_rhythm}, ensure_ascii=False))

    # 1. 群专属记忆库与历史会话归档（始终注入，保证跨会话记忆与人物刻写生效）
    slang_section = _slang_prompt_section(user_text)
    if slang_section:
        sections.append(slang_section)

    if group_memory and group_memory.strip():
        sections.append(group_memory.strip())

    # 周期性预设提醒
    if include_preset:
        if permanent_preset and permanent_preset.strip():
            sections.append(permanent_preset.strip())

    # 2. 近期群聊上下文（如果有）
    if group_context and group_context.strip():
        sections.append(group_context.strip())

    # 3. 网络梗搜索解析（如果有）
    if search_info:
        from web_search import default_meme_searcher
        meme_sec = default_meme_searcher.format_meme_prompt_section(search_info)
        if meme_sec:
            sections.append(meme_sec)

    must_interact = is_master or is_at_bot or must_reply
    sections.append(
        "【新消息唤醒规则】每条新群消息都重新分析，无需 @。should_reply=false 仅跳过本轮发言，"
        "不进入持续休眠，不要求群友先 @ 才恢复。引用中的安静用语不算当前发言者的命令；"
        "‘我在思考’、‘让我想想’等普通发言按当前上下文理解，不作为停止分析的关键词。"
        "群友正在与你交流、继续追问、接梗、补充或纠正时，应自然承接，即使消息很短或没有 @；"
        "不要因刚回复过、发言频繁或话题不是全群提问就中断对话。是否开口由语义和对话关系决定。"
    )

    sections.append("依据当前对话关系决定是否接话；正在与你交流或继续追问时自然承接，意义不明的图片不强行解释。"
        "直接输出 JSON：{\"should_reply\":true,\"action\":\"none\",\"reply\":\"台词\"}，"
        "或 {\"should_reply\":false,\"reason\":\"本轮不参与的原因\"}。引用和 @ 仅在确有需要时使用。")
    if must_interact:
        sections.append("这是向你的发言，请回应实际内容。" + ("当前发言者是主人。" if is_master else ""))
    if mentioned_users:
        sections.append("发言提及的对象：" + json.dumps(mentioned_users, ensure_ascii=False)
            + "；涉及评价或互动时认清对象，勿误指发言者。")
    sections.append("联网资料用 web_search，图片用 send_image，现有视频用 send_video，生成视频用 generate_video，文件用 send_file。"
        "需要工具时输出 {\"should_reply\":true,\"action\":\"skill\",\"skill\":\"技能名\",\"params\":{...}}；不凭记忆冒充检索或生成结果。")

    # 当前发言
    current_line = f"当前群内发言 [发言人: {author_name}, QQ: {user_id}, 消息ID: {message_id}]: {user_text.strip()}"
    sections.append(current_line)

    return "\n\n".join(sections)



def _extract_balanced_braces(text: str, start_idx: int) -> Optional[str]:
    """从 start_idx 的 '{' 开始向后提取完整的平衡 JSON 对象字符串"""
    depth = 0
    in_str = False
    escape = False
    for i in range(start_idx, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start_idx : i + 1]
    return None


def _try_parse_json_dict(s: str) -> Optional[dict]:
    """尝试将字符串反序列化为 JSON 字典，兼具轻度容错"""
    s = s.strip()
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # 容错: 移除尾部逗号
    try:
        fixed = re.sub(r",\s*([\]}])", r"\1", s)
        data = json.loads(fixed)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _extract_json_candidate(text: str) -> Optional[dict]:
    """从包含混杂文本、思考链的响应中稳健提取 JSON 决策字典"""
    if not text:
        return None

    # 1. 优先提取 Markdown 代码块中的 JSON
    code_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    for block in code_blocks:
        b = block.strip()
        parsed = _try_parse_json_dict(b)
        if parsed:
            return parsed
        # 若代码块内部含外围文字，找内部首个 '{'
        idx = b.find("{")
        if idx != -1:
            snippet = _extract_balanced_braces(b, idx)
            if snippet:
                parsed = _try_parse_json_dict(snippet)
                if parsed:
                    return parsed

    # 2. 定位 should_reply 或 reply 附近的平衡 JSON 对象（防止被前置的思考大括号干扰）
    for key in ('"should_reply"', "'should_reply'", '"reply"', "'reply'"):
        pos = text.find(key)
        if pos != -1:
            start_brace = text.rfind("{", 0, pos + 1)
            if start_brace != -1:
                snippet = _extract_balanced_braces(text, start_brace)
                if snippet:
                    parsed = _try_parse_json_dict(snippet)
                    if parsed:
                        return parsed

    # 3. 扫描全文所有大括号匹配，寻找包含关键字段的字典
    idx = text.find("{")
    while idx != -1:
        snippet = _extract_balanced_braces(text, idx)
        if snippet:
            parsed = _try_parse_json_dict(snippet)
            if parsed and ("should_reply" in parsed or "reply" in parsed or "action" in parsed):
                return parsed
        idx = text.find("{", idx + 1)

    return None


def parse_ai_decision_and_reply(
    raw_response: str,
    default_should_reply: bool = False,
    is_at_bot: bool = False,
    is_master: bool = False,
    default_user_id: str = "",
    default_msg_id: str = ""
) -> AIDecisionResult:
    """
    智能解析大模型返回的参聊决策与回复内容。
    兼具健壮的 JSON 提取与容错回退机制（Markdown codeblock、非标准 JSON 及纯文本对白兼容）。
    """
    clean = (raw_response or "").strip()
    if not clean:
        return AIDecisionResult(should_reply=False, reason="模型返回为空")

    # 1. 尝试从文本中解析 JSON 对象
    parsed_json = _extract_json_candidate(clean)

    if isinstance(parsed_json, dict):
        should_rep = parsed_json.get("should_reply")
        reply_content = str(parsed_json.get("reply", "") or parsed_json.get("content", "") or "").strip()
        messages = []
        for item in parsed_json.get("messages", []) if isinstance(parsed_json.get("messages"), list) else []:
            item = {"text": item} if isinstance(item, str) else item
            if not isinstance(item, dict) or not str(item.get("text") or item.get("reply") or "").strip():
                continue
            action = item.get("action", "none")
            messages.append({"text": str(item.get("text") or item.get("reply")).strip(),
                "action": action if action in ("none", "at", "quote", "both") else "none",
                "target_user_id": str(item.get("target_user_id") or default_user_id),
                "target_msg_id": str(item.get("target_msg_id") or default_msg_id)})
        if messages:
            reply_content = "\n".join(item["text"] for item in messages)
        tasks = parsed_json.get("tasks") if isinstance(parsed_json.get("tasks"), list) else []
        files = parsed_json.get("deliver_files") if isinstance(parsed_json.get("deliver_files"), list) else None
        if tasks and not reply_content and should_rep is not False:
            return AIDecisionResult(should_reply=False, tasks=tasks[:3], reason="任务交给控制台后台执行")

        # 显式拒绝参与
        if should_rep is False:
            # A task receipt or an intentional pass must never leak its JSON
            # into QQ, including directed and master messages.
            if not reply_content:
                return AIDecisionResult(
                    should_reply=False,
                    reason=str(parsed_json.get("reason", "AI 判定不参与当前话题")),
                    target_user_id=default_user_id,
                    target_msg_id=default_msg_id,
                    tasks=tasks[:3]
                )

        action = str(parsed_json.get("action", "none")).lower()
        if action not in ("at", "quote", "both", "none", "skill"):
            action = "at" if is_at_bot else "none"

        from skills.manager import default_skill_manager
        skill_inv = default_skill_manager.parse_skill_invocation(parsed_json)
        s_name, s_params = skill_inv if skill_inv else (None, None)

        if reply_content or is_master or is_at_bot or should_rep is True or s_name:
            return AIDecisionResult(
                should_reply=True,
                action=action,
                reply_text=reply_content or clean,
                reason=str(parsed_json.get("reason", "")),
                target_user_id=default_user_id,
                target_msg_id=default_msg_id,
                skill_name=s_name,
                skill_params=s_params,
                messages=messages,
                tasks=tasks[:3],
                deliver_files=files
            )

    # 2. 纯文本回退（若模型未输出合法 JSON）
    # 检查是否包含静默放弃指示词
    quiet_markers = (
        "[PASS]", "[IGNORE]", "[不回复]", "[保持安静]", "保持沉默",
        "不参与回复", "无需插话", "不适合插话", '"should_reply": false'
    )
    if any(tag in clean for tag in quiet_markers):
        if not is_master and not is_at_bot:
            return AIDecisionResult(
                should_reply=False,
                reason="AI 明确指示保持安静",
                target_user_id=default_user_id,
                target_msg_id=default_msg_id
            )

    # 若为主人发言或 @机器人，或者外部显式要求必须回复（例如指令回复）
    if is_master or is_at_bot or default_should_reply:
        default_action = "at" if is_at_bot else "none"
        return AIDecisionResult(
            should_reply=True,
            action=default_action,
            reply_text=clean,
            target_user_id=default_user_id,
            target_msg_id=default_msg_id
        )

    # 自主参聊模式下：若模型未按规范输出 JSON，且未被 @ 或非主人，安全静默跳过，绝不泄露思考链或乱发对白
    return AIDecisionResult(
        should_reply=False,
        reason="AI 未输出有效参聊决策，安全静默跳过",
        target_user_id=default_user_id,
        target_msg_id=default_msg_id
    )


