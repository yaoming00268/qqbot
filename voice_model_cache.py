"""Reuse confirmed model selection only for our verified, live api_v2 process."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid


def engine_identity(endpoint):
    import psutil
    for path in (Path("data/voice/engine-process.json"), Path("data/broadcast/voice/engine-process.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["endpoint"] != endpoint:
                continue
            process = psutil.Process(data["pid"])
            args = process.cmdline()
            if (abs(process.create_time() - data["create_time"]) > .01
                    or Path(process.exe()).resolve() != Path(data["python"]).resolve()
                    or data["script"] not in args or data["config"] not in args):
                continue
            return {key: data[key] for key in ("pid", "create_time", "python", "endpoint")}
        except (OSError, ValueError, KeyError, psutil.Error):
            continue
    return None


async def select_models(character, endpoint, request, *, reuse=True):
    identity = engine_identity(endpoint) if reuse else None
    weights = {}
    for key in ("gpt_weights", "sovits_weights"):
        path = Path(character[key])
        weights[key] = [str(path.resolve()), path.stat().st_size, path.stat().st_mtime_ns] if path.is_file() else character[key]
    key = hashlib.sha256(endpoint.encode()).hexdigest()[:24]
    cache = Path("data/voice/model-selection") / (key + ".json")
    expected = {"identity": identity, "weights": weights}
    if identity:
        try:
            if json.loads(cache.read_text(encoding="utf-8")) == expected:
                return {"cache_hit": True, "identity": identity}
        except (OSError, ValueError):
            pass
    cache.unlink(missing_ok=True)
    for route, field in (("/set_gpt_weights", "gpt_weights"), ("/set_sovits_weights", "sovits_weights")):
        result = await request(route, {"weights_path": character[field]})
        try:
            accepted = json.loads(result)
        except ValueError:
            accepted = result.decode("utf-8", errors="replace").strip()
        if accepted not in ("success", {"message": "success"}):
            raise ValueError("推理服务没有确认模型切换：" + route)
    if identity and engine_identity(endpoint) == identity:
        cache.parent.mkdir(parents=True, exist_ok=True)
        temp = cache.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temp.write_text(json.dumps(expected, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temp.replace(cache)
    return {"cache_hit": False, "identity": identity}
