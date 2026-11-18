"""Portable, reviewable speech datasets with source-level train/holdout separation."""
from __future__ import annotations
from array import array
from collections import Counter
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
import time
import uuid
import wave


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", value):
        raise ValueError("ID 需为 1–80 位字母、数字、下划线或短横线。")
    return value


def audio_quality(path):
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ValueError("训练样本需为单声道 16bit WAV。")
        duration = audio.getnframes() / audio.getframerate()
        samples = array("h", audio.readframes(audio.getnframes()))
    if not samples:
        raise ValueError("音频为空。")
    peak = max(abs(s) for s in samples) / 32768
    rms = math.sqrt(sum(s*s for s in samples) / len(samples)) / 32768
    return {"duration": duration, "peak": peak, "rms_db": 20*math.log10(max(rms, 1e-10)),
            "clipped_ratio": sum(abs(s) >= 32760 for s in samples) / len(samples)}


def read_annotations(path):
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("请选择小于 32MB 的 CSV、JSONL 或 .list 标注文件。")
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".csv":
        rows = list(csv.DictReader(io.StringIO(text)))
    elif path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif path.suffix.lower() == ".list":
        rows = []
        for index, line in enumerate(text.splitlines()):
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) not in (4, 5):
                raise ValueError(f"第 {index+1} 行需要 音频|角色|语言|台词[|情绪]。")
            rows.append(dict(zip(("audio", "speaker", "language", "text", "emotion"), parts)))
    else:
        raise ValueError("支持 CSV、JSONL 和 GPT-SoVITS .list。")
    if not rows or len(rows) > 50000 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("标注文件需要 1–50000 条对象记录。")
    return rows


