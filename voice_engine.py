"""Start/stop only the independently installed, explicitly configured local engine."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

import aiohttp

from voice_text import VoiceError


def backend_config(config):
    if config.provider == "voicestudio" and config.voicestudio.local_bridge:
        return config.model_copy(update={"provider": "gpt_sovits_v2", "endpoint": config.voicestudio.backend_endpoint})
    return config


def backend_endpoint(config):
    return backend_config(config).endpoint


def runtime_directory(config=None):
    namespace = getattr(config, "process_namespace", "qq")
    return Path({"broadcast": "data/broadcast/voice", "osu-live": "data/osu-live/voice"}.get(namespace, "data/voice")).resolve()


def studio_status(config=None):
    import psutil
    try:
        data = json.loads((runtime_directory(config) / "studio-process.json").read_text(encoding="utf-8"))
        process = psutil.Process(data["pid"])
        if (abs(process.create_time() - data["create_time"]) > .01 or
                Path(process.exe()).resolve() != Path(data["python"]).resolve() or
                data["script"] not in process.cmdline() or data["config"] not in process.cmdline()):
            return {"running": False, "owned": False}
        return {**data, "running": True, "owned": True}
    except (OSError, ValueError, KeyError, psutil.Error):
        return {"running": False, "owned": False}


def start_studio(config):
    import psutil
    from runtime_paths import resource_path
    parsed = urlsplit(config.endpoint)
    if not config.voicestudio.local_bridge or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.path.strip("/"):
        raise VoiceError("自动启动 VoiceStudio 仅支持本机轻量服务；完整或远程服务请自行启动。")
    status = studio_status(config)
    identity = config.model_dump(mode="json")
    for key in ("engine_python", "engine_directory", "engine_config", "catalog_path", "cache_dir"):
        identity[key] = str(Path(identity[key]).resolve())
    identity["voicestudio"]["source_directory"] = str(Path(identity["voicestudio"]["source_directory"]).resolve())
    # Playback and final-file caching do not change the inference service.
    signature = {key: value for key, value in identity.items() if key not in (
        "singing", "training", "enabled", "provider", "character", "default_language", "speed", "auto_start",
        "reuse_audio", "streaming", "bit_rate", "crossfade_ms", "pause_ms", "chunk_chars",
        "delivery_mode", "max_ahead_seconds", "queue_blocks")}
    worker = resource_path("voice_runtime/voicestudio_bridge.py") if getattr(__import__("sys"), "frozen", False) else Path(__file__).with_name("voicestudio_bridge.py")
    worker_stamp = [str(worker.resolve()), worker.stat().st_size, worker.stat().st_mtime_ns]
    if status["running"]:
        if status.get("endpoint") != config.endpoint or status.get("settings") != signature or status.get("worker_stamp") != worker_stamp:
            stop_studio(config)
        else:
            return status
    directory = Path(config.voicestudio.source_directory).resolve()
    if not (directory / "backend/services/tts_backend.py").is_file():
        raise VoiceError("VoiceStudio 尚未安装，请先运行 voice.bat studio-setup。")
    settings = runtime_directory(config) / "studio-config.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    temp = settings.with_suffix(".tmp")
    temp.write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(settings)
    python = Path(config.engine_python).resolve()
    log = settings.with_name("studio-engine.log")
    with log.open("ab") as stream:
        process = subprocess.Popen([str(python), "-X", "utf8", str(worker), "--home", str(Path.cwd()),
            "--config", str(settings), "--port", str(parsed.port or 9881)], cwd=Path.cwd(),
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUTF8": "1"}, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    owned = psutil.Process(process.pid)
    data = {"pid": owned.pid, "create_time": owned.create_time(), "python": str(python), "script": str(worker),
            "config": str(settings), "endpoint": config.endpoint, "settings": signature, "worker_stamp": worker_stamp, "log": str(log)}
    path = settings.with_name("studio-process.json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**data, "owned": True, "running": True}


def stop_studio(config=None):
    import psutil
    status = studio_status(config)
    if status["running"]:
        process = psutil.Process(status["pid"])
        children = [*reversed(process.children(recursive=True)), process]
        for child in children:
            with __import__("contextlib").suppress(psutil.NoSuchProcess):
                child.terminate()
        _, alive = psutil.wait_procs(children, timeout=5)
        for child in alive:
            with __import__("contextlib").suppress(psutil.NoSuchProcess):
                child.kill()
    return {"running": False, "owned": status.get("owned", False)}


async def ready_studio(config, *, check_current=None):
    await ready(backend_config(config), check_current=check_current)
    async with __import__("voice_service").engine_lock("studio-start:" + config.endpoint):
        await asyncio.to_thread(start_studio, config)
        for _ in range(100):
            if check_current:
                check_current()
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as client:
                    async with client.get(config.endpoint + "/health", allow_redirects=False) as response:
                        health = await response.json()
                        if response.status == 200 and health.get("integration") == "qqbot-voicestudio":
                            return {"running": True, "reused": True, "health": health}
            except (aiohttp.ClientError, TimeoutError, ValueError):
                pass
            if not studio_status(config)["running"]:
                raise VoiceError("VoiceStudio 启动后退出，请查看 " + str(runtime_directory(config) / "studio-engine.log"))
            await asyncio.sleep(.2)
    raise VoiceError("VoiceStudio 尚未就绪，请查看 " + str(runtime_directory(config) / "studio-engine.log"))


def state_path(config=None):
    path = runtime_directory(config) / "engine-process.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _engine_status_with_process(config=None):
    import psutil
    try:
        data = json.loads(state_path(config).read_text(encoding="utf-8"))
        process = psutil.Process(data["pid"])
        if abs(process.create_time() - data["create_time"]) > 0.01 or process.exe().casefold() != data["python"].casefold():
            return {"owned": False, "running": False}, None
        arguments = process.cmdline()
        if data["script"] not in arguments or data["config"] not in arguments:
            return {"owned": False, "running": False}, None
        return {**data, "owned": True, "running": process.is_running()}, process
    except (OSError, ValueError, KeyError, psutil.Error):
        return {"owned": False, "running": False}, None


def engine_status(config=None):
    return _engine_status_with_process(config)[0]


def start_engine(config, *, on_started=None):
    import psutil
    status = engine_status(config)
    if status["running"]:
        if status.get("endpoint") != config.endpoint:
            raise VoiceError("已有本地引擎使用另一个接口地址；请先停止该引擎再切换地址。")
        return status
    parsed = urlsplit(config.endpoint)
    if config.provider != "gpt_sovits_v2" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.path.strip("/"):
        raise VoiceError("自动启动仅支持本机 api_v2 根地址；远程或角色接口请启动对应服务。")
    python = Path(config.engine_python).resolve()
    directory = Path(config.engine_directory).resolve()
    script, settings = directory / "api_v2.py", Path(config.engine_config).resolve()
    if not all(path.is_file() for path in (python, script, settings)):
        raise VoiceError("独立语音引擎尚未安装完整，请检查 Python、引擎目录与推理配置。")
    log = state_path(config).parent / "engine.log"
    with log.open("ab") as output:
        process = subprocess.Popen([str(python), "-X", "utf8", str(script), "-a", parsed.hostname,
            "-p", str(parsed.port or 9880), "-c", str(settings)], cwd=directory,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            env={**os.environ, "NLTK_DATA": str(directory.parent / "nltk_data")},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    owned = None
    try:
        owned = psutil.Process(process.pid)
        data = {"pid": process.pid, "create_time": owned.create_time(), "python": str(python),
            "script": str(script), "config": str(settings), "endpoint": config.endpoint, "log": str(log), "started_at": time.time()}
        if on_started: on_started(owned, dict(data))
        path = state_path(config)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(path)
        return {**data, "running": True, "owned": True}
    except BaseException:
        if owned is not None:
            _stop_owned_process(owned)
        else:
            try: process.terminate(); process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired): pass
        raise


def _stop_owned_process(process):
    """Stop only this verified Process object and its descendants."""
    import psutil
    try:
        processes = [*reversed(process.children(recursive=True)), process]
    except psutil.NoSuchProcess:
        return
    for target in processes:
        try: target.terminate()
        except psutil.NoSuchProcess: pass
    _, alive = psutil.wait_procs(processes, timeout=10)
    for target in alive:
        try: target.kill()
        except psutil.NoSuchProcess: pass
    psutil.wait_procs(alive, timeout=5)


def stop_engine(config=None, *, expected_identity=None):
    status, process = _engine_status_with_process(config)
    if expected_identity is not None and (status.get("pid"), status.get("create_time"), status.get("endpoint")) != expected_identity:
        return {**status, "stopped": False, "reason": "identity_changed"}
    if not status["running"] or process is None:
        return status
    _stop_owned_process(process)
    return {"running": False, "owned": True, "stopped_pid": status["pid"]}


async def ready(config, *, check_current=None, on_started=None):
    if config.provider == "voicestudio":
        if not config.voicestudio.local_bridge:
            return {"running": True, "managed": False}
        return await ready_studio(config, check_current=check_current)
    async def reachable():
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as client:
                async with client.get(config.endpoint + "/openapi.json", allow_redirects=False) as response:
                    data = await response.json()
                    return response.status == 200 and "/tts" in data.get("paths", {})
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return False
    if await reachable():
        return {**engine_status(config), "running": True, "reused": True}
    async with __import__("voice_service").engine_lock("start:" + config.endpoint):
        if await reachable():
            return {"running": True, "reused": True}
        startup = asyncio.create_task(asyncio.to_thread(start_engine, config, **({"on_started": on_started} if on_started is not None else {})))
        try:
            status = await asyncio.shield(startup)
        except asyncio.CancelledError:
            while not startup.done():
                try: await asyncio.shield(startup)
                except asyncio.CancelledError: continue
                except BaseException: break
            if startup.done():
                try: startup.result()
                except BaseException: pass
            raise
        for _ in range(180):
            if check_current:
                check_current()
            if await reachable():
                return {**status, "ready": True}
            if not engine_status(config)["running"]:
                raise VoiceError("语音引擎启动后退出，请查看 " + str(runtime_directory(config) / "engine.log"))
            await asyncio.sleep(0.5)
        raise VoiceError("语音引擎尚未就绪，进程和日志已保留。")
