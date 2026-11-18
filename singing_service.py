"""Song + lyrics -> separated/generated singing -> character conversion -> delivery files."""
from __future__ import annotations
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid
import wave

from singing_config import SingingRequest
from voice_service import engine_lock, encode_silk, load_catalog
from audio_processing import denoise_filter


class SingingError(ValueError):
    pass


def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def singing_context(voice):
    config = voice.singing
    seed = Path(config.engine_directory).resolve()
    ace = Path(config.generation_directory).resolve()
    conversion_files = [seed / "inference.py", seed / "checkpoints/seed/DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth",
        seed / "checkpoints/seed/config_dit_mel_seed_uvit_whisper_base_f0_44k.yml", seed / "checkpoints/rmvpe/rmvpe.pt",
        seed / "checkpoints/campplus/campplus_cn_common.bin", seed / "checkpoints/bigvgan/bigvgan_generator.pt",
        seed / "checkpoints/whisper/model.safetensors", Path(config.engine_python)]
    generation_files = [ace / "checkpoints/acestep-v15-base/model.safetensors",
        ace / "checkpoints/vae/diffusion_pytorch_model.safetensors", ace / "checkpoints/Qwen3-Embedding-0.6B/model.safetensors", Path(config.generation_python)]
    installed = all(p.is_file() and p.stat().st_size for p in conversion_files)
    generation = all(p.is_file() and p.stat().st_size for p in generation_files)
    try:
        emotions = list(load_catalog(voice)["characters"][voice.character].get("emotions", {}))
    except (OSError, ValueError, KeyError):
        emotions = []
    return {"enabled": config.enabled, "cover_ready": installed, "compose_ready": installed and generation,
        "character": voice.character, "emotions": emotions, "modes": ["cover", "compose"], "outputs": ["file", "voice", "both"],
        "max_duration_seconds": config.max_duration_seconds,
        "instruction": "sing cover：原唱＋歌词，保留旋律节奏与原唱发音，歌词不用于换词。sing compose：伴奏＋歌词，生成演唱旋律后转为当前角色，不能保证还原原曲唱法。默认歌曲以 MP3 文件交付；voice/both 才转 QQ SILK。后台完成后 deliver 批准发送。"}


def generation_lyrics(text, language, dictionary):
    """Keep originals separately; strip only LRC metadata, never fabricate timestamps."""
    from voice_text import japanese_tokens
    lines = []
    for line in text.splitlines():
        if re.fullmatch(r"\[(?:ar|al|ti|by|offset|length):.*\]", line.strip(), re.I):
            continue
        line = re.sub(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]", "", line).strip()
        if language == "ja" and line and not re.fullmatch(r"\[(?:verse|chorus|bridge|intro|outro|pre-chorus|instrumental|break|solo)[^\]]*\]", line, re.I):
            line = "".join(japanese_tokens(line, dictionary))
        lines.append(line)
    if not any(line and not line.startswith("[") for line in lines):
        raise SingingError("歌词没有可演唱的正文。")
    return "\n".join(lines)


async def terminate(process):
    """Stop only the subprocess tree created for this stage, including Windows venv redirectors."""
    if process.returncode is not None:
        return
    import psutil
    try:
        parent = psutil.Process(process.pid)
        children = [*reversed(parent.children(recursive=True)), parent]
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = await asyncio.to_thread(psutil.wait_procs, children, timeout=3)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
    except psutil.NoSuchProcess:
        pass
    await process.wait()


