"""Deterministic natural-language media routing; model output is never executed."""
from __future__ import annotations

import re

from media_models import MediaIntent


NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def number(value):
    if value.isdigit():
        return int(value)
    if "十" in value:
        tens, _, ones = value.partition("十")
        return NUMBERS.get(tens, 1) * 10 + NUMBERS.get(ones, 0)
    return NUMBERS.get(value, 1)


def is_work_request(text):
    """Directed action requests start in the work lane before model planning."""
    if str(text or "").strip().startswith(("/", "／")):
        from console_tools.chat_commands import parse_command, WORK
        try:
            return parse_command(text).name in WORK
        except ValueError:
            return False
    return bool(re.match(
        r"^(?:(?:请|帮我|麻烦(?:你)?|能不能|能否|可以(?:帮我)?|请你)\s*)*"
        r"(?:(?:网络|网上)(?:搜索|检索|搜)|在网上搜|搜索|检索|查找|查询|查一下|找一下|搜一下|找找|搜搜|查查|搜|下载|抓取|爬取|整理|生成|制作|分析资料|编写脚本)"
        r"|^(?:(?:请|麻烦(?:你)?|能不能|能否|可以)\s*)*(?:给我|发我|发给我|帮我(?:找|查|搜|写|做)|我想要|我要)(?:一下|一份|一段|一篇|个|份)?\s*.+(?:歌词|译文|翻译|全文|原文|报告|攻略|资料|代码|脚本|清单|方案|教程|总结)"
        r"|^(?:please\s+)?(?:search|look\s+up|find|download|fetch|create\s+(?:a\s+)?(?:file|report))\b",
        str(text or "").strip(), re.I))


def is_priority_control(text):
    """Cancellation and local controls remain available while work is running."""
    if str(text or "").strip().startswith(("/", "／")):
        from console_tools.chat_commands import parse_command, WORK
        try:
            return parse_command(text).name not in WORK
        except ValueError:
            return True
    intent = parse_media_intent(text)
    if intent and intent.operation in ("cancel", "status", "help"):
        return True
    from command_deliberation import default_command_deliberator
    result = default_command_deliberator.deliberate(text, is_group=False, is_at_bot=True)
    return (result.is_command and result.command_type not in ("chat", "media")
            or str(text or "").strip().lower() in ("/status", "状态", "status"))


