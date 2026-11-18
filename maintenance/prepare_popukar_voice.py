"""Install the verified Japanese Popukar v4 model and its bundled reference WAV."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
from urllib.parse import quote
import wave
import zipfile

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

COLLECTION = "aihobbyist/GPT-SoVITS_Model_Collection"
MODEL_PATH = "明日方舟/日语/泡普卡.zip"
MODEL_SHA256 = "7806e5daf0f841054a53210ed52160c841a2a62558f11bc9fecce8940b18dc4c"
BASE_REVISION = "8f8857563c040d542de108a2c18b0425abd6c9c5"
BASE_ASSETS = {
    "s2Gv4.pth": "906fe22f48c3e037a389df291d4d32a9414e15dbb8f9628643e83aaced109ea4",
    "vocoder.pth": "4d611913df7b12d49e8976c944558d2d096816365edfc6c35a9e85b67dd14ed9",
}
SOURCE = "https://www.ai-hobbyist.com/thread-964-1-1.html"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, target, expected):
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file() or digest(target) != expected:
        temporary = target.with_name(target.name + ".part")
        print("下载：" + target.name, flush=True)
        with requests.get(url, stream=True, timeout=(30, 180)) as response:
            response.raise_for_status()
            with temporary.open("wb") as output:
                size = 0
                for block in response.iter_content(1024 * 1024):
                    output.write(block)
                    size += len(block)
                    if size // (64 * 1024 * 1024) != (size - len(block)) // (64 * 1024 * 1024):
                        print(f"{target.name}: {size // (1024 * 1024)} MiB", flush=True)
        if digest(temporary) != expected:
            raise ValueError("下载文件 SHA-256 不匹配：" + str(target))
        temporary.replace(target)
    print("校验通过：" + target.name, flush=True)
    return {"url": url, "path": str(target), "sha256": expected, "size": target.stat().st_size}


def install(home=ROOT):
    home = Path(home).resolve()
    engine = home / "tools/voice-engine/GPT-SoVITS-Inference"
    model = home / "data/voice/models/popukar"
    archive_path = home / "data/voice/downloads/popukar-ja-v4.zip"
    jobs = [(f"https://modelscope.cn/models/{COLLECTION}/resolve/master/{quote(MODEL_PATH)}",
             archive_path, MODEL_SHA256)]
    for name, expected in BASE_ASSETS.items():
        relative = "gsv-v4-pretrained/" + name
        jobs.append((f"https://huggingface.co/lj1995/GPT-SoVITS/resolve/{BASE_REVISION}/{relative}",
                     engine / "GPT_SoVITS/pretrained_models" / relative, expected))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(lambda job: download(*job), jobs))

    model.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, metadata_encoding="gb18030") as archive:
        for member in archive.infolist():
            # This publisher's ZIP uses Windows GBK names without the UTF-8 flag.
            name = member.filename
            relative = PurePosixPath(name)
            if (relative.is_absolute() or relative.parts[:2] != ("v4", "泡普卡")
                    or ".." in relative.parts or "\\" in name or ":" in name):
                if member.is_dir() and name == "v4/":
                    continue
                raise ValueError("压缩包包含非预期路径：" + name)
            target = model.joinpath(*relative.parts[2:])
            if not target.resolve().is_relative_to(model):
                raise ValueError("压缩包路径超出模型目录。")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)

    references = list(model.rglob("*.wav"))
    if len(references) != 1:
        raise ValueError("模型参考音频数量与已核验版本不符。")
    reference = references[0]
    with wave.open(str(reference), "rb") as audio:
        audio_info = {"duration": audio.getnframes() / audio.getframerate(),
                      "sample_rate": audio.getframerate(), "channels": audio.getnchannels()}
    gpt, sovits = model / "泡普卡-e10.ckpt", model / "泡普卡_e10_s160_l32.pth"
    if not gpt.is_file() or not sovits.is_file():
        raise ValueError("模型缺少已核验权重。")
    metadata = {"character": "popukar", "name": "泡普卡（日语）", "version": "v4", "source": SOURCE,
                "cv": "桜咲千依", "cv_source": "https://cat222.jp/", "files": records,
                "reference_audio": str(reference), "reference_audio_info": audio_info,
                "reference_text_source": "https://prts.wiki/w/泡普卡/语音记录",
                "reference_text_note": "Use the original game line; the ZIP filename has ASR spelling errors.",
                "local_files": [{"path": str(p), "sha256": digest(p), "size": p.stat().st_size}
                                for p in (gpt, sovits, reference)]}
    (model / "SOURCE.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    from ui.studio_api import StudioAPI
    api = StudioAPI(home)
    current = api.voice("catalog")
    if not current["ok"]:
        raise ValueError(current["error"])
    catalog = current["data"]["catalog"]
    catalog["characters"]["popukar"] = {
        "name": "泡普卡（日语）", "gpt_weights": str(gpt), "sovits_weights": str(sovits),
        "source": SOURCE, "license": "仅限非商业二次创作；禁止二次分发；遵守发布帖说明",
        "attribution": "红血球AE3803 & 白菜工厂1145号员工；数据/角色：鹰角网络；日语 CV：桜咲千依",
        "emotions": {"neutral": {"reference_audio": str(reference),
            "prompt_text": "ポプカル、どうすればいいのか知ってるよ。", "prompt_language": "ja",
            "description": "模型自带默认日语切片；仅此一个情绪样本"}},
    }
    saved = api.voice("save_catalog", {"catalog": catalog, "revision": current["data"]["revision"]})
    if not saved["ok"]:
        raise ValueError(saved["error"])

    original_settings = yaml.safe_load((home / "tools/voice-engine/atri-infer.yaml").read_text(encoding="utf-8"))
    custom = dict(original_settings["custom"])
    custom.update(version="v4", t2s_weights_path=str(gpt), vits_weights_path=str(sovits))
    settings_path = home / "tools/voice-engine/popukar-infer.yaml"
    settings_path.write_text(yaml.safe_dump({"custom": custom}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    result = {"ok": True, "character": "popukar", "engine_config": str(settings_path),
              "catalog": str(home / "data/voice/catalog.json"), "metadata": str(model / "SOURCE.json"),
              "reference": audio_info, "config_changed": False}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    install()
