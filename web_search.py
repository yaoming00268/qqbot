# -*- coding: utf-8 -*-
"""
Web Search & Meme Slang Parser for QQ Bot.
Provides:
1. Detection of internet memes, buzzwords, slang, and obscure/ambiguous phrases.
2. Web search querying with online scrapers and resilient offline dictionary fallback.
3. Guidance generation for natural, non-cringe meme usage in AI replies.
"""

from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("WebSearch")

# 常见热门梗与网络黑话离线知识库（毫秒级响应，网络受限或离线时兜底保障）
OFFLINE_MEME_KNOWLEDGE: Dict[str, Dict[str, str]] = {
    "city不city": {
        "term": "city不city",
        "meaning": "源自外国博主在中国旅游时的口头禅，意指时髦、洋气、具有现代大都市的繁华时尚氛围，亦可用作反问'洋不洋气'。",
        "guidance": "结合语境自然调侃是否洋气、现代化或接地气，切忌生搬硬套复读。"
    },
    "泰裤辣": {
        "term": "泰裤辣",
        "meaning": "谐音'太酷啦'，源自艺人小鬼（王琳凯）在演唱会上的热血喊话，后被网民用于幽默吐槽或反讽调侃。",
        "guidance": "可用于幽默起哄或搞笑捧场，语气要轻松诙谐。"
    },
    "尊嘟假嘟": {
        "term": "尊嘟假嘟",
        "meaning": "源自网络博主模仿宠物幼态口齿不清的谐音'真的假的'，表达惊讶、好奇或卖萌反问。",
        "guidance": "适度用于卖萌或幽默怀疑，不要滥用连续复读。"
    },
    "疯狂星期四": {
        "term": "疯狂星期四",
        "meaning": "源自肯德基每周四特惠促销引发的'疯四文学'，群友常以各种离奇故事铺垫结尾转折为'v我50'。",
        "guidance": "可接招顺应'v我50'或吐槽对方又在编故事打广告。"
    },
    "鸡你太美": {
        "term": "鸡你太美",
        "meaning": "歌曲《只因你太美》的空耳谐音，常伴随唱跳rap篮球等流行网络梗。",
        "guidance": "友好诙谐调侃，切忌人身攻击或恶意对立。"
    },
    "电子木鱼": {
        "term": "电子木鱼",
        "meaning": "指敲击手机软件或网页上的虚拟木鱼以'积电子功德'，常用于在看了损人段子后戏谑表示赎罪、平复心态。",
        "guidance": "在群友开地狱笑话或损人玩笑时，可调侃'功德-1'或'快去敲电子木鱼'。"
    },
    "退退退": {
        "term": "退退退",
        "meaning": "源自一位摆摊大妈与他人争执时一边跺脚一边挥手的魔性动作与口号，用于幽默抵御晦气、不幸或不想理会的事物。",
        "guidance": "用于搞笑嫌弃、赶走倒霉事或驱逐无理取闹的发言。"
    },
    "硬控": {
        "term": "硬控",
        "meaning": "借用游戏术语，指某种令人极其震惊、尴尬或极具魔性吸引力的事物让人无法移开视线或愣在原地。",
        "guidance": "形容被某人某事深深震撼、看呆或无语凝噎。"
    },
    "小黑子": {
        "term": "小黑子",
        "meaning": "网络黑话，指假装粉丝实则恶搞或开玩笑抹黑偶像的群体，常用于熟人之间开玩笑式的戏谑指责。",
        "guidance": "用于朋友间开玩笑吐槽'露出鸡脚了吧'，语调轻松。"
    },
    "破防": {
        "term": "破防",
        "meaning": "原意为突破防御，现指心理防线被触动或击溃，如被某句话戳中痛处、感动得热泪盈眶或急躁破防。",
        "guidance": "适度吐槽对方'这就破防啦？'或同情安慰。"
    },
    "开摆": {
        "term": "开摆",
        "meaning": "'摆烂'的近义词，指放弃努力、顺其自然、不愿继续内卷的生活态度。",
        "guidance": "可以傲娇吐槽对方不求上进，或者表示适当摸鱼放松。"
    },
    "牛马": {
        "term": "牛马",
        "meaning": "指辛勤劳作、被工作支配的打工人或社畜的自嘲称呼。",
        "guidance": "体现共情与幽默同情，切勿居高临下指责。"
    },
    "显眼包": {
        "term": "显眼包",
        "meaning": "指在人群中爱出风头、搞怪或者行为举止特别引人注目且富有喜感的人。",
        "guidance": "用于亲切调侃爱耍宝、抢风头的群友。"
    },
    "淡人": {
        "term": "淡人",
        "meaning": "指性格云淡风轻、情绪稳定甚至有些低欲望、对外界变化反应波澜不惊的人群。",
        "guidance": "可对照自己的性格反差进行趣味点评。"
    },
    "搭子": {
        "term": "搭子",
        "meaning": "一种精准垂直的社交关系，如饭搭子、游戏搭子、旅游搭子，专注特定领域的轻社交。",
        "guidance": "自然融入话题，如'做你的聊天搭子'。"
    },
    "yyds": {
        "term": "yyds",
        "meaning": "拼音缩写'永远的神'，用于高度赞美某人、某物或某部作品封神级别。",
        "guidance": "适度用于夸奖，避免每句复读导致言语贫乏。"
    },
    "xswl": {
        "term": "xswl",
        "meaning": "拼音缩写'笑死我了'，表达极度好笑或调侃。",
        "guidance": "自然表达笑意或幽默。"
    },
    "awsl": {
        "term": "awsl",
        "meaning": "拼音缩写'啊我死了'，常用于见到极其可爱、萌态十足的人或事物时的激动感叹。",
        "guidance": "傲娇角色被夸萌时可反驳'谁要你awsl啦！'。"
    },
    "u1s1": {
        "term": "u1s1",
        "meaning": "拼音缩写'有一说一'，表示客观陈述、实事求是地发表看法。",
        "guidance": "用于开启一段客观真实的评价或吐槽。"
    },
    "纯爱战神": {
        "term": "纯爱战神",
        "meaning": "指坚决维护纯粹专一爱情、厌恶背叛与牛头人（NTR）剧情的二次元群体与爱好者。",
        "guidance": "在涉及情感探讨时可用于表态支持真诚专一。"
    },
    "原神启动": {
        "term": "原神启动",
        "meaning": "源自博主门失禁模仿原神玩家开场音效的搞笑短视频，后成为二次元玩家自嘲或群体狂欢梗。",
        "guidance": "幽默开玩笑带过，不参与游戏粉丝群体无谓争议。"
    },
    "发疯文学": {
        "term": "发疯文学",
        "meaning": "指用大段充满情绪宣泄、毫无逻辑、逻辑颠倒却极具感染力的文字来表达内心的焦虑或进行反向施压。",
        "guidance": "可幽默指出对方'怎么突然发疯了'，傲娇嫌弃。"
    },
    "蚌埠住了": {
        "term": "蚌埠住了",
        "meaning": "谐音'绷不住了'，指在面对极度搞笑、荒谬、难过或破防的情形时无法克制住情绪，忍不住想笑、想哭或吐槽。",
        "guidance": "适度用于吐槽对方让人哭笑不得的行为或荒唐话题。"
    },
    "急了": {
        "term": "急了",
        "meaning": "网络对线与游戏黑话，戏谑对方因被戳中痛处或处于不利局面而情绪激动、气急败坏。",
        "guidance": "用于熟人之间友好互怼调侃，切忌人身攻击或恶意激怒。"
    },
    "典中典": {
        "term": "典中典",
        "meaning": "形容某言论或行为极其典型、老套、充满负面刻板印象或荒谬逻辑，简称为'典'。",
        "guidance": "幽默吐槽老套言论或熟人熟事，语气要轻快诙谐。"
    },
    "遥遥领先": {
        "term": "遥遥领先",
        "meaning": "源自华为发布会对技术优势的强调，后演化为网民在各类生活、科技场景中幽默自豪或调侃的流行梗。",
        "guidance": "结合具体语境幽默点赞或调侃某项成果与优势。"
    },
    "汗流浃背": {
        "term": "汗流浃背",
        "meaning": "源自游戏解说口头禅'汗流浃背了吧小老弟'，指对方被戳破真相、遇到硬茬或面对难堪处境时的紧张心虚与尴尬。",
        "guidance": "用于搞笑调侃对方心虚或面对难题时的窘迫神态。"
    },
    "小丑竟是我自己": {
        "term": "小丑竟是我自己",
        "meaning": "用于自嘲某种自作多情、以为占了便宜实则被蒙在鼓里、极其滑稽可笑的尴尬状态。",
        "guidance": "可用于自嘲失误或幽默安慰同样遇到尴尬事的群友。"
    },
    "纯纯的": {
        "term": "纯纯的",
        "meaning": "网络口头修饰语，强调某种属性极其纯粹、无可争议（如'纯纯的牛马'、'纯纯的搞子'）。",
        "guidance": "口语化自然使用，加强生动吐槽语气。"
    },
    "抽象": {
        "term": "抽象",
        "meaning": "指事物或言行极其怪诞、匪夷所思、逻辑颠倒或魔性搞笑，脱离一般人类常理认知。",
        "guidance": "调侃离谱搞怪的言行或奇葩遭遇，保持生动鲜活。"
    },
    "老六": {
        "term": "老六",
        "meaning": "游戏黑话，指团队中躲在暗处不参与正面正面交锋、偷袭收割或坑队友的人，后延伸至生活中悄悄耍滑头搞小动作的人。",
        "guidance": "用于亲切吐槽群友不讲武德、偷偷摸摸耍心机。"
    },
    "红温": {
        "term": "红温",
        "meaning": "借用英雄联盟英雄兰博技能机制，形容人被气得满脸通红、急躁上头、情绪濒临暴走。",
        "guidance": "用于生动调侃对方急躁破防的样子，语气傲娇诙谐。"
    },
    "基操勿六": {
        "term": "基操勿六",
        "meaning": "'基本操作，请大家不要喊666'的缩写，用于展示小技能或小成就后低调装帅或幽默凡尔赛。",
        "guidance": "用于在完成某事后顺带傲娇凡尔赛一把。"
    },
    "芜湖起飞": {
        "term": "芜湖起飞",
        "meaning": "谐音'呜呼起飞'，表达兴奋、心情大好或事情进展极其顺利的亢奋情绪。",
        "guidance": "在有好消息或气氛热烈时用于起哄捧场。"
    },
    "特种兵旅游": {
        "term": "特种兵旅游",
        "meaning": "指年轻人用极短时间、极高强度、极少睡眠打卡海量景点的极限旅游模式。",
        "guidance": "可调侃周末爆肝出游或快节奏赶场行程。"
    },
    "脆皮大学生": {
        "term": "脆皮大学生",
        "meaning": "调侃当代大学生看似年轻但身体极易因微小意外受伤生病的幽默自嘲。",
        "guidance": "幽默关心对方身体健康，叮嘱注意休息。"
    },
    "dddd": {
        "term": "dddd",
        "meaning": "拼音缩写'懂的都懂'，表示事情心照不宣、无需明说。",
        "guidance": "自然用于朋友间眼神交流式的心照不宣。"
    },
    "yygq": {
        "term": "yygq",
        "meaning": "拼音缩写'阴阳怪气'，指说话带刺、冷嘲热讽或含沙射影。",
        "guidance": "傲娇角色被调侃时可反击'谁阴阳怪气啦！'。"
    },
    "srds": {
        "term": "srds",
        "meaning": "拼音缩写'虽然但是'，用于转折发表自己的真实观点。",
        "guidance": "自然用于展开客观吐槽或转折评价。"
    },
    "zqsg": {
        "term": "zqsg",
        "meaning": "拼音缩写'真情实感'，指投入了真实诚挚的情感。",
        "guidance": "在探讨剧情或真诚话题时自然使用。"
    },
    "gkd": {
        "term": "gkd",
        "meaning": "拼音缩写'搞快点'，催促动作快点、快点更新或分享。",
        "guidance": "幽默催促群友分享后续或交出资源。"
    },
    "emo": {
        "term": "emo",
        "meaning": "泛指情绪低落、郁闷、心累或陷入夜间抑郁emo状态。",
        "guidance": "适度关心安慰对方，或者傲娇吐槽'又在深夜emo了'。"
    },
    "kswl": {
        "term": "kswl",
        "meaning": "拼音缩写'嗑死我了'，用于看到喜欢的CP甜蜜互动时的兴奋感叹。",
        "guidance": "可用于群友起哄或讨论二次元剧情时吃瓜助兴。"
    },
    "i人": {
        "term": "i人",
        "meaning": "MBTI性格测试中的内向内倾型群体，通常偏好独处、社交消耗能量。",
        "guidance": "对照自己的性格进行趣味点评与共鸣。"
    },
    "e人": {
        "term": "e人",
        "meaning": "MBTI性格测试中的外向外倾型群体，热衷社交、从人群互动中获取能量。",
        "guidance": "可调侃群内爱耍宝活跃的显眼包群友。"
    },
    "赢麻了": {
        "term": "赢麻了",
        "meaning": "形容接连不断取得胜利，多用于网民反讽阿Q式胜利或幽默凡尔赛。",
        "guidance": "适度用于幽默反讽调侃，不生硬复读。"
    },
    "食不食油饼": {
        "term": "食不食油饼",
        "meaning": "空耳谐音'是不是有病'，黑粉恶搞衍生的网络调侃口头禅。",
        "guidance": "用于朋友间开玩笑嗔怪'你食不食油饼'，傲娇嫌弃。"
    },
    "露出鸡脚": {
        "term": "露出鸡脚",
        "meaning": "谐音'露出马脚'，调侃对方不小心暴露了黑粉、卧底或特定爱好身份。",
        "guidance": "用于熟人之间幽默指认对方假装正经。"
    },
    "你干嘛": {
        "term": "你干嘛",
        "meaning": "经典恶搞语音梗，用于表达惊讶、受惊、嗔怪或被冒犯时的幽默反应。",
        "guidance": "被突然戳一戳或开玩笑时可傲娇娇嗔'你干嘛哈哈哎哟'。"
    },
    "大聪明": {
        "term": "大聪明",
        "meaning": "反讽调侃某人自以为聪明实则办了蠢事，或做出了令人哭笑不得的操作。",
        "guidance": "用于吐槽熟人搞笑翻车或迷之操作。"
    },
    "NPC": {
        "term": "NPC",
        "meaning": "非玩家角色，自嘲在现实生活中如同按部就班、默默无闻的背景板。",
        "guidance": "幽默共情或鼓励对方每个人都是自己生活的主角。"
    },
    "寄": {
        "term": "寄",
        "meaning": "源自GG（Good Game），意指事情办砸了、失败了、彻底没戏了。",
        "guidance": "群友翻车时可幽默调侃'这下真寄了'。"
    },
    "麻了": {
        "term": "麻了",
        "meaning": "指经历太多离谱、心累的事后感到身心麻木、无力吐槽。",
        "guidance": "同情安慰或顺应吐槽现实的无奈。"
    }
}

