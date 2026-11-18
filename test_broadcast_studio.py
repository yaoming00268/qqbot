"""Real Studio bridge persistence boundaries, without account/device connections."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import hashlib
from pathlib import Path
from unittest.mock import patch

from config import AppConfig
from ui.studio_api import StudioAPI


def test_live_voice_settings_and_qq_settings_persist_independently(tmp_path):
    api = StudioAPI(tmp_path)
    initial = api.bootstrap()["data"]
    original = AppConfig.load(str(tmp_path / "config.yaml")).voice.model_dump()
    result = api.save_config({"broadcast_voice": {"enabled": True, "character": "live-character",
        "endpoint": "http://127.0.0.1:9882", "delivery_mode": "streaming"}}, initial["revision"])
    assert result["ok"], result
    config = AppConfig.load(str(tmp_path / "config.yaml"))
    assert config.voice.model_dump() == original
    assert config.broadcast_voice.enabled and config.broadcast_voice.character == "live-character"
    result = api.save_config({"voice": {"character": "qq-character", "enabled": False}}, result["data"]["revision"])
    assert result["ok"], result
    config = AppConfig.load(str(tmp_path / "config.yaml"))
    assert config.broadcast_voice.character == "live-character"
    assert config.broadcast_voice.enabled
    from voice_engine import runtime_directory
    assert runtime_directory(config.voice) != runtime_directory(config.broadcast_voice)


def test_live_emotion_catalog_uses_live_selected_directory(tmp_path):
    import json
    api = StudioAPI(tmp_path)
    api.bootstrap()
    catalog = tmp_path / "live-catalog.json"
    catalog.write_text(json.dumps({"version": 1, "characters": {"live": {
        "emotions": {"sad": {"description": "轻声难过"}}}}}), encoding="utf-8")
    config = AppConfig.load(str(tmp_path / "config.yaml"))
    config.broadcast_voice.catalog_path = str(catalog)
    config.broadcast_voice.character = "live"
    config.broadcast_voice.enabled = True
    config.voice.catalog_path = "missing-qq-catalog.json"
    config.save(str(tmp_path / "config.yaml"))
    result = api.broadcast("voice_catalog")
    assert result["ok"], result
    assert result["data"]["capability"]["emotions"] == ["sad"]
    assert result["data"]["capability"]["pipeline"] == "broadcast"


def test_live_engine_stop_preserves_qq_engine_and_bridge(tmp_path, monkeypatch):
    import json
    import sys
    import psutil
    from voice_engine import runtime_directory, stop_engine, stop_studio, engine_status, studio_status
    monkeypatch.chdir(tmp_path)
    config = AppConfig()
    terminated, processes = [], {}
    class Process:
        def __init__(self, pid):
            self.pid, self.alive = pid, True
        def create_time(self): return float(self.pid)
        def exe(self): return sys.executable
        def cmdline(self): return ["worker.py", "settings.json"]
        def children(self, recursive=False): return []
        def is_running(self): return self.alive
        def terminate(self):
            terminated.append(self.pid)
            self.alive = False
    for profile, start in ((config.voice, 100), (config.broadcast_voice, 200)):
        directory = runtime_directory(profile)
        directory.mkdir(parents=True)
        for name, pid in (("engine-process.json", start), ("studio-process.json", start+1)):
            processes[pid] = Process(pid)
            (directory / name).write_text(json.dumps({"pid": pid, "create_time": float(pid),
                "python": sys.executable, "script": "worker.py", "config": "settings.json"}), encoding="utf-8")
    monkeypatch.setattr(psutil, "Process", lambda pid: processes[pid])
    monkeypatch.setattr(psutil, "wait_procs", lambda children, timeout: (children, []))
    stop_studio(config.broadcast_voice)
    stop_engine(config.broadcast_voice)
    assert set(terminated) == {200, 201}
    assert engine_status(config.voice)["running"]
    assert studio_status(config.voice)["running"]


def test_binding_current_persona_preserves_identity_and_other_settings(tmp_path):
    api = StudioAPI(tmp_path)
    initial = api.bootstrap()["data"]
    config = AppConfig.load(str(tmp_path / "config.yaml"))
    config.qq.secret = "fixture-secret"
    config.save(str(tmp_path / "config.yaml"))
    response = api.broadcast("bind_actor")
    assert response["ok"], response
    result = response["data"]
    saved = AppConfig.load(str(tmp_path / "config.yaml"))
    actor = saved.agent.actors[saved.bilibili.actor_id]
    selected = next(p for p in initial["personas"]["items"] if p["name"] == initial["personas"]["active"])
    assert actor.persona_id == selected["id"]
    assert saved.agent.bindings["persona:" + selected["id"]] == saved.bilibili.actor_id
    assert saved.qq.secret == "fixture-secret"
    assert saved.bilibili.allow_publish is False
    assert result["actor_id"] == saved.bilibili.actor_id
    assert api.broadcast("bind_actor")["data"]["actor_id"] == result["actor_id"]


def test_explicit_media_import_is_content_addressed_and_reusable(tmp_path):
    api = StudioAPI(tmp_path)
    media = tmp_path / "chosen audio.wav"
    media.write_bytes(b"fixture-wave-bytes")
    response = api.broadcast("import_media", {"path": str(media)})
    assert response["ok"], response
    value = response["data"]
    assert Path(value["path"]).is_relative_to(tmp_path / "data/broadcast/media")
    assert Path(value["path"]).read_bytes() == media.read_bytes()
    assert value["sha256"] == hashlib.sha256(media.read_bytes()).hexdigest()
    assert api.broadcast("import_media", {"path": str(media)})["data"] == value
    forbidden = tmp_path / "not-media.json"
    forbidden.write_text("private fixture")
    assert not api.broadcast("import_media", {"path": str(forbidden)})["ok"]


def test_preview_returns_local_renderer_without_connecting_devices(tmp_path):
    api = StudioAPI(tmp_path)
    with patch("vtuber_host.overlay_url", return_value="http://127.0.0.1:18765/overlay#fixture") as preview, patch("vtuber_host.execute") as control:
        response = api.broadcast("preview")
    assert response["ok"], response
    assert response["data"]["url"].startswith("http://127.0.0.1:")
    preview.assert_called_once()
    control.assert_not_called()


def test_finished_owner_turn_cannot_reuse_broadcast_control(tmp_path):
    import json
    import pytest
    from broadcast_commands import operate
    from console_tools.broker import ConsoleBroker
    config = AppConfig()
    config.vtuber.enabled = True
    path = tmp_path / "config.yaml"
    config.save(str(path))
    broker = ConsoleBroker(tmp_path / "console.sqlite3")
    context_file = broker.issue_context({"namespace": "fixture", "scope": "fixture", "session_key": "fixture",
        "main_agent": True, "is_master": True, "broadcast_config_path": str(path)}, tmp_path / "contexts")
    token = json.loads(Path(context_file).read_text(encoding="utf-8"))["token"]
    broker.seal_messages(token)
    with patch("broadcast_host.execute") as remote:
        with pytest.raises(Exception, match="本轮已经结束"):
            operate(broker, token, "avatar", cue="reset")
        remote.assert_not_called()
