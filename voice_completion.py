"""Conservative duration sanity check, not a transcript accuracy score."""
from __future__ import annotations

import re
import wave


def inspect_chunk(path, text, language, speed=1):
    with wave.open(str(path), "rb") as stream:
        duration = stream.getnframes()/stream.getframerate()
    if language == "ja":
        units = len(re.findall(r"[ぁ-ゖァ-ヺー]", text)) - len(re.findall(r"[ゃゅょぁぃぅぇぉャュョァィゥェォ]", text))
        minimum = units / 20 / speed
    elif language == "zh":
        minimum = len(re.findall(r"[\u3400-\u9fff]", text)) / 12 / speed
    elif language == "en":
        minimum = len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)) / 7 / speed
    else:
        minimum = 0
    return {"duration": round(duration,4), "minimum": round(minimum,4),
            "suspected_early_end": duration + .08 < minimum,
            "method": "conservative-duration-sanity; not ASR verification"}
