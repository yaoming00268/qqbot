"""Install the fixed VoiceStudio source used by the local streaming extension."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import time


REVISION = "4da51ddb4841e04e1e9878f9934fde46c21389f9"
REPOSITORY = "https://github.com/debpalash/VoiceStudio.git"


def setup_studio(config):
    directory = Path(config.voicestudio.source_directory).resolve()
    python = Path(config.engine_python).resolve()
    if not python.is_file():
        raise ValueError("请先安装现有语音推理环境，或填写可用的 engine_python。")
    if not directory.exists():
        subprocess.run(["git", "clone", "--no-checkout", "--filter=blob:none", REPOSITORY, str(directory)], check=True)
        subprocess.run(["git", "-C", str(directory), "checkout", "--detach", REVISION], check=True)
    actual = subprocess.check_output(["git", "-C", str(directory), "rev-parse", "HEAD"], encoding="utf-8").strip()
    if actual != REVISION:
        raise ValueError("VoiceStudio 目录不是已审核固定版本；请选择一个新的安装目录。")
    files = ["backend/services/tts_backend.py", "backend/services/sentence_chunker.py",
             "backend/services/expression.py", "backend/services/instruct_safety.py", "LICENSE", "LICENSE-NOTICE.md"]
    if subprocess.check_output(["git", "-C", str(directory), "diff", "--name-only", "HEAD", "--", *files], encoding="utf-8").strip():
        raise ValueError("VoiceStudio 的已审核组件有本地修改，未覆盖。")
    subprocess.run([str(python), "-m", "pip", "install", "pykakasi==2.3.0", "aiohttp>=3.10,<4", "numpy>=1.26,<3"],
                   check=True, timeout=180)
    probe = subprocess.run([str(python), "-X", "utf8", "-c",
        "import sys;sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[1]+'/backend');from services.tts_backend import GPTSoVITSBackend;from services.sentence_chunker import SentenceChunker;import aiohttp,numpy;assert GPTSoVITSBackend()._check_language('ja')=='ja';print(GPTSoVITSBackend.id,aiohttp.__version__)", str(directory)],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=90)
    rows = [{"path": name, "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest()} for name in files]
    result = {"repository": REPOSITORY, "revision": actual, "python": str(python), "installed_at": time.time(),
              "components": rows, "probe": probe.stdout.strip(), "new_model_required": False,
              "integration": "QQBot VoiceStudio GPT-SoVITS streaming extension", "license": "AGPL-3.0-only"}
    target = directory.parent / "voicestudio-installation.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "installation": str(target), "revision": actual, "new_model_required": False}
