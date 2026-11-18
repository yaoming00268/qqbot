"""Keep verified training references independent from disposable datasets."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import uuid
import wave

from voice_dataset import file_hash
from voice_emotions import annotation_attributes, describe


def training_reference(source, row, directory, dataset_id):
    source, directory = Path(source), Path(directory)
    if file_hash(source) != row["sha256"]:
        raise ValueError("训练切片内容已变化，请重新导入后选择参考。")
    with wave.open(str(source), "rb") as stream:
        rate, channels, width = stream.getframerate(), stream.getnchannels(), stream.getsampwidth()
        count = stream.getnframes()
        pcm = stream.readframes(count)
    if channels != 1 or width != 2 or not 8000 <= rate <= 96000 or len(pcm) != count*2:
        raise ValueError("参考切片必须为完整的单声道 PCM16 WAV。")
    duration = count / rate
    if not 1 <= duration <= 10:
        raise ValueError("参考音频需要 1–10 秒；超过 10 秒请重新切分并核对台词，不会直接裁掉尾部。")
    if row.get("language") not in ("ja", "zh", "en"):
        raise ValueError("当前参考接口支持日语、中文和英语。")
    text = str(row.get("text", "")).strip()
    if not text or len(text) > 2000 or any(c in text for c in "|\n\r\t\0"):
        raise ValueError("请先填写与切片逐字对应的单行台词。")
    attributes = annotation_attributes(row)
    padding = max(0, round(3.1*rate)-count)
    output_pcm = pcm + bytes(padding*2)
    digest = hashlib.sha256(str(rate).encode()+output_pcm).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (digest[:32]+".wav")
    temporary = target.with_name(target.stem+"."+uuid.uuid4().hex+".tmp")
    try:
        with wave.open(str(temporary), "wb") as stream:
            stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            stream.writeframes(output_pcm)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"reference_audio": str(target), "prompt_text": text, "prompt_language": row["language"],
        "description": describe(row["emotion"], **attributes) + (" · 待复核" if not row.get("reviewed") else ""),
        "training_dataset_id": dataset_id, "training_sample_id": row["id"],
        "source_sha256": row["sha256"], "sha256": file_hash(target),
        "original_duration": duration, "duration": (count+padding)/rate, "padding_seconds": padding/rate,
        "reviewed": row.get("reviewed", False), **attributes}