class SingingService:
    def __init__(self, voice):
        self.voice, self.config = voice, voice.singing

    async def _run(self, argv, directory, label, *, cwd=None, progress=None, check_current=None, capture=False):
        log = directory / (label + ".log")
        with log.open("ab") as output:
            process = await asyncio.create_subprocess_exec(*map(str, argv), cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE if capture else output,
                stderr=output, env={**os.environ, "PYTHONUTF8": "1"},
                **({"creationflags": 0x08000000} if os.name == "nt" else {}))
            waiting = asyncio.create_task(process.communicate())
            previous = ""
            try:
                while not waiting.done():
                    if check_current:
                        check_current()
                    path = directory / (label + "-progress.json")
                    if progress and path.is_file():
                        raw = path.read_text(encoding="utf-8")
                        if raw != previous:
                            previous = raw
                            progress({"stage": label, **json.loads(raw)})
                    await asyncio.wait({waiting}, timeout=0.2)
                stdout, _ = waiting.result()
                if check_current:
                    check_current()
                if process.returncode:
                    detail = log.read_bytes()[-1800:].decode("utf-8", errors="replace")
                    raise SingingError(f"{label} 失败（{process.returncode}）：{detail}")
                return stdout
            finally:
                if not waiting.done():
                    await terminate(process)
                    await asyncio.gather(waiting, return_exceptions=True)

    async def _worker(self, stage, directory, values, *, progress=None, check_current=None):
        generation = stage == "generate" or values.get("generation")
        python = Path(self.config.generation_python if generation else self.config.engine_python).resolve()
        engine = Path(self.config.generation_directory if generation else self.config.engine_directory).resolve()
        root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        worker = root / "singing_worker.py"
        result_file = directory / (stage + ("-generation" if values.get("generation") else "") + "-result.json")
        job = directory / (stage + "-job.json")
        atomic_json(job, {**values, "device": self.config.device, "engine_directory": str(Path(self.config.engine_directory).resolve()),
            "generation_directory": str(Path(self.config.generation_directory).resolve()), "output_directory": str(directory / stage),
            "progress_file": str(directory / (stage + "-progress.json")), "result_file": str(result_file)})
        (directory / stage).mkdir(exist_ok=True)
        await self._run([python, "-X", "utf8", worker, stage, job], directory, stage, cwd=engine,
                        progress=progress, check_current=check_current)
        return json.loads(result_file.read_text(encoding="utf-8"))

    async def check(self):
        result = singing_context(self.voice)
        folder = Path(self.config.cache_dir).resolve() / ("check-" + uuid.uuid4().hex[:12])
        folder.mkdir(parents=True)
        for name, generation in [("conversion", False), ("generation", True)]:
            try:
                async with asyncio.timeout(60):
                    result[name] = await self._worker("check", folder, {"generation": generation})
            except (OSError, ValueError, TimeoutError) as error:
                result[name] = {"error": str(error)}
        result["healthy"] = result["cover_ready"] and result["compose_ready"] and all("error" not in result[k] for k in ("conversion", "generation"))
        return result

    async def _ffmpeg(self, directory, label, args, **kwargs):
        return await self._run([self.voice.ffmpeg_path, "-nostdin", "-hide_banner", "-v", "error", "-y", *args], directory, label, **kwargs)

    async def render(self, request, *, progress=None, check_current=None, allow_disabled=False):
        request = request if isinstance(request, SingingRequest) else SingingRequest.model_validate(request)
        config = self.config
        if not config.enabled and not allow_disabled:
            raise SingingError("唱歌功能尚未开启，请在语音工作室启用。")
        if not request.lyrics.strip() or len(request.lyrics) > config.max_lyrics_chars or "\x00" in request.lyrics:
            raise SingingError("歌词为空、含无效字符或超过设置上限。")
        if request.mode == "compose" and (request.vocals_only or request.accompaniment):
            raise SingingError("compose 的 song 已是伴奏，不可同时指定 vocals_only 或 accompaniment。")
        character = load_catalog(self.voice).get("characters", {}).get(self.voice.character, {})
        reference = character.get("emotions", {}).get(request.emotion, {}).get("reference_audio", "")
        if not reference or not Path(reference).is_file():
            raise SingingError("所选角色情绪缺少本机参考音频，请先配置真实样本。")
        folder = Path(config.cache_dir).resolve() / uuid.uuid4().hex
        folder.mkdir(parents=True)
        manifest_path = folder / "manifest.json"
        manifest = {"version": 1, "id": folder.name, "state": "queued", "created_at": time.time(),
            "request": request.model_dump(), "character": self.voice.character,
            "reference": str(Path(reference).resolve()), "source": character.get("source", ""),
            "license": character.get("license", ""), "attribution": character.get("attribution", ""), "stages": []}
        def update(stage, **extra):
            manifest.update(stage=stage, **extra)
            manifest["stages"].append({"stage": stage, "time": time.time()})
            atomic_json(manifest_path, manifest)
            if progress:
                progress({"stage": stage, "manifest_path": str(manifest_path), **extra})
        update("queued")
        (folder / "lyrics.txt").write_text(request.lyrics, encoding="utf-8")
        async def work():
            async with asyncio.timeout(config.timeout_seconds):
                # One GPU singing task across Studio/CLI/Bot; TTS shares its own model lock.
                from voice_engine import backend_config, backend_endpoint
                async with engine_lock("singing:gpu"), engine_lock(backend_endpoint(self.voice)):
                    from voice_engine import engine_status, stop_engine, start_engine
                    status = engine_status()
                    restore = config.suspend_owned_tts and status.get("owned") and status.get("running") and status.get("endpoint") == backend_endpoint(self.voice) and config.device == "cuda"
                    if restore:
                        await asyncio.to_thread(stop_engine)
                    try:
                        return await self._render(request, folder, manifest, manifest_path, update,
                                                  reference, check_current=check_current, progress=progress)
                    finally:
                        if restore:
                            try:
                                await asyncio.to_thread(start_engine, backend_config(self.voice))
                            except Exception as error:
                                manifest["tts_restore_error"] = str(error)
                                atomic_json(manifest_path, manifest)
        task = asyncio.create_task(work())
        try:
            while not task.done():
                if check_current:
                    check_current()
                await asyncio.wait({task}, timeout=0.2)
            return task.result()
        except BaseException as error:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            cancelled = isinstance(error, asyncio.CancelledError) or type(error).__name__ in ("TrainingCancelled", "CancelledError", "StaleContext")
            manifest.update(state="cancelled" if cancelled else "failed", error=str(error) or type(error).__name__, finished_at=time.time())
            atomic_json(manifest_path, manifest)
            raise

    async def _render(self, request, folder, manifest, manifest_path, update, reference, *, check_current, progress):
        config = self.config
        options = {"check_current": check_current, "progress": progress}
        async def prepare(source, name, *, max_seconds=None):
            source = Path(source).resolve()
            if not source.is_file() or not 0 < source.stat().st_size <= config.max_input_mb * 1024 * 1024:
                raise SingingError("输入文件不存在、为空或超过大小上限。")
            copy = folder / (name + "-input" + source.suffix[:12])
            before = source.stat()
            await asyncio.to_thread(shutil.copyfile, source, copy)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise SingingError("输入文件在读取期间发生变化，请重新提交。")
            wav = folder / (name + ".wav")
            limit = max_seconds or config.max_duration_seconds
            await self._ffmpeg(folder, "decode-" + name, ["-protocol_whitelist", "file,pipe",
                "-format_whitelist", "wav,mp3,flac,mov,ogg,aac,matroska,webm,aiff,asf,ape",
                "-i", copy, "-vn", "-t", limit + 1,
                "-ar", 44100, "-ac", 2, "-c:a", "pcm_s16le", wav], **options)
            with wave.open(str(wav), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
            if duration <= 0 or duration > limit + 0.01:
                raise SingingError(f"{name} 音频超过 {limit} 秒上限或为空。")
            manifest.setdefault("inputs", {})[name] = {"path": str(copy), "sha256": await asyncio.to_thread(file_hash, copy), "duration": duration}
            return wav, duration
        update("preparing", state="running")
        song, duration = await prepare(request.song, "song")
        if request.song_output in ("voice", "both") and duration > config.max_voice_seconds:
            raise SingingError("歌曲超过 QQ 语音时长上限，请选择 file 输出。")
        ref, ref_duration = await prepare(reference, "reference", max_seconds=30)
        if ref_duration < 1:
            raise SingingError("角色参考音频至少需要 1 秒。")
        if config.denoise.enabled and config.denoise.reference:
            cleaned = folder / "reference-clean.wav"
            await self._ffmpeg(folder, "denoise-reference", ["-i", ref, "-af", denoise_filter(config.denoise),
                "-c:a", "pcm_s16le", cleaned], **options)
            ref = cleaned
        if request.mode == "cover":
            if request.vocals_only:
                vocals, backing = song, None
            else:
                update("separating")
                paths = await self._separate_cached(song, folder, manifest, **options)
                vocals, backing = Path(paths["vocals"]), Path(paths["instrumental"])
            if request.accompaniment:
                backing, backing_duration = await prepare(request.accompaniment, "backing")
                if abs(backing_duration - duration) > 1:
                    raise SingingError("独立伴奏与导唱时长相差超过 1 秒，请先对齐。")
        else:
            lyrics = generation_lyrics(request.lyrics, request.language, self.voice.dictionary)
            (folder / "generation-lyrics.txt").write_text(lyrics, encoding="utf-8")
            update("generating")
            generated = await self._worker("generate", folder, {"source": str(song), "lyrics": lyrics,
                "language": request.language, "style": request.style, "steps": config.generation_steps, "seed": request.seed}, **options)
            vocals, backing = Path(generated["path"]), song
            manifest["generation"] = generated
        if config.denoise.enabled:
            update("denoising")
            cleaned = folder / "vocals-clean.wav"
            await self._ffmpeg(folder, "denoise-vocals", ["-i", vocals, "-af", denoise_filter(config.denoise),
                "-c:a", "pcm_s16le", cleaned], **options)
            vocals = cleaned
            manifest["denoise"] = {"filter": denoise_filter(config.denoise), "reference": config.denoise.reference,
                "vocals": str(cleaned), "accompaniment_processed": False}
        update("converting")
        converted = await self._worker("convert", folder, {"source": str(vocals), "reference": str(ref),
            "steps": config.diffusion_steps, "pitch_shift": request.pitch_shift}, **options)
        vocal = folder / "converted-vocals.wav"
        output_denoise = config.denoise.model_copy(update={"strength_db": min(config.denoise.strength_db, 3)})
        await self._ffmpeg(folder, "vocal-pcm", ["-i", converted["path"], "-af", denoise_filter(output_denoise), "-ar", 44100, "-ac", 1,
                                               "-c:a", "pcm_s16le", vocal], **options)
        with wave.open(str(vocal), "rb") as audio:
            converted_duration = audio.getnframes() / audio.getframerate()
            signal = audio.readframes(audio.getnframes())
            if audio.getsampwidth() != 2:
                raise SingingError("转换歌声应为 16bit PCM WAV。")
        from array import array
        samples = array("h", signal)
        if sys.byteorder != "little":
            samples.byteswap()
        rms = math.sqrt(sum(x * x for x in samples) / max(1, len(samples)))
        if abs(converted_duration - duration) > 0.75 or rms < 8:
            raise SingingError("角色歌声时长不匹配或近乎静音，已保留中间音频供检查。")
        manifest["conversion"] = converted
        update("mixing")
        output = folder / "mixed.wav"
        # amix uses fixed gains and a limiter; no automatic ducking or speed/pitch change.
        args = ["-i", vocal]
        if backing:
            args += ["-i", backing]
            pitch = f"rubberband=pitch={2 ** (request.pitch_shift / 12):.9f}," if request.pitch_shift else ""
            filters = f"[0:a]aresample=44100,volume={config.vocal_gain_db}dB,apad[v];[1:a]aresample=44100,{pitch}volume={config.accompaniment_gain_db}dB,apad[b];[v][b]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95:latency=1[out]"
        else:
            filters = f"[0:a]volume={config.vocal_gain_db}dB,apad,alimiter=limit=0.95:latency=1[out]"
        await self._ffmpeg(folder, "mix", [*args, "-filter_complex", filters, "-map", "[out]", "-t", duration,
            "-ar", 44100, "-ac", 2, "-c:a", "pcm_s16le", output], **options)
        mp3 = folder / "song.mp3"
        await self._ffmpeg(folder, "mp3", ["-i", output, "-c:a", "libmp3lame", "-b:a", "192k", mp3], **options)
        result = {"id": folder.name, "state": "ready", "mode": request.mode, "duration": duration,
            "character": self.voice.character, "emotion": request.emotion, "wav_path": str(output),
            "mp3_path": str(mp3), "lyrics_path": str(folder / "lyrics.txt"), "directory": str(folder),
            "manifest_path": str(manifest_path), "song_output": request.song_output, "files": []}
        if request.song_output in ("file", "both"):
            result["files"].append({"kind": "file", "path": str(mp3), "name": "演唱.mp3", "mime": "audio/mpeg"})
        if request.song_output in ("voice", "both"):
            update("encoding-voice")
            pcm, silk = folder / "voice.wav", folder / "song.silk"
            await self._ffmpeg(folder, "pcm", ["-i", output, "-ar", 24000, "-ac", 1, "-c:a", "pcm_s16le", pcm], **options)
            await asyncio.to_thread(encode_silk, pcm, silk, self.voice.bit_rate)
            result["silk_path"] = str(silk)
            result["files"].append({"kind": "voice", "path": str(silk), "name": "演唱.silk", "mime": "audio/silk"})
        for item in result["files"]:
            item.update(size=Path(item["path"]).stat().st_size, sha256=await asyncio.to_thread(file_hash, Path(item["path"])))
        manifest["result"] = result
        update("ready", state="ready", finished_at=time.time(), duration=duration)
        return result

    async def _separate_cached(self, song, folder, manifest, **options):
        """Only complete, hash-verified stems can bypass the heavy separation worker."""
        model_root = Path(self.config.engine_directory) / "checkpoints/torch/checkpoints"
        model_identity = [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(model_root.glob("*.th"))]
        key = hashlib.sha256(json.dumps({"input": manifest["inputs"]["song"]["sha256"],
            "engine": "htdemucs-shifts0-overlap025-v1", "weights": model_identity}, sort_keys=True).encode()).hexdigest()
        cache = Path(self.config.cache_dir).resolve() / "separation" / key
        paths = {name: str(folder / (name + ".wav")) for name in ("vocals", "instrumental")}
        if self.config.reuse_separation:
            try:
                saved = json.loads((cache / "cache.json").read_text(encoding="utf-8"))
                for name in paths:
                    file = cache / (name + ".wav")
                    if await asyncio.to_thread(file_hash, file) != saved[name]:
                        raise ValueError("分轨缓存校验失败。")
                    with wave.open(str(file), "rb") as audio:
                        if audio.getnframes() <= 0:
                            raise ValueError("分轨缓存为空。")
                for name, target in paths.items():
                    await asyncio.to_thread(shutil.copyfile, cache / (name + ".wav"), target)
                manifest["separation"] = {"cache_hit": True, "key": key}
                return paths
            except (OSError, ValueError, KeyError, wave.Error):
                pass
        result = await self._worker("separate", folder, {"source": str(song), **paths}, **options)
        manifest["separation"] = {"cache_hit": False, "key": key}
        if self.config.reuse_separation:
            cache.mkdir(parents=True, exist_ok=True)
            saved = {}
            for name in paths:
                source = Path(result[name])
                temporary = cache / (name + "." + uuid.uuid4().hex + ".tmp")
                await asyncio.to_thread(shutil.copyfile, source, temporary)
                temporary.replace(cache / (name + ".wav"))
                saved[name] = await asyncio.to_thread(file_hash, source)
            atomic_json(cache / "cache.json", saved)
        return result


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
