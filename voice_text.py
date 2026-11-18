"""Deterministic language spans, Japanese readings and natural-boundary chunks."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import re
import threading
import unicodedata

from voice_config import VoiceConfig


class VoiceError(ValueError):
    code = "VOICE_FAILED"


@dataclass
class VoiceChunk:
    language: str
    source: str
    text: str
    tokens: list[str]
    emotion: str = ""
    boundary: str = "sentence"
    pause_before_ms: int = 0
    prosody: str = "normal"
    speed: float = 1.0

    def to_dict(self):
        return asdict(self)


_KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa]")
_TAG = re.compile(r"\[(ja|zh|en)\](.*?)\[/\1\]", re.S | re.I)
_CONTROL = re.compile(r"\[(?P<language>/?(?:ja|zh|en))\]|\[emotion:(?P<emotion>[^\[\]\r\n]{0,100})\]|\[(?P<prosody>/?(?:fast|slow|stretch))\]", re.I)
_EMOTION = re.compile(r"\[emotion:([^\[\]\r\n]{0,100})\]", re.I)
_PROSODY = re.compile(r"\[(/?)(fast|slow|stretch)\]", re.I)
PROSODY_SPEEDS = {"normal": 1.0, "fast": 1.2, "slow": 0.8, "stretch": 0.65}
PROSODY_INSTRUCTION = ("按语境可选用 [fast]短句[/fast] 快读、[slow]短句[/slow] 慢读、"
    "[stretch]短词或语气音[/stretch] 拖长音；闭合后恢复原速，标签不朗读，可与情绪及语言标记组合。"
    "默认自然连贯地按句说话；仅在需要时少量使用，勿逐字加标记或把每个字换行。")


def inline_prosody(text):
    """Validate every scope before a message is queued or audio is produced."""
    matches = list(_PROSODY.finditer(text))
    if re.search(r"\[/?(?:fast|slow|stretch)\b", _PROSODY.sub("", text), re.I):
        raise VoiceError("语速标记不完整；使用 [fast]…[/fast]、[slow]…[/slow] 或 [stretch]…[/stretch]。")
    if len(matches) > 128:
        raise VoiceError("一句语音最多 64 组语速标记。")
    active, start = "", 0
    for match in matches:
        closing, name = match.group(1), match.group(2).lower()
        if closing:
            if active != name:
                raise VoiceError("语速标记不匹配。")
            content = _CONTROL.sub("", text[start:match.start()])
            if not re.search(r"\w", content):
                raise VoiceError("语速标记内需要可朗读的文字。")
            active = ""
        elif active:
            raise VoiceError("语速标记不可嵌套；先闭合再切换。")
        else:
            active, start = name, match.end()
    if active:
        raise VoiceError("语速标记未闭合：" + active)
    return list(dict.fromkeys(match.group(2).lower() for match in matches))


def strip_controls(text):
    """Plain captions keep the words, without performance markup."""
    return _CONTROL.sub("", text)


def _unwrap_lines(source, language):
    def join(match):
        adjacent = source[match.start() - 1] + source[match.end()]
        return " " if language == "en" or language == "mixed" and adjacent.isascii() else ""
    return re.sub(r"(?<=\w)[ \t]*\r?\n[ \t]*(?=\w)", join, source)


def inline_emotions(text, default_emotion, available=None):
    """Validate all emotion switches before queue submission or any synthesis."""
    tags = [match.group(1).strip() or default_emotion for match in _EMOTION.finditer(text)]
    if re.search(r"\[/?emotion\b", _EMOTION.sub("", text), re.I):
        raise VoiceError("情绪标记不完整；使用 [emotion:标签]，用 [emotion:] 恢复默认。")
    if len(tags) > 64:
        raise VoiceError("一句语音最多 64 个情绪切换。")
    labels = list(dict.fromkeys([default_emotion, *tags]))
    if available is not None and any(label not in available for label in labels):
        raise VoiceError("情绪标签没有对应的已配置样本：" + ", ".join(str(x) for x in labels if x not in available))
    return labels


def performance_spans(text, language, default_emotion):
    """Persistent emotion switches compose with explicit mixed-language scopes."""
    inline_emotions(text, default_emotion)
    inline_prosody(text)
    explicit = bool(re.search(r"\[/?(?:ja|zh|en)\]", text, re.I))
    if explicit and language != "mixed":
        raise VoiceError("语言标记只适用于 mixed 混语模式。")
    spans, current_language, emotion, end, boundary = [], None, default_emotion, 0, "start"
    prosody = "normal"
    def append(source):
        # Unwrap before mixed-language inference, so visual CJK line wraps
        # cannot turn one sentence into many independent synthesis spans.
        source = _unwrap_lines(source, current_language or language)
        if not source.strip():
            return
        if explicit:
            if not current_language:
                raise VoiceError("混语显式标记须将全部正文放入 [ja]、[zh] 或 [en] 范围。")
            parts = [(current_language, source)]
        else:
            parts = language_spans(source, language)
        for lang, part in parts:
            spans.append((lang, part, emotion, boundary, prosody))
    for match in _CONTROL.finditer(text):
        append(text[end:match.start()])
        tag = match.group("language")
        if tag:
            if tag.startswith("/"):
                if current_language != tag[1:].lower():
                    raise VoiceError("混语语言标记不匹配。")
                current_language = None
            elif current_language:
                raise VoiceError("混语语言标记不可嵌套。")
            else:
                current_language = tag.lower()
            boundary = "language"
        elif match.group("prosody"):
            tag = match.group("prosody").lower()
            prosody = "normal" if tag.startswith("/") else tag
            boundary = "prosody"
        else:
            emotion = match.group("emotion").strip() or default_emotion
            boundary = "emotion"
        end = match.end()
    append(text[end:])
    if current_language:
        raise VoiceError("混语语言标记不完整。")
    return spans
_LETTERS = dict(zip("abcdefghijklmnopqrstuvwxyz", ["えー", "びー", "しー", "でぃー", "いー", "えふ", "じー", "えいち", "あい", "じぇー", "けー", "える", "えむ", "えぬ", "おー", "ぴー", "きゅー", "あーる", "えす", "てぃー", "ゆー", "ぶい", "だぶりゅー", "えっくす", "わい", "ぜっと"]))
_DIGITS = ["ぜろ", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう"]
_SYMBOLS = {"%": "ぱーせんと", "&": "あんど", "+": "ぷらす", "=": "いこーる", "@": "あっと", "#": "しゃーぷ", "¥": "えん", "$": "どる", "/": "すらっしゅ"}
_PUNCT = set("。、！？!?…,.，:：;；ー〜～—-「」『』（）()【】\"' \n\t・")


def _number_reading(match):
    value = match.group()
    if "." in value:
        left, right = value.split(".", 1)
        return _number_reading(re.match(r"\d+", left)) + "てん" + "".join(_DIGITS[int(x)] for x in right)
    if len(value) > 8 or len(value) > 1 and value[0] == "0":
        return "".join(_DIGITS[int(x)] for x in value)
    number = int(value)
    if not number:
        return "ぜろ"
    def small(n):
        out = ""
        for unit, name, exceptions in [(1000, "せん", {3: "さんぜん", 8: "はっせん"}),
                                       (100, "ひゃく", {3: "さんびゃく", 6: "ろっぴゃく", 8: "はっぴゃく"}),
                                       (10, "じゅう", {})]:
            count, n = divmod(n, unit)
            if count:
                out += exceptions.get(count, ("" if count == 1 else _DIGITS[count]) + name)
        return out + (_DIGITS[n] if n else "")
    high, low = divmod(number, 10000)
    return (small(high) + "まん" if high else "") + (small(low) if low else "")


def japanese_tokens(text, dictionary=None):
    """Only kana and punctuation leave this function, including Latin/numeric input."""
    # Both the dialogue frontend and the local bridge see the same prompts.
    # Cache immutable readings; callers still receive a fresh, mutable list.
    entries = tuple(sorted((dictionary or {}).items(), key=lambda x: -len(x[0])))
    return list(_japanese_tokens_cached(text, entries))


_CONVERTERS = threading.local()


@lru_cache(maxsize=512)
def _japanese_tokens_cached(text, entries):
    from pykakasi import kakasi
    text = unicodedata.normalize("NFKC", text)
    for word, reading in entries:
        text = text.replace(word, reading)
    text = re.sub(r"\d+(?:\.\d+)?", _number_reading, text)
    text = re.sub(r"[A-Za-z]", lambda m: _LETTERS[m.group().lower()], text)
    for symbol, reading in _SYMBOLS.items():
        text = text.replace(symbol, reading)
    tokens = []
    if not hasattr(_CONVERTERS, "kakasi"):
        _CONVERTERS.kakasi = kakasi()
    converted = _CONVERTERS.kakasi.convert(text)
    # Some dictionary frontends silently skip unsupported Unicode characters
    # (or repeat the previous token). Verify source coverage before trusting
    # their readings, so a successful synthesis never loses a word silently.
    if "".join(item["orig"] for item in converted) != text:
        raise VoiceError("日语分词未完整覆盖原文；请为生僻字补充读音词典或假名 spoken_text。")
    for item in converted:
        reading = item["hira"]
        reading = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in reading)
        # Formatting/symbols do not become silent missing words. Unsupported
        # characters are reported so the agent can supply an explicit reading.
        bad = [c for c in reading if not ("ぁ" <= c <= "ゖ" or c in _PUNCT)]
        if bad:
            raise VoiceError("日语读音未能转为假名：" + "".join(dict.fromkeys(bad))[:50] + "；请补充读音词典或 spoken_text。")
        if reading:
            tokens.append(reading)
    if not any(_KANA.search(token) for token in tokens):
        raise VoiceError("日语语音正文没有可朗读的假名。")
    return tuple(tokens)


def language_spans(text, language):
    if language != "mixed":
        if _TAG.search(text):
            raise VoiceError("语言标记只适用于控制台选择的 mixed 混语模式。")
        return [(language, text)]
    if re.search(r"\[/?(?:ja|zh|en)\]", text, re.I):
        spans, end = [], 0
        for match in _TAG.finditer(text):
            if text[end:match.start()].strip():
                raise VoiceError("混语显式标记模式须将全部正文放在 [ja]…[/ja]、[zh]…[/zh] 或 [en]…[/en] 中。")
            if not match.group(2).strip() or "[" in match.group(2):
                raise VoiceError("混语片段不能为空或嵌套。")
            spans.append((match.group(1).lower(), match.group(2)))
            end = match.end()
        if text[end:].strip() or not spans:
            raise VoiceError("混语语言标记不完整。")
        return spans
    spans = []
    for sentence in re.findall(r"[^。！？!?\n]+[。！？!?\n]*", text):
        # A Japanese sentence stays intact: its kanji inherit the kana context.
        lang = "ja" if _KANA.search(sentence) else "zh" if re.search(r"[\u3400-\u9fff]", sentence) else "en"
        spans.append((lang, sentence))
    return spans


def prepare_chunks(text, config: VoiceConfig, default_emotion="", *, clause_limit=None):
    text = str(text or "").strip()
    if not text or len(text) > config.max_text_chars:
        raise VoiceError(f"语音正文不能为空且不能超过 {config.max_text_chars} 字。")
    chunks = []
    limit = min(config.chunk_chars, clause_limit or config.chunk_chars)
    spans, prefix = [], ""
    for span in performance_spans(text, config.default_language, default_emotion):
        language, source, emotion, boundary, prosody = span
        if source and all(c in _PUNCT for c in source):
            if spans:
                previous = spans[-1]
                spans[-1] = (previous[0], previous[1] + source, *previous[2:])
            else:
                prefix += source
        else:
            spans.append((language, prefix + source, emotion, boundary, prosody))
            prefix = ""
    for language, source, emotion, boundary, prosody in spans:
        tokens = japanese_tokens(source, config.dictionary) if language == "ja" else re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|\s+|.", source, re.S)
        current = []
        def emit():
            if current and "".join(current).strip():
                pause = config.pause_ms if chunks and re.search(r"[。！？!?\n]\s*$", chunks[-1].text) else 0
                speed = round(max(0.5, min(2.0, config.speed * PROSODY_SPEEDS[prosody])), 4)
                chunks.append(VoiceChunk(language, source, "".join(current), list(current), emotion, boundary, pause, prosody, speed))
            current.clear()
        for token in tokens:
            # Rare long tokens still obey the synthesis hard limit.
            for start in range(0, len(token), limit):
                part = token[start:start + limit]
                if sum(map(len, current)) + len(part) > limit:
                    emit()
                current.append(part)
                if re.search(r"[。！？!?\n]$", part):
                    emit()
                    boundary = "sentence"
                elif clause_limit and re.search(r"[、，,;；:]$", part) and sum(map(len, current)) >= 10:
                    emit()
                    boundary = "clause"
        emit()
    # Keep tiny normal acknowledgements with their following sentence, and
    # rebalance a hard-limit tail so punctuation/one character is never sent
    # as a separate synthesis request. Explicit performance scopes stay exact.
    index = 0
    while index < len(chunks) - 1:
        left, right = chunks[index:index + 2]
        same = (left.language, left.emotion, left.prosody) == (right.language, right.emotion, right.prosody)
        tiny = len(re.findall(r"\w", left.text)) < 4
        if same and left.prosody == "normal" and tiny and len(left.text + right.text) <= limit:
            left.text += right.text
            left.tokens.extend(right.tokens)
            left.source = left.source if left.source == right.source else left.source + right.source
            chunks.pop(index + 1)
            continue
        if same and not re.search(r"[。！？!?\n]\s*$", left.text) and len(re.findall(r"\w", right.text)) < 4:
            while len(re.findall(r"\w", right.text)) < 4 and len(left.text) > 4 and len(right.text) < limit:
                # Prefer moving a complete reading/word to preserve pronunciation.
                tail = left.tokens[-1]
                if len(tail + right.text) > limit or len(left.text) - len(tail) < 4:
                    tail = left.text[-1:]
                    left.tokens[-1] = left.tokens[-1][:-1]
                    if not left.tokens[-1]:
                        left.tokens.pop()
                else:
                    left.tokens.pop()
                left.text = left.text[:-len(tail)]
                right.text = tail + right.text
                right.tokens.insert(0, tail)
        index += 1
    if not chunks or not any(re.search(r"[\w\u3041-\u30fa]", chunk.text) for chunk in chunks):
        raise VoiceError("没有可朗读的语音正文。")
    return chunks


def completion_limit(character):
    """Tiny fine-tunes may emit EOS after an early clause, despite valid WAV EOF."""
    value = character.get("synthesis", {}).get("max_chunk_chars")
    if value is None:
        return 32 if character.get("experimental") else None
    if type(value) is not int or not 20 <= value <= 100:
        raise VoiceError("角色的 max_chunk_chars 需为 20–100 的整数。")
    return value
