"""External training/ASR process. Never import this module into the frozen GUI."""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import hashlib
import importlib.metadata
import json
import os
import re
from pathlib import Path
import subprocess
import time

# Gradio creates an httpx client during import; bare ::1 in inherited NO_PROXY
# is rejected by newer httpx. This worker's local services never need a proxy.
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1"
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"


def emit(stage, **values):
    print("[VOICE_TRAINING] " + json.dumps({"stage": stage, **values}, ensure_ascii=False), flush=True)


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def assets(job):
    from voice_training_download import download_model
    engine = Path(job["engine_directory"])
    target = engine / "GPT_SoVITS/pretrained_models"
    version = job.get("version", "v2ProPlus")
    names = (["v2Pro/s2Gv2ProPlus.pth", "v2Pro/s2Dv2ProPlus.pth",
              "sv/pretrained_eres2netv2w24s4ep4.ckpt", "s1v3.ckpt"] if version == "v2ProPlus" else
             ["gsv-v4-pretrained/s2Gv4.pth", "s1v3.ckpt"])
    result = download_model("lj1995/GPT-SoVITS", target, names, emit=emit)
    result["version"] = version
    save(target / ("qqbot-training-assets-" + version + ".json"), result)
    return result


def asr(job):
    from faster_whisper import WhisperModel
    import numpy as np
    import soundfile as sf
    target = Path(job["output"])
    target.mkdir(parents=True, exist_ok=True)
    emit("loading-asr", model=job["model"], device=job.get("device", "cpu"))
    model_path = Path(job["model"])
    if not model_path.is_dir():
        from voice_training_download import download_model
        model_path = Path(job["download_root"]) / job["model"].replace("/", "--")
        provenance = download_model(job["model"], model_path,
            ["config.json", "preprocessor_config.json", "tokenizer.json", "vocabulary.json", "vocabulary.txt", "model.bin"], emit=emit)
        save(model_path / "qqbot-provenance.json", provenance)
    model = WhisperModel(str(model_path), device=job.get("device", "cpu"),
        compute_type=job.get("compute_type", "int8"), cpu_threads=4,
        download_root=job.get("download_root"))
    rows = []
    for index, source in enumerate(job["sources"]):
        path = Path(source["audio"])
        original = digest(path)
        emit("transcribing", index=index, total=len(job["sources"]), file=path.name)
        flags = {"creationflags":0x08000000} if os.name == "nt" else {}
        decoded = subprocess.run([job.get("ffmpeg", "ffmpeg"), "-nostdin", "-v", "error",
            "-protocol_whitelist", "file,pipe", "-i", str(path), "-vn", "-ar", "32000", "-ac", "1", "-f", "f32le", "pipe:1"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, **flags)
        sr = 32000
        audio = np.frombuffer(decoded.stdout,dtype="<f4")
        segments, info = model.transcribe(np.ascontiguousarray(audio[::2]), language=job.get("language", "ja"),
            beam_size=5, vad_filter=True, condition_on_previous_text=False,
            word_timestamps=True, vad_parameters={"min_silence_duration_ms": 400})
        for segment in segments:
            # Keep complete ASR phrases. Long phrases are split on timestamped words.
            words = list(segment.words or [])
            groups = []
            if segment.end - segment.start <= 15 or not words:
                groups.append((segment.start, segment.end, segment.text))
            else:
                start = words[0].start
                group = []
                for word in words:
                    if group and word.end - start > 12:
                        groups.append((start, group[-1].end, "".join(w.word for w in group)))
                        start, group = word.start, []
                    group.append(word)
                if group:
                    groups.append((start, group[-1].end, "".join(w.word for w in group)))
            for start, end, text in groups:
                start, end = max(0, start - .10), min(len(audio) / sr, end + .12)
                text = text.strip()
                if not text or end - start < 1 or segment.no_speech_prob > .6:
                    continue
                name = f"{original[:16]}-{int(start*1000):08d}.wav"
                clip = target / name
                sf.write(clip, audio[int(start*sr):int(end*sr)], sr, subtype="PCM_16")
                confidence = float(np.exp(min(0, segment.avg_logprob)))
                # Suggestions stay explicit and reviewable; silence/uncertainty isn't a real emotion.
                emotion = "neutral"
                for tag, cues in [("happy", ("ありがとう", "嬉しい", "うれしい", "楽しい", "大好き")),
                                  ("sad", ("寂しい", "悲しい", "さみしい", "ごめん")),
                                  ("surprised", ("びっくり", "なんで", "どうして")),
                                  ("firm", ("ダメ", "だめ", "やめて", "うるさい"))]:
                    if any(cue in text for cue in cues):
                        emotion = tag
                        break
                rows.append({"audio": str(clip), "text": text, "speaker": job["speaker"],
                    "language": job.get("language", "ja"), "emotion": emotion,
                    "source": source.get("source", str(path)), "source_group": original,
                    "source_sha256": original, "start": start, "end": end,
                    "asr": {"model": job["model"], "confidence": confidence,
                            "avg_logprob": segment.avg_logprob, "no_speech_prob": segment.no_speech_prob},
                    "annotation": {"method": "text-cue-suggestion", "confidence": .35,
                                   "needs_review": True, "reason": "自动转写与台词情绪建议，尚未人工听辨"},
                    "reviewed": False})
    output = target / "annotations.jsonl"
    output.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return {"path": str(output), "count": len(rows), "rows": rows}


def train(job):
    import yaml
    engine = Path(job["engine_directory"]).resolve()
    folder = Path(job["run_directory"]).resolve()
    dataset = Path(job["dataset_directory"]).resolve()
    manifest = json.loads(Path(job.get("dataset_manifest", dataset / "manifest.json")).read_text(encoding="utf-8"))
    selected = set(job["train_ids"])
    rows = [r for r in manifest["rows"] if r["id"] in selected]
    for row in rows:
        if digest(dataset / row["audio"]) != row["sha256"]:
            raise ValueError("训练音频已修改：" + row["id"])
    options = job["options"]
    version = options["version"]
    pretrained = engine / "GPT_SoVITS/pretrained_models"
    sovits = pretrained / ("v2Pro/s2Gv2ProPlus.pth" if version == "v2ProPlus" else "gsv-v4-pretrained/s2Gv4.pth")
    gpt = pretrained / "s1v3.ckpt"
    for path in (sovits, gpt, pretrained / "chinese-hubert-base", pretrained / "chinese-roberta-wwm-ext-large"):
        if not path.exists():
            raise FileNotFoundError(f"训练预训练资产缺失：{path}；请先安装训练资产")
    prepared = folder / "prepared"
    prepared.mkdir(parents=True, exist_ok=True)
    weights = folder / "weights"
    weights.mkdir(exist_ok=True)
    data_list = folder / "train.list"
    data_list.write_text("\n".join("|".join((str(dataset / r["audio"]), r["speaker"], r["language"].upper(), r["text"]))
        for r in rows) + "\n", encoding="utf-8")
    environment = {**os.environ, "version": version, "is_half": "True", "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "TOKENIZERS_PARALLELISM": "false", "USE_LIBUV": "0",
        "_CUDA_VISIBLE_DEVICES": "0", "CUDA_VISIBLE_DEVICES": "0", "hz": "25hz",
        "PYTHONPATH": os.pathsep.join([str(engine / "GPT_SoVITS"), str(engine)]),
        "inp_text": str(data_list), "inp_wav_dir": "", "exp_name": job["id"],
        "i_part": "0", "all_parts": "1", "opt_dir": str(prepared),
        "bert_pretrained_dir": str(pretrained / "chinese-roberta-wwm-ext-large"),
        "cnhubert_base_dir": str(pretrained / "chinese-hubert-base"),
        "pretrained_s2G": str(sovits), "s2config_path": str(engine / (
            "GPT_SoVITS/configs/s2v2ProPlus.json" if version == "v2ProPlus" else "GPT_SoVITS/configs/s2.json")),
        "sv_path": str(pretrained / "sv/pretrained_eres2netv2w24s4ep4.ckpt"),
        "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD": "1"}
    flags = {"creationflags": 0x08000000} if os.name == "nt" else {}
    def launch(stage, script, *args):
        emit(stage, run_directory=str(folder))
        command=[sys.executable, "-X", "utf8", "-u", str(script), *map(str,args)]
        process=subprocess.Popen(command,cwd=engine,env=environment,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,**flags)
        try:
            for line in iter(process.stdout.readline,b""):
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
            code=process.wait()
        finally:
            process.stdout.close()
        if code:
            raise subprocess.CalledProcessError(code,command)
    expected = {Path(r["audio"]).name for r in rows}
    text_file = prepared / "2-name2text-0.txt"
    if not text_file.exists():
        launch("phonemes", engine / "GPT_SoVITS/prepare_datasets/1-get-text.py")
    text = text_file.read_text(encoding="utf-8")
    actual = {line.split("\t")[0] for line in text.splitlines() if len(line.split("\t")) == 4}
    if expected != actual:
        raise ValueError("音素预处理漏掉样本：" + str(sorted(expected - actual)))
    (prepared / "2-name2text.txt").write_text(text, encoding="utf-8")
    launch("hubert", engine / "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py")
    if version == "v2ProPlus":
        launch("speaker-embedding", engine / "GPT_SoVITS/prepare_datasets/2-get-sv.py")
    launch("semantic", engine / "GPT_SoVITS/prepare_datasets/3-get-semantic.py")
    semantic = prepared / "6-name2semantic-0.tsv"
    semantic_text = semantic.read_text(encoding="utf-8")
    if {l.split("\t")[0] for l in semantic_text.splitlines() if "\t" in l} != expected:
        raise ValueError("语义预处理不完整；检查日志，修正后可继续训练。")
    (prepared / "6-name2semantic.tsv").write_text("item_name\tsemantic_audio\n" + semantic_text + "\n", encoding="utf-8")
    for row in rows:
        name = Path(row["audio"]).name
        required = [prepared / "4-cnhubert" / (name + ".pt"), prepared / "5-wav32k" / name]
        if version == "v2ProPlus":
            required.append(prepared / "7-sv_cn" / (name + ".pt"))
        if not all(p.is_file() for p in required):
            raise ValueError("声学预处理不完整：" + name)
    config_file = engine / ("GPT_SoVITS/configs/s2v2ProPlus.json" if version == "v2ProPlus" else "GPT_SoVITS/configs/s2.json")
    s2 = json.loads(config_file.read_text(encoding="utf-8"))
    s2["train"].update(batch_size=options["batch_size"], epochs=options["sovits_epochs"],seed=options["seed"],
        pretrained_s2G=str(sovits), pretrained_s2D=str(pretrained / "v2Pro/s2Dv2ProPlus.pth") if version == "v2ProPlus" else "",
        if_save_latest=True, if_save_every_weights=True, save_every_epoch=min(options["save_every"], options["sovits_epochs"]),
        gpu_numbers="0", grad_ckpt=True, lora_rank=options["lora_rank"], text_low_lr_rate=.4, log_interval=10)
    s2["model"]["version"] = version
    s2["data"]["exp_dir"] = str(prepared)
    # The trainer saves/resumes under exp_dir/logs_s2_VERSION, independently
    # of its TensorBoard root. Create that same checkpoint root before launch.
    checkpoint_dir = prepared / ("logs_s2_" + version + ("_lora_" + str(options["lora_rank"]) if version == "v4" else ""))
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    s2["s2_ckpt_dir"] = str(checkpoint_dir)
    s2.update(save_weight_dir=str(weights), name=job["id"], version=version)
    save(folder / "s2.json", s2)
    script = engine / ("GPT_SoVITS/s2_train.py" if version == "v2ProPlus" else "GPT_SoVITS/s2_train_v3_lora.py")
    # Upstream hardcodes five persistent workers. A saved, hashed copy changes
    # only loader concurrency for Windows/8GB hosts; the optimization code is intact.
    source = script.read_text(encoding="utf-8")
    patched = source.replace("num_workers=5", "num_workers=0").replace("num_workers=6", "num_workers=0").replace("persistent_workers=True", "persistent_workers=False").replace("prefetch_factor=4", "prefetch_factor=None")
    patches=["DataLoader workers=0, persistent=False, prefetch=None"]
    if os.name == "nt":
        # This worker masks to GPU 0. Windows CUDA builds can lack a working
        # Gloo transport; one GPU requires no collective communication.
        patched,count=re.subn(r"    dist\.init_process_group\([\s\S]*?\n    \)",
            "    # QQBot single-GPU Windows training: no process group needed.",patched,count=1)
        if count != 1:
            raise ValueError("官方训练源码的分布式初始化格式已变化，请更新适配器。")
        for name in ("net_g","net_d"):
            patched=patched.replace(f"DDP({name}, device_ids=[rank], find_unused_parameters=True)",name)
            patched=patched.replace(f"DDP({name}.cuda(rank), device_ids=[rank], find_unused_parameters=True)",f"{name}.cuda(rank)")
            patched=patched.replace(f"{name}.module.load_state_dict",f"{name}.load_state_dict")
        patches.append("Windows single GPU: omit DDP/process group; preserve the optimizer and training loop")
    runner = folder / "upstream_s2_train.py"
    runner.write_text(patched, encoding="utf-8")
    save(folder / "upstream-provenance.json", {"source": str(script), "sha256": digest(script),
        "runner_sha256": digest(runner), "patches": patches,
        "packages": {k: importlib.metadata.version(k) for k in ("torch", "pytorch-lightning", "transformers", "peft")}})
    launch("sovits-training", runner, "--config", folder / "s2.json")
    s1 = yaml.safe_load((engine / "GPT_SoVITS/configs/s1longer-v2.yaml").read_text(encoding="utf-8"))
    s1["train"].update(batch_size=options["batch_size"], epochs=options["gpt_epochs"],
        save_every_n_epoch=min(options["save_every"], options["gpt_epochs"]), if_save_latest=True, if_save_every_weights=True,
        if_dpo=False, half_weights_save_dir=str(weights), exp_name=job["id"], seed=options["seed"])
    s1["data"]["num_workers"] = 0
    s1.update(pretrained_s1=str(gpt), train_semantic_path=str(prepared / "6-name2semantic.tsv"),
              train_phoneme_path=str(prepared / "2-name2text.txt"), output_dir=str(prepared / "logs_s1"))
    (folder / "s1.yaml").write_text(yaml.safe_dump(s1, allow_unicode=True), encoding="utf-8")
    s1_source=engine / "GPT_SoVITS/s1_train.py"
    original=s1_source.read_text(encoding="utf-8")
    import_line="from AR.data.data_module import Text2SemanticDataModule"
    if import_line not in original:
        raise ValueError("官方 GPT DataModule 入口已变化，请更新适配器。")
    adapted=original.replace(import_line,"from upstream_s1_data import Text2SemanticDataModule")
    data_source=engine / "GPT_SoVITS/AR/data/data_module.py"
    original_data=data_source.read_text(encoding="utf-8")
    adapted_data=original_data.replace("persistent_workers=True","persistent_workers=False").replace("prefetch_factor=16","prefetch_factor=None")
    s1_patches=["DataLoader workers=0, persistent=False, prefetch=None"]
    if os.name == "nt":
        strategy='strategy=DDPStrategy(process_group_backend="nccl" if platform.system() != "Windows" else "gloo")\n        if torch.cuda.is_available()\n        else "auto"'
        if strategy not in original:
            raise ValueError("官方 GPT Trainer 的 strategy 配置已变化，请更新适配器。")
        adapted=adapted.replace(strategy,'strategy="auto"')
        sampler="DistributedBucketSampler(self._train_dataset, batch_size=batch_size)"
        if sampler not in original_data:
            raise ValueError("官方 GPT sampler 入口已变化，请更新适配器。")
        adapted_data=adapted_data.replace(sampler,"DistributedBucketSampler(self._train_dataset, batch_size=batch_size, num_replicas=1, rank=0)")
        s1_patches.append("Lightning auto strategy and explicit rank=0/replicas=1 for one Windows GPU")
    s1_script=folder / "upstream_s1_train.py"
    s1_data=folder / "upstream_s1_data.py"
    s1_script.write_text(adapted,encoding="utf-8")
    s1_data.write_text(adapted_data,encoding="utf-8")
    provenance=json.loads((folder / "upstream-provenance.json").read_text(encoding="utf-8"))
    provenance["s1"]={"source":str(s1_source),"source_sha256":digest(s1_source),
                      "runner_sha256":digest(s1_script),"patches":s1_patches,
                      "data_source":str(data_source),"data_source_sha256":digest(data_source),"data_runner_sha256":digest(s1_data)}
    save(folder / "upstream-provenance.json",provenance)
    launch("gpt-training", s1_script, "--config_file", folder / "s1.yaml")
    sovits_files = sorted(weights.glob("*.pth"), key=lambda p:p.stat().st_mtime_ns)
    gpt_files = sorted(weights.glob("*.ckpt"), key=lambda p:p.stat().st_mtime_ns)
    if not sovits_files or not gpt_files:
        raise ValueError("训练程序退出但没有同时导出 GPT 与 SoVITS 权重，检查日志。")
    result = {"gpt_weights": str(gpt_files[-1]), "sovits_weights": str(sovits_files[-1]), "version": version,
              "weights": {p.name: {"sha256": digest(p), "size": p.stat().st_size} for p in (gpt_files[-1], sovits_files[-1])}}
    save(folder / "result.json", result)
    return result


def main():
    command = sys.argv[1]
    if command == "check":
        import torch
        import faster_whisper
        import tensorboard
        import pytorch_lightning
        import gradio
        return {"cuda": torch.cuda.is_available(), "torch": torch.__version__, "ready": True}
    job = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    return {"assets": assets, "asr": asr, "train": train}[command](job)


if __name__ == "__main__":
    try:
        result = main()
        if len(sys.argv) > 2:
            job = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
            if job.get("result_path"):
                save(job["result_path"], result)
        emit("complete", result=result)
    except BaseException as error:
        import traceback
        traceback.print_exc()
        emit("failed", error=str(error))
        raise SystemExit(1)