# 梗探测关键词与模式
_MEME_QUERY_PATTERNS = [
    re.compile(r"(.+?)(?:是什么梗|啥梗|是个什么梗|是个啥梗|什么意思|是啥意思|啥意思|是啥|是个啥|指什么)", re.IGNORECASE),
    re.compile(r"(?:懂不懂|知道不知道|听说过)(.+?)(?:的含金量|这个梗|吗)", re.IGNORECASE),
    re.compile(r"(?:什么是|解释一下|科普一下|求科普)(.+)", re.IGNORECASE),
]

# 常见缩写黑话词表
_SLANG_KEYWORDS = [
    "yyds", "xswl", "awsl", "nsdd", "u1s1", "emo", "kswl", "bdjw", "nbcs",
    "dddd", "yygq", "srds", "zqsg", "gkd",
    "city不city", "泰裤辣", "尊嘟假嘟", "疯狂星期四", "鸡你太美", "电子木鱼",
    "退退退", "硬控", "小黑子", "破防", "开摆", "牛马", "显眼包", "淡人",
    "搭子", "纯爱战神", "原神启动", "发疯文学", "老六", "绝绝子", "特种兵",
    "背刺", "画大饼", "摸鱼", "躺平", "真香", "奥利给",
    "蚌埠住了", "急了", "典中典", "遥遥领先", "汗流浃背", "小丑竟是我自己",
    "纯纯的", "抽象", "红温", "基操勿六", "芜湖起飞", "特种兵旅游", "脆皮大学生",
    "i人", "e人", "赢麻了", "食不食油饼", "露出鸡脚", "你干嘛", "大聪明", "NPC", "寄", "麻了"
]


