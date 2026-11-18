"""Install training/ASR add-ons in their own environment, reuse inference read-only."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess


def setup_training(home, voice, *, progress=None):
    home = Path(home).resolve()
    python = (home / voice.training.python).resolve()
    inference = (home / voice.engine_python).resolve()
    flags = {"creationflags": 0x08000000} if os.name == "nt" else {}
    if not inference.is_file():
        raise ValueError("请先安装 GPT-SoVITS 推理环境。")
    def command(argv):
        subprocess.run([str(v) for v in argv], check=True, cwd=home, **flags)
    if not python.is_file():
        command([inference, "-m", "venv", python.parent.parent])
    # Resolve the real site directories, including the existing CUDA torch reuse.
    probe = subprocess.check_output([str(inference), "-X", "utf8", "-c",
        "import sys,json; print(json.dumps([p for p in sys.path if p and 'site-packages' in p]))"],
        text=True, encoding="utf-8", **flags)
    sites = json.loads(probe)
    destination = python.parent.parent / "Lib/site-packages/qqbot_inference_readonly.pth"
    destination.write_text("\n".join(Path(p).as_posix() for p in sites) + "\n", encoding="utf-8")
    if progress:
        progress({"stage": "installing", "message": "安装 TensorBoard 与 Faster Whisper；复用现有 CUDA 依赖"})
    command([python, "-m", "pip", "install", "--disable-pip-version-check",
             "tensorboard>=2.18,<3", "faster-whisper==1.2.1", "gradio==4.44.1", "huggingface-hub<1"])
    worker = Path(__file__).with_name("voice_training_worker.py")
    if not worker.is_file():
        from runtime_paths import resource_path
        worker = resource_path("training_runtime/voice_training_worker.py")
    command([python, "-X", "utf8", worker, "check"])
    return {"python": str(python), "ready": True}
