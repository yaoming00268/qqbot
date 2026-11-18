"""Install pinned singing engines and assets independently of the bot runtime."""
from __future__ import annotations
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import time
from urllib.parse import quote
import requests

SEED_COMMIT = "51383efd921027683c89e5348211d93ff12ac2a8"
ACE_COMMIT = "ca1e85fe9430179831e6bc6be790c332190a3866"
SEED_MODEL = "DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth"
SEED_CONFIG = "config_dit_mel_seed_uvit_whisper_base_f0_44k.yml"
# Each snapshot is explicit. Inference runs offline and never executes HF Python code for Seed-VC.
ASSETS = [
    ("Plachta/Seed-VC", "257283f9f41585055e8f858fba4fd044e5caed6e", "seed-vc/checkpoints/seed", [SEED_MODEL, SEED_CONFIG]),
    ("lj1995/VoiceConversionWebUI", "e6d0c1a17da07c33557852f9dfa2bd44cc75737d", "seed-vc/checkpoints/rmvpe", ["rmvpe.pt"]),
    ("funasr/campplus", "e4b6ede7ce16997aff4ae69fbca1f0175e2afede", "seed-vc/checkpoints/campplus", ["campplus_cn_common.bin"]),
    ("nvidia/bigvgan_v2_44khz_128band_512x", "95a9d1dcb12906c03edd938d77b9333d6ded7dfb", "seed-vc/checkpoints/bigvgan", ["config.json", "bigvgan_generator.pt"]),
    ("openai/whisper-small", "973afd24965f72e36ca33b3055d56a652f456b4d", "seed-vc/checkpoints/whisper", ["config.json", "model.safetensors", "preprocessor_config.json"]),
    ("ACE-Step/Ace-Step1.5", "19671f406d603126926c1b7e2adc169acbcade22", "ace-step/checkpoints", ["vae/", "Qwen3-Embedding-0.6B/"]),
    ("ACE-Step/acestep-v15-base", "e432212fec32b8965a14ffa57ae653438d6abd14", "ace-step/checkpoints/acestep-v15-base", ["config.json", "model.safetensors", "silence_latent.pt"]),
]