class MemeWebSearcher:
    """网络搜索与梗词条解析服务"""

    def __init__(self, cache_ttl_seconds: int = 3600):
        self.cache_ttl_seconds = cache_ttl_seconds
        self._search_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def detect_meme_or_obscure_phrase(self, text: str) -> Optional[str]:
        """
        探测文本中是否包含疑似网络梗、黑话或请求解析不明言语的词条。
        若探测到则返回提取的核心查询关键词，否则返回 None。
        """
        if not text:
            return None

        clean = text.strip()

        # 1. 优先探测显式的求梗提问句式（如“city不city是什么梗”）
        for pat in _MEME_QUERY_PATTERNS:
            m = pat.search(clean)
            if m:
                kw = m.group(1).strip(" ?？!！,，。\"'“”")
                if 1 <= len(kw) <= 25:
                    return kw

        # 2. 探测预设的热门梗与网络黑话关键词
        clean_lower = clean.lower()
        for kw in _SLANG_KEYWORDS:
            if kw.lower() in clean_lower:
                return kw

        # 3. 检查离线词库中的已知键
        for term in OFFLINE_MEME_KNOWLEDGE:
            if term.lower() in clean_lower:
                return term

        # 4. 探测简短且可能为生僻黑话的短语（如以引号括起的特殊网络词汇）
        quote_match = re.search(r"[\"“]([^\n\"”]{2,15})[\"”]", clean)
        if quote_match:
            candidate = quote_match.group(1).strip()
            # 排除普通数字或标准标点
            if re.search(r"[\u4e00-\u9fa5a-zA-Z]", candidate):
                return candidate

        return None

    def _get_offline_knowledge(self, query: str) -> Optional[Dict[str, Any]]:
        """从内置离线知识库匹配梗的解释"""
        q_clean = query.strip().lower()
        for term, info in OFFLINE_MEME_KNOWLEDGE.items():
            if term.lower() == q_clean or term.lower() in q_clean or q_clean in term.lower():
                return {
                    "term": info["term"],
                    "meaning": info["meaning"],
                    "guidance": info["guidance"],
                    "source": "offline_knowledge_base"
                }
        return None

    def _sync_http_search(self, query: str, timeout: float = 3.0, append_meme_terms: bool = True) -> Optional[str]:
        """
        使用轻量 HTTP 发起实时网络检索。
        检索网络上对该梗或未知词的解释片段，或执行通用信息检索。
        """
        q_str = f"{query} 是什么梗 含义" if append_meme_terms else query
        encoded = urllib.parse.quote(q_str)
        search_urls = [
            f"https://www.so.com/s?q={encoded}",
            f"https://cn.bing.com/search?q={encoded}",
            f"https://baike.baidu.com/item/{urllib.parse.quote(query)}"
        ]

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
        }

        for url in search_urls:
            try:
                req = urllib.request.Request(url, headers=headers)
                # 使用标准 opener，支持代理环境与直连；单源设保护超时
                with urllib.request.urlopen(req, timeout=min(timeout, 2.5)) as resp:
                    if resp.status == 200:
                        raw_html = resp.read(256000).decode("utf-8", errors="replace")

                        # 1. 优先从 360 搜索的结构化摘要提取
                        desc_matches = re.findall(r'<p class=["\']res-desc["\'][^>]*>(.*?)</p>', raw_html, re.I | re.S)
                        if desc_matches:
                            snippet = re.sub(r'<[^>]+>', '', desc_matches[0]).strip()
                            if len(snippet) >= 15:
                                return snippet

                        # 2. 优先从 Bing 搜索的结构化摘要提取
                        bing_matches = re.findall(r'<li class=["\']b_algo["\'][^>]*>.*?<div class=["\']b_caption["\'][^>]*>.*?<p[^>]*>(.*?)</p>', raw_html, re.I | re.S)
                        if bing_matches:
                            snippet = re.sub(r'<[^>]+>', '', bing_matches[0]).strip()
                            if len(snippet) >= 15:
                                return snippet

                        # 3. 通用 HTML 过滤提取文本
                        text = re.sub(r"<script[\s\S]*?</script>", "", raw_html, flags=re.IGNORECASE)
                        text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
                        text = re.sub(r"<[^>]+>", " ", text)
                        text = re.sub(r"\s+", " ", text).strip()

                        # 寻找包含“是指”、“源自”、“网络流行语”、“梗”等解释性句段
                        snippets = re.findall(
                            rf"(?:{re.escape(query)}|该梗)[^。！？\n]*?(?:是指|指的是|源于|源自|网络流行语|意思是|出自|表示|调侃|戏称|谐音)[^。！？\n]{{5,80}}[。！？]?",
                            text,
                            re.IGNORECASE
                        )
                        if snippets:
                            return snippets[0].strip()

                        # 若未直接命中模板句，找包含关键词的最相关段落
                        m = re.search(rf"[^。！？\n]{{0,30}}{re.escape(query)}[^。！？\n]{{10,70}}[。！？]", text)
                        if m:
                            return m.group(0).strip()
            except Exception as e:
                logger.debug(f"网络检索源 {url} 请求异常: {e}")
                continue

        return None

    async def search_meme_or_phrase(self, query: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
        """
        异步查询梗或生僻语的解析与合理用梗指引。
        优先缓存，其次离线词库，再次实时网络搜索。
        """
        if not query or not query.strip():
            return None

        q = query.strip()
        now = time.time()

        # 1. 检查内存缓存
        if q in self._search_cache:
            ts, val = self._search_cache[q]
            if now - ts < self.cache_ttl_seconds:
                return val

        # 2. 检查离线词库
        offline_res = self._get_offline_knowledge(q)
        if offline_res:
            self._search_cache[q] = (now, offline_res)
            return offline_res

        # 3. 发起实时网络检索
        try:
            snippet = await asyncio.to_thread(self._sync_http_search, q, timeout)
            if snippet:
                clean_snippet = snippet.replace("\r", " ").replace("\n", " ").strip()
                if len(clean_snippet) > 120:
                    clean_snippet = clean_snippet[:117] + "..."
                result = {
                    "term": q,
                    "meaning": clean_snippet,
                    "guidance": "请结合梗的真实含义自然融入对话，切忌生搬硬套或尴尬复读，符合真实群友风格。",
                    "source": "web_search"
                }
                self._search_cache[q] = (now, result)
                return result
        except Exception as e:
            logger.debug(f"异步网络搜索执行失败: {e}")

        # 4. 兜底回退：无法联网时根据词面生成智能指引
        fallback = {
            "term": q,
            "meaning": f"疑似网络流行梗或特定群聊黑话词汇「{q}」",
            "guidance": "若理解该梗含义则机智接话，若不确定可自然反问或顺着对方话题调侃，避免强行装懂或说教。",
            "source": "heuristic_fallback"
        }
        self._search_cache[q] = (now, fallback)
        return fallback

    async def search_general(self, query: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
        """执行通用网络搜索，获取客观网页摘要与信息"""
        if not query or not query.strip():
            return None
        q = query.strip()
        try:
            snippet = await asyncio.to_thread(self._sync_http_search, q, timeout, False)
            if snippet:
                clean_snippet = snippet.replace("\r", " ").replace("\n", " ").strip()
                if len(clean_snippet) > 240:
                    clean_snippet = clean_snippet[:237] + "..."
                return {
                    "query": q,
                    "snippet": clean_snippet,
                    "source": "web_search"
                }
        except Exception as e:
            logger.debug(f"通用网络检索失败: {e}")
        return None

    def format_meme_prompt_section(self, meme_info: Optional[Dict[str, Any]]) -> str:
        """格式化为适合注入 Prompt 的网络梗解析与用梗指引"""
        if not meme_info:
            return ""

        term = meme_info.get("term", "")
        meaning = meme_info.get("meaning", "")
        guidance = meme_info.get("guidance", "自然接梗，切勿生搬硬套。")

        lines = [
            f"【网络梗与未知语境检索解析】",
            f"- 关注词条: 「{term}」",
            f"- 释义参考: {meaning}",
            f"- 合理用梗指引: {guidance}"
        ]
        return "\n".join(lines)


# 全局单例
default_meme_searcher = MemeWebSearcher()
