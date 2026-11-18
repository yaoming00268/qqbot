"""Shared Chinese labels for speech annotation and reference selection."""
from __future__ import annotations


def _choices(group, rows):
    return [{"value": key, "label": label, "description": description, "group": group}
            for key, label, description in rows]


EMOTIONS = (
    _choices("平静与亲近", [
        ("neutral", "自然", "不刻意强调情绪，接近日常说话。"),
        ("calm", "平静", "语气稳定、放松，起伏较小。"),
        ("gentle", "温柔", "柔和地表达关心，避免强硬语气。"),
        ("warm", "温暖", "友善而有亲近感，让人感到被接纳。"),
        ("caring", "关切", "带着在意、询问或照顾对方的情绪。"),
        ("comforting", "安慰", "安抚、鼓励处于低落或紧张中的人。"),
        ("affectionate", "亲昵", "亲近、依恋，带有柔软的感情。"),
        ("grateful", "感激", "真诚表达感谢和珍惜。"),
        ("relieved", "释然", "紧张解除后的放松和安心。"),
    ]) + _choices("愉快与活力", [
        ("happy", "开心", "积极愉快，语调轻快。"),
        ("cheerful", "开朗", "明朗活泼，保持有精神的语气。"),
        ("excited", "兴奋", "期待或激动，节奏与语调起伏较大。"),
        ("playful", "俏皮", "轻松玩笑，带一点调皮。"),
        ("teasing", "打趣", "善意逗弄对方，带揶揄的语气。"),
        ("proud", "得意", "对成果或自己感到满意、自豪。"),
        ("confident", "自信", "笃定、清晰，表达有把握。"),
        ("hopeful", "期待", "对即将发生的事情抱有希望。"),
        ("amused", "觉得好笑", "被逗乐，隐约带着笑意。"),
    ]) + _choices("害羞与犹豫", [
        ("shy", "害羞", "羞涩、收敛，表达略带迟疑。"),
        ("embarrassed", "难为情", "因为被关注或出糗而不自在。"),
        ("flustered", "慌乱", "突然失去镇定，话语略显急促。"),
        ("hesitant", "犹豫", "拿不定主意，停顿和试探较多。"),
        ("pouting", "闹别扭", "轻微不满，像赌气或嘟囔。"),
        ("tsundere", "傲娇", "表面逞强或否认，内里带着在意。"),
        ("apologetic", "歉意", "诚恳认错或表达抱歉。"),
    ]) + _choices("低落与压力", [
        ("sad", "难过", "悲伤、失落，声音较沉。"),
        ("lonely", "孤单", "寂寞、缺少陪伴的感觉。"),
        ("disappointed", "失望", "期待落空，带沮丧或泄气。"),
        ("worried", "担心", "在意不确定的结果，语气忧虑。"),
        ("anxious", "焦虑", "不安、急切，难以放松。"),
        ("afraid", "害怕", "受到威胁或惊吓，带退缩感。"),
        ("tired", "疲惫", "缺乏精力，节奏和力度较低。"),
        ("sleepy", "困倦", "将睡未睡，语气松缓。"),
    ]) + _choices("惊讶与冲突", [
        ("surprised", "惊讶", "对意外情况作出反应，语调上扬。"),
        ("curious", "好奇", "对信息感兴趣，想继续了解。"),
        ("confused", "困惑", "不理解或摸不着头脑。"),
        ("skeptical", "怀疑", "对说法持保留态度，带质疑。"),
        ("serious", "认真", "严肃专注，强调事情的重要性。"),
        ("determined", "坚定", "下定决心，语气有力量。"),
        ("annoyed", "不耐烦", "轻度烦躁、不满，耐心不足。"),
        ("angry", "生气", "明确的愤怒或强烈不满。"),
        ("frustrated", "懊恼", "受阻后的烦闷、挫败。"),
        ("sarcastic", "讽刺", "用反话或冷淡语气表达不满。"),
    ])
)
DELIVERIES = _choices("说话方式", [
    ("normal", "正常说话", "使用样本原有的自然发声。"),
    ("soft", "轻声", "降低发声力度，保持清楚可闻。"),
    ("whisper", "耳语", "贴近耳语的气声，音量较低。"),
    ("breathy", "气声", "发声中带有较明显的呼气感。"),
    ("smiling", "带笑", "说话时带笑意，音色较明亮。"),
    ("laughing", "笑着说", "明显笑声或笑意伴随台词。"),
    ("crying", "哭腔", "带哽咽、鼻音或哭泣的发声。"),
    ("sobbing", "抽泣", "断续抽噎，停顿较多。"),
    ("shouting", "喊叫", "用力、高声地表达。"),
    ("murmuring", "低声嘟囔", "像自言自语，发声较低。"),
    ("sighing", "叹息", "带明显的叹气或无奈呼气。"),
    ("trembling", "颤抖", "发声有不稳定、颤动的感觉。"),
    ("clear", "清晰郑重", "咬字清楚，节奏明确。"),
    ("narration", "旁白", "平稳叙述，较少对话式的夸张。"),
    ("questioning", "询问", "带提问的上扬语调。"),
    ("singing", "歌唱", "样本含持续音高和旋律；不代表 TTS 已支持歌唱。"),
])
INTENSITIES = _choices("情绪强度", [
    ("subtle", "轻微", "情绪表现克制、含蓄。"),
    ("medium", "适中", "情绪清晰可辨，接近日常表达。"),
    ("strong", "强烈", "情绪突出，语调和力度变化较大。"),
    ("extreme", "非常强烈", "情绪达到高峰，具有明显戏剧性。"),
])


def presets():
    return {"emotions": EMOTIONS, "deliveries": DELIVERIES, "intensities": INTENSITIES}


def annotation_attributes(row):
    """Known dimensions are strict; the emotion ID remains open for old datasets."""
    result = {}
    for key, choices, default in (("delivery", DELIVERIES, "normal"), ("intensity", INTENSITIES, "medium")):
        value = row.get(key) or default
        if value not in {item["value"] for item in choices}:
            raise ValueError("未知的发声方式或情绪强度：" + str(value))
        result[key] = value
    return result


def describe(emotion, delivery="normal", intensity="medium"):
    names = {item["value"]: item["label"] for item in EMOTIONS + DELIVERIES + INTENSITIES}
    return " · ".join(names.get(value, value) for value in (emotion, delivery, intensity))
