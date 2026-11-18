"""External DSP worker. Executed only by a singing-engine Python environment."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

# A frozen release stores this standalone worker next to dependency data and
# dist-info directories. Those resources are not an import environment: their
# metadata can mask the isolated engine's installed package versions.
_worker_directory = Path(__file__).resolve().parent
sys.path[:] = [entry for entry in sys.path if entry and Path(entry).resolve() != _worker_directory]


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def separate(job):
    import torch
    import soundfile as sf
    import numpy as np
    from demucs.pretrained import get_model
    from demucs.apply import apply_model
    torch.hub.set_dir(str(Path(job["engine_directory"]) / "checkpoints/torch"))
    audio, sr = sf.read(job["source"], dtype="float32", always_2d=True)
    if sr != 44100:
        raise ValueError("分离输入必须为 44.1kHz。")
    model = get_model("htdemucs").to(job["device"]).eval()
    wave = torch.from_numpy(audio.T)
    ref = wave.mean(0)
    mean, std = ref.mean(), ref.std().clamp_min(1e-6)
    wave = (wave - mean) / std
    with torch.inference_mode():
        stems = apply_model(model, wave[None], device=job["device"], shifts=0,
                            split=True, overlap=0.25, progress=True)[0].cpu()
    stems = stems * std + mean
    index = model.sources.index("vocals")
    sf.write(job["vocals"], stems[index].T.numpy(), sr, subtype="PCM_16")
    backing = sum(stems[i] for i in range(len(model.sources)) if i != index)
    sf.write(job["instrumental"], np.clip(backing.T.numpy(), -1, 1), sr, subtype="PCM_16")
    return {"vocals": job["vocals"], "instrumental": job["instrumental"], "model": "htdemucs"}


def convert(job):
    from types import SimpleNamespace
    import yaml
    import torch
    engine = Path(job["engine_directory"])
    checkpoints = engine / "checkpoints"
    sys.path.insert(0, str(engine))
    import hf_utils
    local = {"Plachta/Seed-VC": checkpoints / "seed", "lj1995/VoiceConversionWebUI": checkpoints / "rmvpe",
             "funasr/campplus": checkpoints / "campplus"}
    def resolve(repo_id, model_filename, config_filename=None):
        root = local[repo_id]
        weight = root / model_filename
        if not weight.is_file():
            raise FileNotFoundError(weight)
        return (str(weight), str(root / config_filename)) if config_filename else str(weight)
    hf_utils.load_custom_model_from_hf = resolve
    import inference
    inference.device = torch.device(job["device"])
    original = checkpoints / "seed/config_dit_mel_seed_uvit_whisper_base_f0_44k.yml"
    config = yaml.safe_load(original.read_text(encoding="utf-8"))
    config["model_params"]["vocoder"]["name"] = str(checkpoints / "bigvgan")
    config["model_params"]["speech_tokenizer"]["name"] = str(checkpoints / "whisper")
    settings = Path(job["output_directory"]) / "seed-local.yml"
    settings.write_text(yaml.safe_dump(config), encoding="utf-8")
    args = SimpleNamespace(source=job["source"], target=job["reference"], output=job["output_directory"],
        diffusion_steps=job["steps"], length_adjust=1.0, inference_cfg_rate=0.7, f0_condition=True,
        auto_f0_adjust=False, semi_tone_shift=job["pitch_shift"], fp16=job["device"] == "cuda",
        checkpoint=str(checkpoints / "seed/DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth"),
        config=str(settings))
    inference.main(args)
    files = list(Path(args.output).glob("vc_*.wav"))
    if len(files) != 1:
        raise ValueError("Seed-VC 未生成唯一有效音频。")
    return {"path": str(files[0]), "engine": "seed-vc-f0", "steps": args.diffusion_steps}


def generate(job):
    engine = Path(job["generation_directory"])
    os.environ["ACESTEP_CHECKPOINTS_DIR"] = str(engine / "checkpoints")
    os.environ["ACESTEP_INIT_LLM"] = "false"
    sys.path.insert(0, str(engine))
    import acestep.model_downloader as downloader
    # Headless DiT-only lego needs base + VAE + text encoder, not turbo or the optional LM.
    downloader.MAIN_MODEL_COMPONENTS = ["vae", "Qwen3-Embedding-0.6B"]
    def offline_download(*args, **kwargs):
        raise RuntimeError("缺少本地固定模型，请运行 voice.bat sing setup。")
    downloader._smart_download = offline_download
    from acestep.handler import AceStepHandler
    from acestep.inference import GenerationParams, GenerationConfig, generate_music
    handler = AceStepHandler()
    message, ok = handler.initialize_service(project_root=str(engine), config_path="acestep-v15-base",
        device=job["device"], offload_to_cpu=True, offload_dit_to_cpu=True,
        compile_model=False, use_flash_attention=False, quantization=None)
    if not ok:
        raise RuntimeError(message)
    params = GenerationParams(task_type="lego", src_audio=job["source"],
        instruction="Generate the vocals track based on the audio context:",
        caption=job["style"], lyrics=job["lyrics"], vocal_language=job["language"],
        repainting_start=0.0, repainting_end=-1, thinking=False, use_cot_metas=False,
        use_cot_caption=False, use_cot_language=False, inference_steps=job["steps"], seed=job["seed"])
    config = GenerationConfig(batch_size=1, use_random_seed=False, seeds=[job["seed"]], audio_format="wav")
    def progress(value, desc="", **kwargs):
        save(job["progress_file"], {"fraction": float(value), "message": str(desc)[:500]})
    result = generate_music(handler, None, params, config, save_dir=job["output_directory"], progress=progress)
    if not result.success or not result.audios:
        raise RuntimeError(result.error or "未生成歌声。")
    return {"path": result.audios[0]["path"], "engine": "acestep-v15-base-lego", "seed": job["seed"]}


def check(job):
    import torch
    import soundfile
    if job.get("generation"):
        sys.path.insert(0, job["generation_directory"])
        from acestep.handler import AceStepHandler
        from acestep.inference import GenerationParams
        from transformers import AutoModel
        import accelerate
    else:
        from demucs.pretrained import get_model
        import transformers
    return {"torch": torch.__version__, "cuda": torch.cuda.is_available(),
            "device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["separate", "convert", "generate", "check"])
    parser.add_argument("job", type=Path)
    args = parser.parse_args()
    data = json.loads(args.job.read_text(encoding="utf-8"))
    # Only pinned, hash-checked legacy checkpoints use pickle in these isolated workers.
    os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    result = globals()[args.stage](data)
    save(data["result_file"], result)
