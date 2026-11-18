"""Reproducible independent GPT-SoVITS/ATRI environment; no bot process control."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import io
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests
import yaml

ENGINE_URL = "https://github.com/AI-Hobbyist/GPT-SoVITS-Inference"
ENGINE_COMMIT = "e030403bcafbd26bb3eb8ac376226e7ccaaec8c0"
SAMPLE_REPO = "2DIPW/ATRI_GPT-SoVITS"
SAMPLE_REVISION = "85b2af13fe867ec74ca497b0c367d3811a0f849c"
BASE_REPO = "lj1995/GPT-SoVITS"
BASE_REVISION = "8f8857563c040d542de108a2c18b0425abd6c9c5"
ATTRIBUTION = "bilibili@二维环状无限深势阱 (2DIPW)"
NLTK_REVISION = "550b6625bcef1f2abff2ff770a5a0d272c9c6b2a"


def progress(value):
    print(value, file=sys.stderr, flush=True)


def run(argv, *, cwd):
    result = subprocess.run([str(value) for value in argv], cwd=cwd, stdout=sys.stderr, stderr=sys.stderr,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(f"安装步骤失败（退出码 {result.returncode}）：{argv[0]}")


def download_sample(home, engine):
    model = home / "data/voice/models/atri"
    items = []
    for repo, revision in ((SAMPLE_REPO, SAMPLE_REVISION), (BASE_REPO, BASE_REVISION)):
        response = requests.get(f"https://huggingface.co/api/models/{repo}/revision/{revision}",
                                params={"blobs": "true"}, timeout=60)
        response.raise_for_status()
        for row in response.json()["siblings"]:
            name = row["rfilename"]
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                raise ValueError("模型来源包含无效文件路径。")
            selected = (name.endswith((".wav", ".ckpt", ".pth", ".md")) if repo == SAMPLE_REPO else
                        name.startswith(("chinese-hubert-base/", "chinese-roberta-wwm-ext-large/")))
            if selected:
                target = (model if repo == SAMPLE_REPO else engine / "GPT_SoVITS/pretrained_models") / name
                items.append((repo, revision, name, target, row.get("lfs", {}).get("sha256")))
    def download(item):
        repo, revision, name, target, expected = item
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://huggingface.co/{repo}/resolve/{revision}/{quote(name)}"
        if not target.exists():
            temp = target.with_name(target.name + ".part")
            with requests.get(url, stream=True, timeout=(30, 180)) as response:
                response.raise_for_status()
                with temp.open("wb") as output:
                    for block in response.iter_content(1024 * 1024):
                        output.write(block)
            with temp.open("rb") as stream:
                if expected and hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError("模型下载校验失败：" + name)
            temp.replace(target)
        with target.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if expected and digest != expected:
            raise ValueError("已有模型与固定来源版本不符：" + str(target))
        progress("已校验：" + name)
        return {"repository": repo, "revision": revision, "file": name, "local_path": str(target),
                "size": target.stat().st_size, "sha256": digest, "url": url}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(download, items))
    model.mkdir(parents=True, exist_ok=True)
    (model / "SOURCE.json").write_text(json.dumps({"license": "CC-BY-NC-SA-4.0", "attribution": ATTRIBUTION,
        "game": "ATRI -My Dear Moments-", "use": "Local noncommercial evaluation; see model README.md.",
        "files": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    return model


def patch_jieba(engine):
    """Windows cp312 has no jieba_fast wheel; preserve an explicit Python fallback."""
    for name in ("chinese.py", "chinese2.py", "tone_sandhi.py"):
        path = engine / "GPT_SoVITS/text" / name
        text = path.read_text(encoding="utf-8")
        if "except ImportError:" in text and ("import jieba as jieba_fast" in text or name == "tone_sandhi.py" and "    import jieba" in text):
            continue
        for source, replacement in (
            ("import jieba_fast as jieba", "try:\n    import jieba_fast as jieba\nexcept ImportError:\n    import jieba"),
            ("import jieba_fast", "try:\n    import jieba_fast\nexcept ImportError:\n    import jieba as jieba_fast"),
            ("import jieba_fast.posseg as psg", "try:\n    import jieba_fast.posseg as psg\nexcept ImportError:\n    import jieba.posseg as psg")):
            text = re.sub(r"(?m)^" + re.escape(source) + r"$", lambda _: replacement, text)
        path.write_text(text, encoding="utf-8")


def write_configuration(home, engine, model, *, device):
    base = engine / "GPT_SoVITS/pretrained_models"
    custom = {"version": "v1", "device": device, "is_half": device == "cuda",
              "bert_base_path": str(base / "chinese-roberta-wwm-ext-large"),
              "cnhuhbert_base_path": str(base / "chinese-hubert-base"),
              "t2s_weights_path": str(model / "atri-e10.ckpt"), "vits_weights_path": str(model / "atri_e25_s5250.pth")}
    settings = home / "tools/voice-engine/atri-infer.yaml"
    settings.write_text(yaml.safe_dump({"custom": custom}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    catalog_path = home / "data/voice/catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else {"version": 1, "characters": {}}
    previous = catalog["characters"].get("atri", {})
    references = {}
    for label, text, description in (
        ("neutral", "ここはですね、こうやって解くんです。いいですか", "讲解切片（手动绑定，待试听校准）"),
        ("sad", "悲しまないでください。わたしも、悲しくなってしまいます", "难过台词（手动绑定，待试听校准）"),
        ("firm", "どうしてもです。夏生さんはここで待っててください", "坚定台词（手动绑定，待试听校准）")):
        audio = next(iter(model.rglob(text + "*.wav")), None)
        if audio is None:
            raise ValueError("样板缺少参考切片：" + text)
        references[label] = {"reference_audio": str(audio), "prompt_text": text, "prompt_language": "ja", "description": description}
    # Keep manually edited labels while relocating sample paths that belonged
    # to the old home; setup never replaces another character's catalog entry.
    for label, reference in previous.get("emotions", {}).items():
        reference = dict(reference)
        filename = Path(reference.get("reference_audio", "")).name
        relocated = next(iter(model.rglob(filename)), None) if filename else None
        if relocated:
            reference["reference_audio"] = str(relocated)
        references[label] = reference
    catalog["characters"]["atri"] = {**previous, "name": previous.get("name", "ATRI"),
        "gpt_weights": custom["t2s_weights_path"], "sovits_weights": custom["vits_weights_path"],
        "source": "https://huggingface.co/" + SAMPLE_REPO, "license": "CC-BY-NC-SA-4.0",
        "attribution": ATTRIBUTION, "emotions": references}
    temp = catalog_path.with_suffix(".tmp")
    temp.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(catalog_path)
    return settings, catalog_path


def prepare_language_assets(interpreter, engine):
    cache = engine / "GPT_SoVITS/pretrained_models/fast_langdetect"
    cache.mkdir(parents=True, exist_ok=True)
    nltk_data = engine.parent / "nltk_data"
    nltk_data.mkdir(parents=True, exist_ok=True)
    # Fetch only the fixed public language resources. Checking their published
    # hashes avoids unpinned downloads during an English synthesis request.
    base = f"https://raw.githubusercontent.com/nltk/nltk_data/{NLTK_REVISION}"
    response = requests.get(base + "/index.xml", timeout=60)
    response.raise_for_status()
    index = {row.attrib["id"]: row.attrib for row in ET.fromstring(response.content).iter("package")}
    records = []
    for name in ("averaged_perceptron_tagger", "averaged_perceptron_tagger_eng", "cmudict"):
        row = index[name]
        relative = f"{row['subdir']}/{name}.zip"
        target = nltk_data / relative
        expected = row["sha256_checksum"]
        data = target.read_bytes() if target.is_file() else b""
        if hashlib.sha256(data).hexdigest() != expected:
            response = requests.get(base + "/packages/" + relative, timeout=90)
            response.raise_for_status()
            data = response.content
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("NLTK 数据校验失败：" + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                relative_member = PurePosixPath(member.filename)
                destination = target.parent / relative_member
                if relative_member.is_absolute() or ".." in relative_member.parts or not destination.resolve().is_relative_to(nltk_data.resolve()):
                    raise ValueError("NLTK 资源包含无效文件路径。")
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(member))
        records.append({"file": relative, "sha256": expected, "revision": NLTK_REVISION})
        progress("已校验 NLTK：" + name)
    (nltk_data / "SOURCE.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    script = ("import sys; from fast_langdetect.infer import LangDetector,LangDetectConfig; "
              "print(LangDetector(LangDetectConfig(cache_dir=sys.argv[1])).detect('你好 Hello',model='full'))")
    run([interpreter, "-X", "utf8", "-c", script, cache], cwd=engine)


def setup_voice(home, *, python=None, device="cuda", reuse=None, index_url="", skip_install=False):
    from runtime_paths import resource_path
    home = Path(home).resolve()
    directory = home / "tools/voice-engine"
    engine, environment = directory / "GPT-SoVITS-Inference", directory / ".venv"
    directory.mkdir(parents=True, exist_ok=True)
    if not engine.exists():
        run(["git", "clone", "--no-checkout", ENGINE_URL, engine], cwd=directory)
        run(["git", "-C", engine, "checkout", ENGINE_COMMIT], cwd=directory)
    commit = subprocess.check_output(["git", "-C", str(engine), "rev-parse", "HEAD"], text=True,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).strip()
    if commit != ENGINE_COMMIT:
        raise ValueError("已有引擎不是固定版本；请指定独立数据目录，避免覆盖现有代码。")
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not skip_install:
        if not interpreter.is_file():
            if python is None and getattr(sys, "frozen", False):
                raise ValueError("创建独立推理环境需要 --python 指向 Python 3.10–3.12。")
            parent = Path(python or sys.executable).resolve()
            run([parent, "-m", "venv", environment], cwd=directory)
        if reuse:
            reuse = Path(reuse).resolve()
            if not (reuse / "torch/__init__.py").is_file():
                raise ValueError("复用目录中没有 torch。")
            library = environment / "Lib/site-packages" if os.name == "nt" else next((environment / "lib").glob("python*/site-packages"))
            (library / "qqbot_torch.pth").write_text(reuse.as_posix() + "\n", encoding="utf-8")
        else:
            run([interpreter, "-m", "pip", "install", "torch==2.8.0", "torchaudio==2.8.0", "--index-url",
                 index_url or ("https://download.pytorch.org/whl/cu128" if device == "cuda" else "https://download.pytorch.org/whl/cpu")], cwd=directory)
        requirements = resource_path("voice/requirements-inference.txt" if getattr(sys, "frozen", False) else "tools/voice-engine/requirements-inference.txt")
        run([interpreter, "-m", "pip", "install", "-r", requirements], cwd=directory)
    if not interpreter.is_file():
        raise ValueError("独立推理 Python 不存在；先运行 setup 安装环境。")
    run([interpreter, "-X", "utf8", "-c", "import torch,torchaudio,fastapi,pyopenjtalk; assert torch.__version__.split('+')[0] == torchaudio.__version__.split('+')[0]; print('Voice dependencies ready:',torch.__version__)"], cwd=directory)
    patch_jieba(engine)
    model = download_sample(home, engine)
    prepare_language_assets(interpreter, engine)
    settings, catalog = write_configuration(home, engine, model, device=device)
    result = {"engine_commit": commit, "python": str(interpreter), "engine_directory": str(engine),
              "engine_config": str(settings), "catalog": str(catalog), "device": device,
              "source": "https://huggingface.co/" + SAMPLE_REPO, "license": "CC-BY-NC-SA-4.0",
              "attribution": ATTRIBUTION, "config_changed": False}
    (home / "data/voice/setup.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
