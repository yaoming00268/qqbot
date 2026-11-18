"""Dataset preparation, cancellable official trainers and explicit model publication."""
from __future__ import annotations
import asyncio
from collections import deque
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from voice_dataset import (atomic_json, audio_quality, file_hash, identifier, import_dataset,
                           load_dataset, split_dataset, summarize, update_annotations)


class VoiceTraining:
    def __init__(self, voice, home=None):
        self.home = Path(home or Path.cwd()).resolve()
        self.voice = voice.model_copy(deep=True)
        self.options = voice.training.model_copy(deep=True)
        self.root = self.path(self.options.directory)
        self.python = self.path(self.options.python)
        self.engine = self.path(voice.engine_directory)

    def path(self, value):
        path = Path(value)
        return (path if path.is_absolute() else self.home / path).resolve()

    def status(self):
        datasets, runs = [], []
        for path in sorted((self.root / "datasets").glob("*/manifest.json"), key=lambda p:p.stat().st_mtime, reverse=True)[:100]:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                datasets.append({k:value.get(k) for k in ("id", "name", "state", "speaker", "summary", "error", "created_at")})
            except (OSError, ValueError):
                pass
        for path in sorted((self.root / "runs").glob("*/manifest.json"), key=lambda p:p.stat().st_mtime, reverse=True)[:100]:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                runs.append({k:value.get(k) for k in ("id", "dataset_id", "state", "stage", "error", "created_at", "options", "published_character", "result", "validation")})
            except (OSError, ValueError):
                pass
        from voice_emotions import presets
        catalog_path = self.path(self.voice.catalog_path)
        catalog = json.loads(catalog_path.read_text(encoding="utf-8-sig")) if catalog_path.is_file() else {"characters": {}}
        characters = [{"id": key, "name": value.get("name", key), "training_run": value.get("training_run"),
                       "emotions": list(value.get("emotions", {}))}
                      for key, value in catalog["characters"].items()]
        return {"installed": self.python.is_file(), "python": str(self.python), "directory": str(self.root),
                "presets": presets(), "characters": characters,
                "datasets": datasets, "runs": runs, "recipe": "GPT-SoVITS official v2ProPlus full fine-tune / v4 LoRA",
                "emotion_method": "标签用于参考选择与分组质检；不作为 GPT-SoVITS 的额外情绪监督"}

    def use_reference(self, dataset_id, sample_id, character_id, *, dataset_revision, catalog_revision, replace=False):
        from voice_references import training_reference
        identifier(character_id)
        folder = self.root / "datasets" / identifier(dataset_id)
        if file_hash(folder / "manifest.json") != dataset_revision:
            raise ValueError("数据集已更新，请重新载入后选择参考。")
        data = load_dataset(self.root, dataset_id)
        row = next((r for r in data["rows"] if r["id"] == sample_id), None)
        if row is None:
            raise ValueError("训练切片不存在。")
        catalog_path = self.path(self.voice.catalog_path)
        if file_hash(catalog_path) != catalog_revision:
            raise ValueError("角色目录已更新，请重新载入后选择参考。")
        catalog = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
        character = catalog["characters"].get(character_id)
        if character is None:
            raise ValueError("目标角色不存在，请先注册角色。")
        existing = character.setdefault("emotions", {}).get(row["emotion"])
        if existing and not replace:
            raise ValueError("该情绪已有参考，请确认替换绑定；原始音频会保留。")
        reference = training_reference(folder / row["audio"], row,
            self.path("data/voice/references/training"), dataset_id)
        character["emotions"][row["emotion"]] = reference
        # Re-check after audio IO, before committing a catalog edited by another window.
        if file_hash(catalog_path) != catalog_revision or file_hash(folder / "manifest.json") != dataset_revision:
            raise ValueError("目录或标注在处理期间已变化，请重新载入。")
        atomic_json(catalog_path, catalog)
        return {"character": character_id, "emotion": row["emotion"], "reference": reference,
                "revision": file_hash(catalog_path)}

    @staticmethod
    def check_stop(stop):
        if stop and stop.is_set():
            from emotion_training import TrainingCancelled
            raise TrainingCancelled()

    def worker(self, operation, job, *, folder=None, stop=None, progress=None):
        from runtime_paths import resource_path
        if not self.python.is_file():
            raise ValueError("训练环境尚未安装，请先点击“安装训练环境”。")
        folder = Path(folder or self.root / "jobs" / uuid.uuid4().hex).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        result_path = folder / (operation + "-result.json")
        result_path.unlink(missing_ok=True)
        job = {**job, "result_path": str(result_path), "engine_directory": str(self.engine)}
        request = folder / (operation + "-job.json")
        atomic_json(request, job)
        script = Path(__file__).with_name("voice_training_worker.py")
        if not script.is_file():
            script = resource_path("training_runtime/voice_training_worker.py")
        flags = {"creationflags": 0x08000000} if os.name == "nt" else {}
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_DISABLE_XET": "1"}
        log = folder / (operation + ".log")
        process = subprocess.Popen([str(self.python), "-X", "utf8", "-u", str(script), operation, str(request)],
            cwd=self.home, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **flags)
        # Only this newly created process and its descendants may be stopped.
        tail = deque(maxlen=20)
        def consume():
            with log.open("a", encoding="utf-8") as output:
                for raw in iter(process.stdout.readline, b""):
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    output.write(line + "\n")
                    output.flush()
                    tail.append(line)
                    if progress:
                        try:
                            event = json.loads(line[len("[VOICE_TRAINING] "):]) if line.startswith("[VOICE_TRAINING] ") else {
                                "stage": "training-log", "message": line[-500:]}
                            if event.get("stage") != "complete":
                                progress(event)
                        except (ValueError, RuntimeError):
                            pass
        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        started = time.monotonic()
        try:
            while process.poll() is None:
                self.check_stop(stop)
                if time.monotonic() - started > self.options.timeout_seconds:
                    raise TimeoutError("训练任务超过时限；已保留检查点和日志，可继续训练。")
                time.sleep(.15)
        except BaseException:
            import psutil
            try:
                parent = psutil.Process(process.pid)
                children = parent.children(recursive=True)
                for child in children:
                    try:
                        child.terminate()
                    except psutil.Error:
                        pass
                parent.terminate()
                _, alive = psutil.wait_procs([*children, parent], timeout=3)
                for child in alive:
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
            except psutil.Error:
                pass
            process.wait(timeout=10)
            raise
        finally:
            reader.join(timeout=10)
            process.stdout.close()
        if process.returncode or not result_path.is_file():
            raise ValueError(f"{operation} 未完成，日志：{log}\n" + "\n".join(tail)[-1800:])
        return json.loads(result_path.read_text(encoding="utf-8"))

    def acquire_chieri(self, *, stop=None, progress=None):
        """Ordinary, openly accessible promotional samples only; no paid archives."""
        import hashlib
        import re
        import urllib.request
        from urllib.parse import urljoin
        folder = self.root / "sources/chieri-official"
        folder.mkdir(parents=True, exist_ok=True)
        pages = ["https://www.cabbage-soft.com/products/amachoco/",
                 "https://www.cabbage-soft.com/products/amachoco2/character/",
                 "https://www.cabbage-soft.com/products/amachoco3/character/"]
        sources = []
        for page_index, url in enumerate(pages):
            self.check_stop(stop)
            with urllib.request.urlopen(url, timeout=30) as response:
                content = response.read(2*1024*1024)
            (folder / (f"page-{page_index}.html")).write_bytes(content)
            html = content.decode("utf-8", errors="replace")
            # Exact ordinary sample names exclude the separate h* sample category.
            links = list(dict.fromkeys(re.findall(r'(?:\.\./)?assets/voice/(?:voice|sample)_chieri[_-][0-9]+\.(?:m4a|mp3|ogg|wav)', html)))
            for link in links:
                self.check_stop(stop)
                source = urljoin(url, link)
                audio = folder / (f"amachoco{page_index+1}-" + Path(link).name)
                if not audio.is_file():
                    if progress:
                        progress({"stage": "downloading-samples", "url": source})
                    with urllib.request.urlopen(source, timeout=45) as response:
                        data = response.read(32*1024*1024+1)
                    if not 32 < len(data) <= 32*1024*1024 or data[:10].lower().startswith((b"<!doctype", b"<html")):
                        raise ValueError("官方样本返回无效媒体内容：" + source)
                    temporary = audio.with_suffix(audio.suffix + ".tmp")
                    temporary.write_bytes(data)
                    temporary.replace(audio)
                sources.append({"audio": str(audio), "source": source, "page": url,
                                "sha256": file_hash(audio), "downloaded_at": time.time()})
        result = {"character": "雪村千絵莉", "speaker": "chieri", "sources": sources,
                  "attribution": "きゃべつそふと / 雪村千絵莉 / CV 藤咲ウサ",
                  "license": "官方公开试听素材；未发现开放训练或再分发许可，原权利归权利人。",
                  "scope": "本机训练验证；来源记录不代表取得再分发许可"}
        if not sources:
            raise ValueError("官方页面未返回可下载的普通语音样本。")
        atomic_json(folder / "sources.json", result)
        return result

    def prepare_audio(self, sources, *, speaker, language="ja", name="", stop=None, progress=None,
                      source="", license_text="", attribution=""):
        folder = self.root / "jobs" / ("asr-" + uuid.uuid4().hex[:12])
        result = self.worker("asr", {"sources": sources, "speaker": identifier(speaker), "language": language,
            "model": self.options.asr_model, "device": self.options.asr_device,
            "ffmpeg": self.voice.ffmpeg_path,
            "compute_type": self.options.asr_compute_type, "output": str(folder / "slices"),
            "download_root": str(self.home / "tools/voice-engine/asr-models")}, folder=folder, stop=stop, progress=progress)
        return import_dataset(self.root, result["path"], speaker=speaker, name=name or speaker,
            ffmpeg=self.voice.ffmpeg_path, stop=stop, progress=progress, source=source,
            license_text=license_text, attribution=attribution)

    def import_annotations(self, path, **kwargs):
        return import_dataset(self.root, self.path(path), ffmpeg=self.voice.ffmpeg_path, **kwargs)

    def dataset(self, identity):
        data = load_dataset(self.root, identity)
        data["revision"] = file_hash(self.root / "datasets" / identity / "manifest.json")
        return data

    def run(self, dataset_id, *, allow_unreviewed=False, resume="", stop=None, progress=None):
        stop = stop or threading.Event()
        data = load_dataset(self.root, dataset_id, verify=True)
        if any(not r.get("reviewed") for r in data["rows"]) and not allow_unreviewed:
            raise ValueError("数据集有未复核的自动标注。请先复核，或显式勾选“使用未复核数据作实验训练”。")
        self.root.mkdir(parents=True, exist_ok=True)
        if resume:
            folder = self.root / "runs" / identifier(resume)
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            if manifest["dataset_id"] != dataset_id or manifest.get("dataset_sha256") != file_hash(self.root / "datasets" / dataset_id / "manifest.json"):
                raise ValueError("数据集已修改，请新建训练任务；旧检查点继续保留。")
            if manifest.get("state") == "completed":
                raise ValueError("该训练已完成，请验证或发布模型。")
            options = manifest["options"]
        else:
            options = self.options.model_dump()
            identity = "train-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            folder = self.root / "runs" / identity
            folder.mkdir(parents=True, exist_ok=False)
            split = split_dataset(data["rows"], ratio=options["validation_ratio"], seed=options["seed"])
            manifest = {"version": 1, "id": identity, "dataset_id": dataset_id, "created_at": time.time(),
                        "options": options, "split": split, "summary": summarize(data),
                        "dataset_sha256": file_hash(self.root / "datasets" / dataset_id / "manifest.json"),
                        "experimental": summarize(data)["seconds"] < 600 or any(not r.get("reviewed") for r in data["rows"]),
                        "emotion_method": "reference-conditioning; labels never appended to transcripts",
                        "source": data.get("source", ""), "license": data.get("license", ""),
                        "attribution": data.get("attribution", "")}
            atomic_json(folder / "dataset_snapshot.json", data)
        def update(event):
            if event.get("stage") != "training-log":
                manifest.update(stage=event.get("stage", manifest.get("stage")), progress=event)
                atomic_json(folder / "manifest.json", manifest)
            if progress:
                progress({**event, "run_id": manifest["id"]})
        manifest.update(state="running", stage="queued", started_at=time.time(), error="")
        manifest.pop("finished_at", None)
        atomic_json(folder / "manifest.json", manifest)
        async def work():
            from voice_engine import backend_config, backend_endpoint, engine_status, start_engine, stop_engine
            from voice_service import engine_lock
            async with engine_lock("singing:gpu"), engine_lock(backend_endpoint(self.voice)):
                self.check_stop(stop)
                status = engine_status()
                restore = status.get("owned") and status.get("running") and status.get("endpoint") == backend_endpoint(self.voice)
                if restore:
                    await asyncio.to_thread(stop_engine)
                try:
                    from runtime_control import finish_thread
                    return await finish_thread(self.worker, "train", {"id": manifest["id"], "options": options,
                        "run_directory": str(folder), "dataset_directory": str(self.root / "datasets" / dataset_id),
                        "dataset_manifest": str(folder / "dataset_snapshot.json"),
                        "train_ids": manifest["split"]["train"]}, folder=folder, stop=stop, progress=update)
                finally:
                    if restore:
                        try:
                            await asyncio.to_thread(start_engine, backend_config(self.voice))
                        except Exception as error:
                            manifest["tts_restore_error"] = str(error)
        async def cancellable():
            task = asyncio.create_task(work())
            try:
                while not task.done():
                    self.check_stop(stop)
                    await asyncio.wait({task}, timeout=.2)
                return task.result()
            except BaseException:
                stop.set()
                raise
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        try:
            result = asyncio.run(cancellable())
            manifest.update(state="completed", stage="ready", result=result, finished_at=time.time())
            atomic_json(folder / "manifest.json", manifest)
            return manifest
        except BaseException as error:
            manifest.update(state="cancelled" if type(error).__name__ == "TrainingCancelled" else "failed",
                            error=str(error), finished_at=time.time())
            atomic_json(folder / "manifest.json", manifest)
            raise

    def character(self, run_id):
        folder = self.root / "runs" / identifier(run_id)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("state") != "completed":
            raise ValueError("只能验证或发布已导出两份权重的训练。")
        data = json.loads((folder / "dataset_snapshot.json").read_text(encoding="utf-8"))
        selected = set(manifest["split"]["train"])
        candidates = [r for r in data["rows"] if r["id"] in selected and 1 <= r["duration"] <= 10]
        emotions = {}
        for row in sorted(candidates, key=lambda r:(not r.get("reviewed"), r["clipped_ratio"], abs(r["duration"]-5))):
            if row["emotion"] not in emotions:
                from voice_references import training_reference
                emotions[row["emotion"]] = training_reference(
                    self.root / "datasets" / manifest["dataset_id"] / row["audio"], row,
                    self.path("data/voice/references/training"), manifest["dataset_id"])
        if not emotions:
            raise ValueError("训练集没有 1–10 秒的有效参考切片，请补充或重新切分。")
        result = manifest["result"]
        for key in ("gpt_weights", "sovits_weights"):
            path = Path(result[key]).resolve()
            if not path.is_relative_to(folder.resolve()) or not path.is_file() or path.stat().st_size < 1024*1024:
                raise ValueError("角色权重缺失或不属于该训练任务。")
            if file_hash(path) != result["weights"][path.name]["sha256"]:
                raise ValueError("角色权重校验失败。")
        character = {"name": data["name"], "gpt_weights": result["gpt_weights"], "sovits_weights": result["sovits_weights"],
            "emotions": emotions, "source": manifest.get("source", ""), "license": manifest.get("license", ""),
            "attribution": manifest.get("attribution", ""), "training_run": manifest["id"],
            "training_version": result["version"], "experimental": manifest["experimental"]}
        if manifest["experimental"]:
            character["synthesis"] = {"max_chunk_chars": 32}
        return folder, manifest, data, character

    def validate(self, run_id, *, stop=None, progress=None):
        from voice_service import VoiceService
        folder, manifest, data, character = self.character(run_id)
        catalog = folder / "validation-catalog.json"
        atomic_json(catalog, {"version": 1, "characters": {"training-validation": character}})
        config = self.voice.model_copy(deep=True)
        config.enabled, config.auto_start, config.reuse_audio = True, True, False
        config.character, config.catalog_path = "training-validation", str(catalog)
        config.cache_dir = str(folder / "validation-audio")
        # The local bridge loads its startup catalog. Direct api_v2 validates new
        # weights without publishing an unverified profile to the live bridge.
        from voice_engine import backend_config
        config = backend_config(config)
        config.default_language = "ja"
        held = set(manifest["split"]["validation"])
        rows = [r for r in data["rows"] if r["id"] in held][:3]
        results = []
        try:
            for index, row in enumerate(rows):
                self.check_stop(stop)
                config.default_language = row["language"] if row["language"] in ("ja", "zh", "en") else "mixed"
                emotion = row["emotion"] if row["emotion"] in character["emotions"] else next(iter(character["emotions"]))
                result = asyncio.run(VoiceService(config).synthesize(row["text"], emotion,
                    check_current=lambda:self.check_stop(stop), progress=progress))
                results.append({"sample_id": row["id"], "emotion": emotion,
                    "wav_path": result["wav_path"], "duration": result["duration"],
                    "manifest_path": result["manifest_path"], "quality": audio_quality(result["wav_path"])})
            validation = {"state": "passed", "kind": "held-out-synthesis-smoke", "samples": results,
                          "time": time.time(), "quality_note": "验证合成、转码和无泄漏留出文本；音色相似度与情绪仍需听辨。"}
        except BaseException as error:
            manifest["validation"] = {"state": "failed", "error": str(error), "samples": results, "time": time.time()}
            atomic_json(folder / "manifest.json", manifest)
            raise
        manifest["validation"] = validation
        atomic_json(folder / "manifest.json", manifest)
        return validation

    def publish(self, run_id, character_id, *, expected_revision):
        folder, manifest, data, character = self.character(run_id)
        identifier(character_id)
        if manifest.get("validation", {}).get("state") != "passed":
            raise ValueError("请先执行留出集验证试听，再发布角色模型。")
        catalog_path = self.path(self.voice.catalog_path)
        current_revision = file_hash(catalog_path) if catalog_path.exists() else ""
        if current_revision != expected_revision:
            raise ValueError("角色目录已变化，请重新载入后发布。")
        catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else {"version": 1, "characters": {}}
        if character_id in catalog["characters"]:
            raise ValueError("角色 ID 已存在，请用新的 ID 发布；现有角色不会被覆盖。")
        catalog["characters"][character_id] = character
        atomic_json(catalog_path, catalog)
        manifest["published_character"] = character_id
        atomic_json(folder / "manifest.json", manifest)
        return {"character": character_id, "catalog_path": str(catalog_path), "revision": file_hash(catalog_path),
                "applied": False, "experimental": manifest["experimental"]}

    def log(self, run_id, limit=12000):
        folder = self.root / "runs" / identifier(run_id)
        path = folder / "train.log"
        if not path.is_file():
            return {"text": "", "path": str(path)}
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size-min(max(int(limit),1000),64000)))
            text = stream.read().decode("utf-8", errors="replace")
        return {"text": text, "path": str(path)}