def run(argv, cwd):
    subprocess.run([str(x) for x in argv], cwd=cwd, check=True, stdout=sys.stderr,
                   stderr=sys.stderr, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def download_ranges(url, target, size):
    """Resume validated 32MB ranges; merge only after every HTTP range is complete."""
    chunks = target.with_name(target.name + ".chunks")
    chunks.mkdir(exist_ok=True)
    block = 32 * 1024 * 1024
    ranges = [(start, min(size - 1, start + block - 1)) for start in range(0, size, block)]
    def fetch(bounds):
        session = requests.Session()
        session.trust_env = os.environ.get("QQBOT_SINGING_DOWNLOAD_DIRECT") != "1"
        mirror = os.environ.get("QQBOT_SINGING_MODEL_MIRROR", "").rstrip("/")
        download_url = url.replace("https://huggingface.co", mirror, 1) if mirror else url
        start, end = bounds
        path = chunks / str(start)
        if path.is_file() and path.stat().st_size == end - start + 1:
            return path
        part = path.with_suffix(".part")
        for attempt in range(10):
            try:
                have = part.stat().st_size if part.is_file() else 0
                if have == end - start + 1:
                    part.replace(path)
                    return path
                if have > end - start + 1:
                    part.unlink()
                    have = 0
                offset = start + have
                # A unique range query also avoids CDN caches that ignore Range in their key.
                with session.get(download_url, params={"qqbot_range": f"{offset}-{end}"},
                        headers={"Range": f"bytes={offset}-{end}"}, stream=True, timeout=(20, 25)) as r:
                    r.raise_for_status()
                    if r.status_code != 206 or r.headers.get("Content-Range") != f"bytes {offset}-{end}/{size}":
                        raise ValueError("模型服务器未返回请求的数据范围。")
                    total = have
                    with part.open("ab") as output:
                        for data in r.iter_content(1024 * 1024):
                            total += len(data)
                            if total > end - start + 1:
                                raise ValueError("模型数据范围超出预期。")
                            output.write(data)
                    if total != end - start + 1:
                        raise ValueError("模型数据范围不完整。")
                part.replace(path)
                return path
            except (requests.RequestException, ValueError) as error:
                print(f"重试数据块 {target.name} {start}: {type(error).__name__} ({attempt + 1}/10)", file=sys.stderr, flush=True)
                if attempt == 9:
                    raise
                time.sleep(1 + attempt)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        files = list(pool.map(fetch, ranges))
    assembled = target.with_name(target.name + ".assembled")
    with assembled.open("wb") as output:
        for part in files:
            with part.open("rb") as source:
                import shutil
                shutil.copyfileobj(source, output, 1024 * 1024)
    return assembled, files


def setup_singing(home, *, python=None, reuse=None, generation=True, skip_install=False):
    home = Path(home).resolve()
    root = home / "tools/singing-engine"
    root.mkdir(parents=True, exist_ok=True)
    resources = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False):
        resources = Path(sys._MEIPASS)
    parent = Path(python or sys.executable)
    if getattr(sys, "frozen", False) and python is None:
        raise ValueError("冻结版安装需要 --python 指向 Python 3.11 或 3.12。")
    for directory, repo, commit in [("seed-vc", "Plachtaa/seed-vc", SEED_COMMIT),
                                    *([("ace-step", "ace-step/ACE-Step-1.5", ACE_COMMIT)] if generation else [])]:
        target = root / directory
        if not target.exists():
            run(["git", "clone", "--no-checkout", "https://github.com/" + repo + ".git", target], root)
            run(["git", "-C", target, "checkout", commit], root)
        actual = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
        if actual != commit:
            raise ValueError(f"{directory} 版本不符，请保留已有修改并手动核对 {commit}。")
    for env, requirements in [(".venv", "seed"), *([(".ace-venv", "generation")] if generation else [])]:
        environment = root / env
        interpreter = environment / "Scripts/python.exe"
        if not interpreter.exists():
            run([parent, "-m", "venv", environment], root)
        if reuse:
            reuse = Path(reuse).resolve()
            if not (reuse / "torch").is_dir():
                raise ValueError("复用路径须为包含 torch 的匹配 Python site-packages。")
            (environment / "Lib/site-packages/qqbot_torch.pth").write_text(reuse.as_posix() + "\n", encoding="utf-8")
        if not skip_install:
            if reuse and not (reuse / "torchaudio").is_dir():
                run([interpreter, "-m", "pip", "install", "--no-deps", "torchaudio==2.8.0+cu128",
                     "--index-url", "https://download.pytorch.org/whl/cu128"], root)
            if not reuse:
                run([interpreter, "-m", "pip", "install", "torch==2.8.0", "torchaudio==2.8.0", "torchvision==0.23.0",
                     "--index-url", "https://download.pytorch.org/whl/cu128"], root)
            run([interpreter, "-m", "pip", "install", "-r", resources / f"singing/requirements-{requirements}.txt"], root)
    items = []
    for repo, revision, directory, names in ASSETS:
        if not generation and directory.startswith("ace-step/"):
            continue
        response = requests.get(f"https://huggingface.co/api/models/{repo}/revision/{revision}",
                                params={"blobs": "true"}, timeout=60)
        response.raise_for_status()
        for row in response.json()["siblings"]:
            name = row["rfilename"]
            if not any(name == n or n.endswith("/") and name.startswith(n) for n in names):
                continue
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                raise ValueError("模型来源包含无效路径。")
            items.append((repo, revision, name, root / directory / name, row.get("lfs", {}).get("sha256"), row.get("size", 0)))

    def download(item):
        repo, revision, name, target, expected, size = item
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://huggingface.co/{repo}/resolve/{revision}/{quote(name)}"
        if target.is_file() and expected:
            with target.open("rb") as f:
                valid = hashlib.file_digest(f, "sha256").hexdigest() == expected
        else:
            valid = False  # Small Git blobs are refreshed from the fixed revision as well.
        if not valid:
            temp = target.with_name(target.name + ".part")
            print("下载 " + repo + "/" + name, file=sys.stderr, flush=True)
            chunks = []
            if size > 64 * 1024 * 1024:
                temp, chunks = download_ranges(url, target, size)
            else:
                with requests.get(url, stream=True, timeout=(30, 180)) as r:
                    r.raise_for_status()
                    with temp.open("wb") as f:
                        for block in r.iter_content(1024 * 1024):
                            f.write(block)
            with temp.open("rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            if expected and digest != expected:
                raise ValueError("模型哈希不符：" + name)
            temp.replace(target)
            for part in chunks:
                part.unlink(missing_ok=True)
            target.with_name(target.name + ".part").unlink(missing_ok=True)
            print("已校验 " + name, file=sys.stderr, flush=True)
        with target.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        return {"repository": repo, "revision": revision, "file": name, "url": url,
                "path": str(target), "size": target.stat().st_size, "sha256": digest}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(download, items))
    hub = root / "seed-vc/checkpoints/torch"
    run([root / ".venv/Scripts/python.exe", "-X", "utf8", "-c",
         "import torch; torch.hub.set_dir(" + repr(str(hub)) + "); from demucs.pretrained import get_model; get_model('htdemucs')"], root)
    for path in (hub / "checkpoints").glob("*.th"):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        records.append({"repository": "facebookresearch/demucs", "version": "4.0.1", "file": path.name,
            "url": "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/" + path.name,
            "path": str(path), "size": path.stat().st_size, "sha256": digest})
    manifest = root / "installation.json"
    result = {"seed_commit": SEED_COMMIT, "ace_commit": ACE_COMMIT if generation else None,
              "files": records, "generation": generation}
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"installation": str(manifest), "assets": len(records), "generation": generation}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path.cwd())
    parser.add_argument("--python", type=Path)
    parser.add_argument("--torch-site-packages", type=Path)
    parser.add_argument("--cover-only", action="store_true")
    parser.add_argument("--skip-install", action="store_true")
    args = parser.parse_args()
    print(json.dumps(setup_singing(args.home, python=args.python, reuse=args.torch_site_packages,
                                  generation=not args.cover_only, skip_install=args.skip_install), ensure_ascii=False))