def import_dataset(root, annotation, *, dataset_id="", speaker="", name="", ffmpeg="ffmpeg", stop=None, progress=None,
                   source="", license_text="", attribution=""):
    dataset_id = identifier(dataset_id or ("dataset-" + uuid.uuid4().hex[:12]))
    root, annotation = Path(root).resolve(), Path(annotation).resolve()
    target = root / "datasets" / dataset_id
    target.mkdir(parents=True, exist_ok=False)
    (target / "audio").mkdir()
    manifest = {"version": 1, "id": dataset_id, "name": name or dataset_id, "state": "importing",
                "created_at": time.time(), "source": source, "license": license_text, "attribution": attribution,
                "rows": [], "rejected": [], "annotation_source": str(annotation)}
    seen = set()
    try:
        rows = read_annotations(annotation)
        speakers = {str(row.get("speaker", speaker)).strip() for row in rows}
        if not speaker and (len(speakers) != 1 or not next(iter(speakers))):
            raise ValueError("每个数据集只训练一个角色；请填写角色 ID 或分别导入不同说话人。")
        speaker = identifier(speaker or next(iter(speakers)))
        for index, row in enumerate(rows):
            if stop and stop.is_set():
                from emotion_training import TrainingCancelled
                raise TrainingCancelled()
            if progress:
                progress({"stage": "importing", "index": index+1, "total": len(rows)})
            wav = None
            try:
                text = str(row.get("text", "")).strip()
                if not text or len(text) > 2000 or any(c in text for c in "|\t\n\r\x00"):
                    raise ValueError("台词为空、过长或包含列表分隔符。")
                language = str(row.get("language", "ja")).lower()
                language = {"jp": "ja", "zh-cn": "zh", "cn": "zh"}.get(language, language)
                if language not in ("zh", "ja", "en", "ko", "yue"):
                    raise ValueError("语言需要 ja/zh/en/ko/yue。")
                original_speaker = str(row.get("speaker", speaker)).strip()
                if original_speaker and original_speaker != speaker:
                    raise ValueError("样本的说话人与本数据集角色不一致。")
                emotion = str(row.get("emotion") or "neutral").strip()
                identifier(emotion)
                from voice_emotions import annotation_attributes
                attributes = annotation_attributes(row)
                original = Path(str(row.get("audio") or row.get("path") or "")).expanduser()
                original = (original if original.is_absolute() else annotation.parent / original).resolve()
                if not original.is_file() or not 0 < original.stat().st_size <= 256*1024*1024:
                    raise ValueError("本地音频不存在、为空或超过 256MB。")
                source_hash = file_hash(original)
                # The normalized PCM digest also deduplicates different source codecs.
                wav = target / "audio" / (uuid.uuid4().hex + ".wav")
                flags = {"creationflags": 0x08000000} if os.name == "nt" else {}
                result = subprocess.run([str(ffmpeg), "-nostdin", "-v", "error", "-y",
                    "-protocol_whitelist", "file,pipe", "-i", str(original), "-vn", "-t", "31",
                    "-ar", "32000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                    capture_output=True, timeout=45, **flags)
                if result.returncode:
                    raise ValueError("音频转换失败：" + result.stderr.decode("utf-8",errors="replace")[-300:])
                quality = audio_quality(wav)
                if not 1 <= quality["duration"] <= 30:
                    raise ValueError("单条切片需为 1–30 秒；请先执行自动切分与转写。")
                if quality["rms_db"] < -50 or quality["clipped_ratio"] > .02:
                    raise ValueError("音频接近静音或有明显削波，需要重新处理。")
                with wave.open(str(wav), "rb") as stream:
                    pcm_hash = hashlib.sha256(stream.readframes(stream.getnframes())).hexdigest()
                if pcm_hash in seen:
                    raise ValueError("重复音频（已去重）。")
                seen.add(pcm_hash)
                identity = pcm_hash[:24]
                final = wav.with_name(identity + ".wav")
                wav.rename(final)
                reviewed = row.get("reviewed", True)
                if isinstance(reviewed, str):
                    reviewed = reviewed.strip().lower() in ("true", "yes", "1")
                annotation_meta = row.get("annotation") or {"method": "imported", "needs_review": not reviewed}
                manifest["rows"].append({"id": identity, "audio": "audio/" + final.name,
                    "text": text, "speaker": speaker, "language": language, "emotion": emotion, **attributes,
                    "sha256": file_hash(final), "pcm_sha256": pcm_hash, **quality,
                    "source": row.get("source", source or str(original)),
                    "source_group": str(row.get("source_group") or row.get("source_sha256") or source_hash),
                    "source_sha256": row.get("source_sha256", source_hash),
                    "reviewed": bool(reviewed), "annotation": annotation_meta,
                    "asr": row.get("asr"), "start": row.get("start"), "end": row.get("end")})
            except (OSError, ValueError, subprocess.SubprocessError, wave.Error) as error:
                if wav:
                    wav.unlink(missing_ok=True)
                manifest["rejected"].append({"index": index+1, "audio": str(row.get("audio", "")), "reason": str(error)})
        if len(manifest["rows"]) < 3:
            raise ValueError("有效切片不足 3 条，无法建立训练/留出集；请检查拒收记录。")
        manifest.update(state="ready", speaker=speaker)
        manifest["summary"] = summarize(manifest)
        atomic_json(target / "manifest.json", manifest)
        return manifest
    except BaseException as error:
        manifest.update(state="cancelled" if type(error).__name__ == "TrainingCancelled" else "failed", error=str(error))
        atomic_json(target / "manifest.json", manifest)
        raise


def summarize(manifest):
    rows = manifest["rows"]
    return {"count": len(rows), "seconds": sum(r["duration"] for r in rows),
            "emotions": dict(Counter(r["emotion"] for r in rows)),
            "languages": dict(Counter(r["language"] for r in rows)),
            "unreviewed": sum(not r.get("reviewed") for r in rows),
            "sources": len({r["source_group"] for r in rows}), "rejected": len(manifest.get("rejected", []))}


def load_dataset(root, identity, *, verify=False):
    folder = Path(root).resolve() / "datasets" / identifier(identity)
    value = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if value.get("state") != "ready":
        raise ValueError("数据集尚未成功导入。")
    for row in value["rows"]:
        path = (folder / row["audio"]).resolve()
        if not path.is_relative_to(folder.resolve()) or not path.is_file():
            raise ValueError("数据集音频丢失或路径超出数据集目录。")
        if verify and file_hash(path) != row["sha256"]:
            raise ValueError("数据集音频已被修改，请重新导入：" + row["id"])
    return value


def split_dataset(rows, *, ratio=.15, seed=42):
    # Connected source groups share a partition even if identical dialogue was
    # exported in different files. No slicing or re-encoding crosses the holdout.
    parents = {}
    def find(key):
        parents.setdefault(key, key)
        if parents[key] != key:
            parents[key] = find(parents[key])
        return parents[key]
    texts = {}
    for row in rows:
        group = find(row["source_group"])
        text = re.sub(r"\W", "", row["text"]).casefold()
        if text in texts:
            parents[find(group)] = find(texts[text])
        texts[text] = group
    groups = {}
    for row in rows:
        groups.setdefault(find(row["source_group"]), []).append(row)
    if len(groups) < 2:
        raise ValueError("独立音源不足 2 组，无法建立无泄漏留出集。请添加另一段独立录音。")
    keys = sorted(groups)
    random.Random(seed).shuffle(keys)
    target = max(1, round(len(rows)*ratio))
    validation = []
    for key in keys[:-1]:
        if len(validation) >= target:
            break
        validation += groups[key]
    held = {row["id"] for row in validation}
    training = [row for row in rows if row["id"] not in held]
    if len(training) < 2:
        raise ValueError("训练切片不足 2 条，请补充独立音源。")
    return {"train": [r["id"] for r in training], "validation": [r["id"] for r in validation],
            "seed": seed, "ratio": ratio, "method": "source-and-identical-text-connected-groups"}


def update_annotations(root, identity, edits, expected_revision):
    folder = Path(root).resolve() / "datasets" / identifier(identity)
    path = folder / "manifest.json"
    if file_hash(path) != expected_revision:
        raise ValueError("数据集已更新，请重新载入标注。")
    dataset = load_dataset(root, identity)
    by_id = {r["id"]: r for r in dataset["rows"]}
    for edit in edits:
        if edit.get("id") not in by_id or set(edit) - {"id", "text", "emotion", "delivery", "intensity", "reviewed"}:
            raise ValueError("标注包含未知样本或字段。")
        text = str(edit.get("text", by_id[edit["id"]]["text"])).strip()
        if not text or any(c in text for c in "|\t\n\r\x00"):
            raise ValueError("请填写有效的单行台词。")
        emotion = identifier(edit.get("emotion", by_id[edit["id"]]["emotion"]))
        row = by_id[edit["id"]]
        from voice_emotions import annotation_attributes
        attributes = annotation_attributes({**row, **edit})
        row.update(text=text, emotion=emotion, **attributes)
        if "reviewed" in edit:
            if type(edit["reviewed"]) is not bool:
                raise ValueError("reviewed 必须为布尔值。")
            row["reviewed"] = edit["reviewed"]
        row["annotation"] = {"method": "human-review", "needs_review": not row["reviewed"], "updated_at": time.time()}
    dataset["summary"] = summarize(dataset)
    atomic_json(path, dataset)
    return dataset