def parse_media_intent(text: str, default_count=3):
    text = str(text or "").strip()
    if not text:
        return None
    lowered = text.lower()
    clean_punct = re.sub(r"[!！?？~～.。，,、\s]+$", "", lowered).strip()

    # 1. 帮助指令
    if clean_punct in ("/media", "/media help", "/媒体", "媒体帮助", "媒体功能", "媒体指令"):
        return MediaIntent(operation="help")

    # 2. 状态查询与取消
    if re.fullmatch(r"(?:/?(?:任务进度|下载进度|media\s+status|media-status|task|查看进度|下载状态))(?:\s+([a-f0-9]{12}))?", lowered):
        found = re.search(r"\b[a-f0-9]{12}\b", lowered)
        return MediaIntent(operation="status", job_id=found[0] if found else "")
    if re.fullmatch(r"(?:/?(?:取消|cancel))(?:刚才的)?(?:下载|任务)?(?:\s+([a-f0-9]{12}))?", lowered):
        found = re.search(r"\b[a-f0-9]{12}\b", lowered)
        return MediaIntent(operation="cancel", job_id=found[0] if found else "")

    # 3. 翻页 / 下一张 / 换一批
    next_pattern = r"^/?(?:next|下一页|换一批|换一组|换一张|再来一张|再来一批|再来一组|再来一个|再来一份|下一组|换几个|翻页|看下一页|看下一个|看下一张|下一个|下一张|换一个)(?:吧|呗|呀|啊)?$"
    if re.fullmatch(next_pattern, clean_punct):
        return MediaIntent(operation="next")

    # 4. 挑选候选媒体结果 (/pick N 或 "把第二个视频发我", "第3张", "选第二张", "发第1个")
    explicit_pick = re.fullmatch(r"/pick\s+(\d+)(?:\s+([a-f0-9]{12}))?", lowered)
    if explicit_pick:
        return MediaIntent(operation="pick", index=max(1, min(100, int(explicit_pick[1]))), result_id=explicit_pick[2] or "")

    pick = re.search(r"(?:第\s*([一二两三四五六七八九十\d]+)\s*(?:个|张|条|份)|(?:选|要|发|看|下载)\s*第\s*([一二两三四五六七八九十\d]+)\s*(?:个|张|条|份)?)", text)
    # 排除非媒体结果挑选（如第1个选项、第2集、第3章、第4题等）
    non_media_pick = re.search(r"第\s*[一二两三四五六七八九十\d]+\s*(?:个|张)?\s*(?:章|集|节|段|步|课|选项|方案|答案|题|位|人|天|周|月|年|次|回|名)", text)
    if pick and not non_media_pick:
        if "换" in text:
            return MediaIntent(operation="next", count=1, send_requested=True)
        if any(w in text for w in ("发", "给", "要", "选", "下载", "看")) and not any(w in text for w in ("搜", "搜索", "查找")):
            idx_val = pick[1] or pick[2]
            return MediaIntent(operation="pick", index=max(1, min(100, number(idx_val))), as_file="文件" in text or "原图" in text)
        if re.fullmatch(r"^(?:(?:帮我|请|把)?\s*)?第?\s*[一二两三四五六七八九十\d]+\s*(?:个|张|条|份|张图|幅)?(?:\s*(?:图片|照片|原图|文件|视频))?(?:吧|呗)?$", clean_punct):
            idx_val = pick[1] or pick[2]
            return MediaIntent(operation="pick", index=max(1, min(100, number(idx_val))), as_file="文件" in text or "原图" in text)

    # 5. 导出文件
    if any(w in lowered for w in ("txt", "csv", "markdown", "md文件", "文本", "文本文档", "文本文件", "表格")) and any(w in text for w in ("整理", "导出", "生成", "存成", "保存", "转成", "存为", "做成", "保存为")):
        extension = "csv" if any(w in lowered for w in ("csv", "表格")) else "md" if any(w in lowered for w in ("markdown", "md文件")) else "txt"
        return MediaIntent(operation="export", kind="file", extension=extension)

    # 6. 原图引用下载
    if any(w in text for w in ("原图", "这张图片", "这张图", "引用图片", "这条图片", "发原图", "原图下载", "发送原图")) and any(w in text for w in ("发", "给", "下载", "保存", "传")):
        return MediaIntent(operation="quote", kind="image", as_file="文件" in text)

    # 7. URL 直传与下载
    url_match = re.search(r"https?://[^\s<>\"'，。！？]+", text)
    if url_match:
        url = url_match[0].rstrip(")）]")
        is_media_url = bool(re.search(r"\.(?:mp4|webm|ogv|mov|jpe?g|png|gif|webp|pdf|zip|tar|gz)(?:\?|$)", url, re.I))
        file_download_verbs = ("下载", "保存", "发到群", "发我", "传到群", "作为文件", "存到")
        if is_media_url or any(w in text for w in file_download_verbs):
            kind = "video" if "视频" in text or re.search(r"\.(?:mp4|webm|ogv|mov)(?:\?|$)", url, re.I) else "image" if any(w in text for w in ("图", "照片")) or re.search(r"\.(?:jpe?g|png|gif|webp)(?:\?|$)", url, re.I) else "file"
            return MediaIntent(operation="send_url", kind=kind, url=url, request_text=text, as_file="作为文件" in text)

    command = re.match(r"^/(image|video|file|library|groupfile)\s+(.+)$", text, re.I | re.S)

    # 8. 负向过滤与非命令守卫：排除否定请求、陈述分享、软件咨询与日常闲聊
    if not command:
        # 负向请求拦截（如“不要发这个表情”、“别发表情包了”、“不要发图”、“我不要看图片”）
        if re.search(r"(?:不要|别|不用|请勿|严禁|别再)\s*(?:再)?(?:发|看|搜|找|要|传|下载)?\s*(?:这个|这些|这张|这只|本|此)?\s*(?:图片|照片|猫图|狗图|壁纸|表情包|表情|插画|头像|动图|美图|视频|短视频|文件|资料|原图)?(?:了|吧)?$", text):
            return None
        if re.search(r"^(?:我)?(?:不|不要|不想|别)\s*(?:看|发|要|找)\s*(?:图片|照片|猫图|狗图|壁纸|表情包|表情|视频|短视频|文件|资料)", text):
            return None
        # 非命令的陈述句、分享评价、解释提问或代码片段讨论
        if re.search(r"^(?:请?解释|为什么|什么是|怎么|如何|你觉得|你喜欢|你平时|我昨天|刚才看了|代码片段|cq:image|你看)", lowered):
            return None
        if any(w in text for w in ("真逗", "真搞笑", "太逗", "太搞笑", "真有意思", "好搞笑")):
            return None
        # 软件工具咨询或制作剪辑方法咨询（如“推荐几个做视频的软件”、“怎么剪辑短视频”、“Python编程教程”）
        if re.search(r"(?:软件|工具|app|怎么剪辑|怎么做|怎么拍|怎么画|怎么学|剪辑教程|制作教程|拍摄教程|编程教程|开发教程|代码教程|技术教程)", text):
            return None
        # 日常陈述如“明天要交文件”、“我们要拍短视频”、“重要的文件要保存好”
        if re.search(r"(?:明天|后天|今天|下周|准备|计划|主要|重要|交)\s*(?:要|需)?\s*(?:交|做|写|拍)?\s*(?:文件|资料|短视频|视频)", text):
            return None
        if re.search(r"^我们要\s*(?:拍|做|写|录)", text):
            return None
        # 询问对方意向“你要看这个资料吗”、“你要发图吗”
        if re.search(r"^你\s*(?:要|想)\s*(?:看|发|要|收).*[吗？\?]$", text):
            return None

    search_verbs = ("搜", "找", "查", "搜索", "查找", "检索", "查下", "搜下", "找下", "搜搜", "找找", "查查")
    send_verbs = (
        "发", "给我", "发我", "发来", "发到群里", "发出来", "下载",
        "整点", "整张", "整几个", "整两张", "来点", "来张", "来个", "来两张", "来几张",
        "看看", "看下", "我想看", "想看", "给我看", "想要", "我要", "要看", "要张", "要个", "要两张", "要几张", "要点",
        "换一张", "换个", "换张", "换两张", "换一批", "换一组", "再来一张", "再来个", "再来两张", "再来几张", "再来点",
        "推荐几张", "推荐张", "推荐个", "求几张", "求张", "求个", "求点", "搞点", "弄点", "求"
    )

    image_kinds = (
        "图片", "猫图", "狗图", "照片", "壁纸", "手机壁纸", "电脑壁纸", "表情包", "表情",
        "插画", "插图", "头像", "截图", "动图", "gif", "美图", "原画", "立绘", "角色立绘",
        "cg", "海报", "写真", "图包", "同人图", "画", "二次元图"
    )
    video_kinds = ("视频", "短视频", "录像", "影片", "动画", "clip", "mv")
    file_kinds = ("文件", "资料", "pdf", "资料库", "群文件", "文档", "教程", "课件", "手册", "电子书")

    search = any(w in text for w in search_verbs)
    send = any(w in text for w in send_verbs)
    has_image = (any(w in lowered for w in image_kinds)
                 or bool(re.search(r"(?:猫猫|柴犬|风景|星空|二次元|可爱|美)图", text))
                 or bool(re.search(r"(?<![a-z0-9])(?:cg|evcg)(?![a-z0-9])", lowered)))
    has_video = any(w in lowered for w in video_kinds)
    has_file = any(w in lowered for w in file_kinds)
    has_kind = has_image or has_video or has_file

    if not command and not ((search or send) and has_kind):
        return None

    kind = "video" if has_video else "file" if has_file else "image"
    operation = "search_send" if (kind == "image" and not any(w in text for w in ("候选", "列表", "有哪些"))) or (kind == "file" and send) else "search"
    query = text
    if command:
        token, query = command[1].lower(), command[2].strip()
        kind = {"image": "image", "video": "video", "file": "file", "library": "file", "groupfile": "file"}[token]
        operation = "library" if token == "library" else "group_files" if token == "groupfile" else "search_send" if token == "image" else "search"
    elif "群文件" in text:
        operation = "group_files"
    elif "资料库" in text:
        operation = "library"
    count_match = re.search(r"([一二两三四五六七八九十\d]+)\s*(?:张|个|条|份)", query)
    if count_match:
        count = number(count_match[1])
    elif re.search(r"(?:发|找|来|整|要|换|再来)[个张幅]", text):
        count = 1
    elif re.search(r"几[张个条份]", text):
        count = default_count if kind == "image" else 3
    else:
        count = default_count if kind == "image" else 3
    orientation = "portrait" if "竖屏" in text or "竖图" in text else "landscape" if "横屏" in text or "横图" in text else ""
    ratio = re.search(r"(?<!\d)([1-9]\d?):([1-9]\d?)(?!\d)", query)
    duration = re.search(r"([一二两三四五六七八九十\d]+)\s*(分钟|秒)(?:以内|以下|内)?", query)
    max_duration = number(duration[1]) * (60 if duration[2] == "分钟" else 1) if duration else 0
    if ratio:
        query = query.replace(ratio[0], "")
    if duration:
        query = query.replace(duration[0], "")
    query = re.sub(r"([一二两三四五六七八九十几\d]+)\s*(张|个|条|份)", "", query)
    phrases = (
        "网络搜索一下", "网上搜索一下", "网络搜索", "网上搜索", "网络检索", "在网上搜", "网上搜下",
        "帮我", "请帮我", "麻烦帮我", "给我", "发给我", "发我", "发来", "发过来", "发到群里", "发出来", "发到群",
        "发个", "发张", "发两张", "发几张", "发点", "下载后", "直接", "最新那份", "最新的",
        "我想看", "想看", "看看", "看下", "整点", "整张", "整几个", "整两张",
        "来点", "来张", "来个", "来两张", "来几张", "推荐几张", "推荐张", "推荐个", "求几张", "求张", "求个", "求点", "求", "搞点", "弄点",
        "想要", "我要", "要看", "要张", "要个", "要两张", "要几张", "要点",
        "换一张", "换个", "换张", "换两张", "换一批", "换一组", "再来一张", "再来个", "再来两张", "再来几张", "再来点",
        "群文件里的", "群文件里", "群文件", "资料库里的", "资料库里", "资料库", "群里",
        "搜索一下", "查找一下", "搜索", "查找", "查一下", "找一下", "搜搜", "找找", "查查", "搜下", "找下", "查下",
        "搜", "找", "查", "发", "看", "下载",
        "图片", "照片", "视频", "短视频", "文件", "文档", "资料", "教程",
        "以内", "以下", "表情包", "手机壁纸", "电脑壁纸", "壁纸", "插画", "头像", "美图",
        "风景图", "柴犬图", "猫猫图", "柴犬照片"
    )
    query = (
        query.replace("猫猫图片", "猫")
        .replace("猫图片", "猫")
        .replace("狗图片", "狗")
        .replace("柴犬图片", "柴犬")
        .replace("猫猫图", "猫")
        .replace("猫图", "猫")
        .replace("狗图", "狗")
        .replace("柴犬图", "柴犬")
        .replace("风景图", "风景")
        .replace("星空图", "星空")
    )
    for phrase in sorted(phrases, key=len, reverse=True):
        query = query.replace(phrase, " ")
    query = re.sub(r"^(?:请|把|给|发|的)\s*|(?:发|的)(?:来|给)?$", "", query.strip()).strip(" ，。？！!")
    query = query.replace("的", " ")
    cleaned_query = re.sub(r"\s+", " ", query).strip()
    if not cleaned_query:
        for kw in ("壁纸", "手机壁纸", "电脑壁纸", "头像", "表情包", "插画", "美图", "风景", "猫", "狗"):
            if kw in text:
                cleaned_query = kw
                break
    return MediaIntent(operation=operation, kind=kind, query=cleaned_query, request_text=text,
                       count=max(1, count), count_requested=bool(count_match),
                       orientation=orientation, aspect_ratio=ratio[0] if ratio else "", max_duration=max_duration,
                       as_file="作为文件" in text, send_requested=send, latest="最新" in text)
