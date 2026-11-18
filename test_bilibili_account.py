"""Account login, autonomous actions and LiveHime handoff with offline fixtures."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from bilibili_credentials import credentials_from_cookies, load_credentials, save_credentials
from bilibili_operations import schedule
from broadcast_commands import context_binding, operate
from broadcast_service import BroadcastService
from config import AppConfig


class AccountFixture:
    def __init__(self):
        self.posts = []
        self.feed = AsyncMock(return_value={"videos": [{"aid": 42, "bvid": "BV1234567890", "title": "公开内容"}]})
        self.search = AsyncMock(return_value={"videos": []})
        self.video_info = AsyncMock(return_value={"aid": 42, "bvid": "BV1234567890", "content_basis": "metadata"})
        self.comments = AsyncMock(return_value={"comments": []})
        self.notifications = AsyncMock(return_value={"replies": [], "mentions": []})
        self.send_comment = AsyncMock(return_value={"rpid": "123"})
        self.like_video = AsyncMock(return_value={"liked": True})
        self.update_signature = AsyncMock(return_value={"signature": "简介"})
        self.publish_dynamic = AsyncMock(return_value={"dynamic_id": "123"})

    def status(self): return {"sdk_ready": True, "credentials_present": True}
    def credential(self): return object()
    async def disconnect(self): pass


@pytest.fixture
def account(tmp_path):
    config = AppConfig.model_validate({"agent": {"database_path": str(tmp_path / "actors.sqlite3"),
        "actors": {"operator": {"name": "运营角色", "prompt": "根据实际资料参与讨论。"}}},
        "bilibili": {"enabled": True, "allow_publish": True, "autonomy": {"enabled": True,
            "actor_id": "operator", "allow_comments": True, "allow_likes": True,
            "allow_dynamics": True, "allow_profile_update": True, "min_write_interval_seconds": 30}}})
    path = tmp_path / "config.yaml"
    config.save(str(path))
    service = BroadcastService(path, adapter=AccountFixture())
    return config, path, service


def run(service, operation, params=None):
    return asyncio.run(service.execute(operation, params))


def test_browser_cookie_capture_accepts_only_bilibili_domains_and_known_fields():
    jar = SimpleCookie()
    for name, value, domain in (("SESSDATA", "fixture-session", ".bilibili.com"),
            ("bili_jct", "fixture-csrf", "www.bilibili.com"), ("password", "never-store", ".bilibili.com")):
        jar[name] = value
        jar[name]["domain"] = domain
    evil = SimpleCookie()
    evil["SESSDATA"] = "attacker"
    evil["SESSDATA"]["domain"] = "bilibili.com.evil.example"
    assert credentials_from_cookies([jar, evil]) == {"sessdata": "fixture-session", "bili_jct": "fixture-csrf"}


def test_login_storage_roundtrip_is_protected_and_keeps_secrets_out_of_status(tmp_path):
    from bilibili_adapter import BilibiliAdapter
    path = tmp_path / "account.json"
    values = {"sessdata": "fixture-session-secret", "bili_jct": "fixture-csrf-secret"}
    save_credentials(path, values)
    assert load_credentials(path) == values
    if os.name == "nt":
        assert all(value not in path.read_text() for value in values.values())
    result = BilibiliAdapter(path).status()
    assert result["credentials_present"]
    assert all(value not in str(result) for value in values.values())
    saved = path.read_bytes()
    with pytest.raises(ValueError):
        save_credentials(path, {"sessdata": "incomplete"})
    assert path.read_bytes() == saved


@pytest.mark.parametrize("url", ["https://bilibili.com.evil.example/", "http://www.bilibili.com/", "https://u:p@www.bilibili.com/", "file:///config.yaml", "https://www.bilibili.com:8443/"])
def test_embedded_browser_rejects_non_bilibili_entry_urls(url):
    from bilibili_browser import validate_url
    with pytest.raises(ValueError): validate_url(url)


def test_embedded_remote_page_has_no_studio_bridge_and_separate_profile(tmp_path):
    from bilibili_browser import launch
    with patch("webview.create_window") as create, patch("webview.start") as start:
        launch(tmp_path)
    assert create.call_args.kwargs["url"] == "https://www.bilibili.com/"
    assert "js_api" not in create.call_args.kwargs
    assert start.call_args.kwargs["storage_path"].endswith("browser-profile")
    assert start.call_args.kwargs["private_mode"] is False


@pytest.mark.parametrize("operation,params", [("feed", {}), ("search", {"query": "音乐"}),
    ("video", {"bvid": "BV1234567890"}), ("comments", {"oid": 42}), ("notifications", {})])
def test_structured_reads_mark_external_material_as_untrusted(account, operation, params):
    _, _, service = account
    result = run(service, operation, params)
    assert result["untrusted_content"] is True


def test_comment_receipt_replay_is_idempotent_and_changed_payload_is_rejected(account):
    _, _, service = account
    p = {"oid": 42, "root": 10, "parent": 11, "text": "对这段论证有另一种看法。", "request_key": "one-comment"}
    first = run(service, "comment", p)
    assert first["state"] == "accepted"
    assert run(service, "comment", p) == first
    assert service.adapter.send_comment.await_count == 1
    service.adapter.send_comment.assert_awaited_once_with(oid=42, resource_type="video", root=10, parent=11, text=p["text"])
    with pytest.raises(ValueError, match="另一项操作"):
        run(service, "comment", {**p, "text": "不同的内容"})


def test_unknown_network_result_is_durable_and_never_replayed(account):
    _, _, service = account
    service.adapter.send_comment.side_effect = RuntimeError("fixture secret should never appear")
    p = {"oid": 42, "text": "网络超时测试", "request_key": "unknown-once"}
    result = run(service, "comment", p)
    assert result["state"] == "unknown"
    assert "fixture secret" not in str(result)
    assert run(service, "comment", p) == result
    assert service.adapter.send_comment.await_count == 1


def test_rate_limit_and_global_publish_switch_apply_before_network(account):
    config, path, service = account
    run(service, "comment", {"oid": 42, "text": "第一次观点", "request_key": "first"})
    with pytest.raises(PermissionError, match="频率"):
        run(service, "comment", {"oid": 99, "text": "另一条观点", "request_key": "second"})
    config.bilibili.allow_publish = False
    config.save(str(path))
    with pytest.raises(PermissionError):
        run(service, "like", {"bvid": "BV1234567890", "request_key": "denied"})
    service.adapter.like_video.assert_not_awaited()


def test_quota_and_duplicate_protection_survive_a_service_restart(account):
    _, path, service = account
    run(service, "comment", {"oid": 42, "text": "同一份宣传文字", "request_key": "old"})
    with service.store.connect() as db:
        db.execute("UPDATE bilibili_action_limits SET created=created-60")
    restarted = BroadcastService(path, adapter=AccountFixture())
    with pytest.raises(PermissionError, match="相同内容"):
        run(restarted, "comment", {"oid": 99, "text": "同一份宣传文字", "request_key": "new"})
    restarted.adapter.send_comment.assert_not_awaited()


def test_thread_reply_cap_prevents_repeated_argument_in_same_thread(account):
    _, _, service = account
    for index in range(2):
        run(service, "comment", {"oid": 42, "root": 9, "parent": 10, "text": "观点" + str(index), "request_key": str(index)})
        with service.store.connect() as db:
            db.execute("UPDATE bilibili_action_limits SET created=created-60")
    with pytest.raises(PermissionError, match="讨论串"):
        run(service, "comment", {"oid": 42, "root": 9, "parent": 10, "text": "第三次争论", "request_key": "third"})


def test_comment_permissions_and_parent_validation_are_independent(account):
    config, path, service = account
    with pytest.raises(ValueError, match="root"):
        run(service, "comment", {"oid": 42, "parent": 11, "text": "回复", "request_key": "missing-root"})
    config.bilibili.autonomy.allow_comments = False
    config.save(str(path))
    with pytest.raises(PermissionError, match="评论"):
        run(service, "comment", {"oid": 42, "text": "不应发出", "request_key": "denied"})
    service.adapter.send_comment.assert_not_awaited()


def test_scheduler_deduplicates_wakeups_and_honors_restarts_and_switches(account):
    from agent_core.runtime import ActorRuntime
    config, path, service = account
    runtime = ActorRuntime(config.agent)
    assert schedule(service)["scheduled"]
    assert schedule(service)["scheduled"] is False
    event = runtime.store.events("operator")[0]
    assert event["kind"] == "bilibili.account_cycle"
    runtime.store.finish_events([event["id"]])
    restarted = BroadcastService(path, adapter=AccountFixture())
    assert schedule(restarted)["scheduled"] is False
    assert schedule(restarted, force=True)["scheduled"]
    config.bilibili.autonomy.enabled = False
    config.save(str(path))
    with pytest.raises(PermissionError): run(service, "autonomy_run")
    asyncio.run(runtime.close())


def test_account_actor_tools_are_separate_from_untrusted_live_audience(account):
    from console_tools.contracts import capabilities
    _, path, _ = account
    bound = context_binding(path, actor_id="operator", account_ops=True)
    assert bound["broadcast_account_ops"]
    caps = capabilities({**bound, "main_agent": True, "actor_event": True})["broadcast"]
    assert {"feed", "comment", "publish", "notifications"} <= set(caps)
    assert "avatar" not in caps and "live_start" not in caps
    audience = capabilities({"broadcast_config_path": str(path), "broadcast_actor": "operator", "main_agent": True, "actor_event": True})["broadcast"]
    assert set(audience) == {"status", "events", "speak", "perform", "avatar"}


def test_actor_operation_rechecks_grant_and_lease(account, tmp_path):
    from agent_core.runtime import ActorRuntime
    from console_tools.broker import ConsoleBroker
    config, path, _ = account
    runtime = ActorRuntime(config.agent)
    runtime.store.ingest("operator", "fixture", "bilibili.account_cycle", {}, source_id="one")
    owner, _ = runtime.store.claim_events("operator")
    broker = ConsoleBroker(tmp_path / "console.sqlite3")
    context = {**context_binding(path, actor_id="operator", account_ops=True), "namespace": "actor:operator", "scope": "actor:operator",
        "main_agent": True, "actor_event": True, "actor_database": str(runtime.store.path), "actor_lease_owner": owner}
    file = broker.issue_context(context, tmp_path / "contexts")
    token = json.loads(Path(file).read_text())["token"]
    with patch("broadcast_host.execute", return_value={"videos": []}) as network:
        operate(broker, token, "feed")
        assert network.call_args.args[2]["_account_actor"] == "operator"
        config.bilibili.autonomy.enabled = False
        config.save(str(path))
        with pytest.raises(PermissionError, match="授权已撤回"):
            operate(broker, token, "feed")
    asyncio.run(runtime.close())


def test_real_actor_cycle_runs_read_comment_and_draft_without_owner_identity(account):
    from agent_core.runtime import ActorRuntime
    from console_tools.cli import execute
    _, path, service = account
    captured = []
    async def chat(key, prompt, **options):
        context_file = json.loads(prompt)["console_context_file"]
        assert "不辱骂" in options["system_prompt"]
        result = execute("broadcast", context_file, operation="feed", params={"limit": 3})
        captured.append(result)
        result = execute("broadcast", context_file, operation="comment", params={"oid": 42,
            "text": "依据已读内容提出不同意见。", "request_key": "actor-once"})
        assert result["state"] == "accepted"
        draft = execute("broadcast", context_file, operation="draft", params={"broadcast_kind": "dynamic", "text": "今天的内容选题。"})
        assert draft["kind"] == "dynamic"
        execute("finish", context_file)
        return "记录下一次内容选题。"
    async def scenario():
        runtime = ActorRuntime(service.app_config.agent, ai=SimpleNamespace(chat=chat))
        runtime.broadcast_config_path = str(path)
        schedule(service)
        def gateway(path, operation, params):
            return asyncio.run(service.execute(operation, params))
        # CLI tools are synchronous; emulate the real separate HTTP service thread.
        def network(path, operation, params):
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                return pool.submit(gateway, path, operation, params).result(timeout=10)
        try:
            with patch("broadcast_host.execute", side_effect=network):
                await runtime.tick()
                await asyncio.gather(*(task for _, task in runtime.model_tasks.values()))
            assert runtime.store.events("operator")[0]["state"] == "processed"
            assert service.adapter.send_comment.await_count == 1
            assert captured[0]["untrusted_content"]
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_autonomous_dynamic_publish_needs_its_own_permission(account):
    config, path, service = account
    p = {"broadcast_kind": "dynamic", "text": "账号介绍", "_account_actor": "operator"}
    draft = run(service, "draft", p)
    config.bilibili.autonomy.allow_dynamics = False
    config.save(str(path))
    with pytest.raises(PermissionError):
        run(service, "publish", {"draft_id": draft["id"], "revision": draft["revision"],
                                 "request_key": "no-publish", "_account_actor": "operator"})
    service.adapter.publish_dynamic.assert_not_awaited()


def test_studio_binds_account_actor_without_changing_live_actor(tmp_path):
    from ui.studio_api import StudioAPI
    api = StudioAPI(tmp_path)
    api.bootstrap()
    result = api.broadcast("bind_account_actor")
    assert result["ok"], result
    config = AppConfig.load(str(tmp_path / "config.yaml"))
    assert config.bilibili.autonomy.actor_id in config.agent.actors
    assert config.bilibili.actor_id == ""
    assert not config.bilibili.autonomy.enabled and not config.bilibili.allow_publish


def test_livehime_window_handoff_never_starts_obs_or_platform_stream(account):
    from bilibili_desktop import prepare_livehime
    config, path, _ = account
    with patch("vtuber_host.overlay_url", return_value="http://127.0.0.1:1234/overlay?preview=1#token=private"), \
            patch("bilibili_desktop.open_browser", return_value={"opened": True}) as browser, patch("vtuber_host.execute") as obs:
        result = prepare_livehime(path.parent, config)
    assert result["stream_started"] is False
    assert "private" not in str(result)
    assert browser.call_args.kwargs["preview"] is True
    obs.assert_not_called()


def test_livehime_virtual_camera_handoff_uses_obs_protocol_without_push_stream(account):
    from bilibili_desktop import prepare_livehime
    config, path, _ = account
    config.bilibili.livehime.transport = "obs_virtual_camera"
    with patch("vtuber_host.execute", return_value={"outputActive": False}) as obs:
        result = prepare_livehime(path.parent, config)
    actions = [call.args[0] for call in obs.call_args_list]
    assert "obs.virtualcam_start" in actions
    assert "obs.start_stream" not in actions
    assert result["stream_started"] is False
    assert "不携带音频" in result["audio_note"]


def test_frozen_browser_command_has_explicit_role_and_home(tmp_path):
    from bilibili_desktop import browser_command
    with patch("sys.frozen", True, create=True), patch("sys.executable", str(tmp_path / "QQBotStudio.exe")):
        command = browser_command(tmp_path)
    assert command == [str(tmp_path / "QQBot.exe"), "--home", str(tmp_path), "--module", "bilibili-browser"]


def test_sdk_video_metadata_and_missing_subtitle_are_reported_honestly(tmp_path):
    from bilibili_adapter import BilibiliAdapter
    adapter = BilibiliAdapter(tmp_path / "unused.json")
    video = SimpleNamespace(get_info=AsyncMock(return_value={"aid": 42, "bvid": "BV1234567890", "title": "标题",
        "owner": {"name": "作者"}, "cid": 123, "sessdata": "should-not-leak"}), get_subtitle=AsyncMock(return_value={"subtitles": []}))
    with patch.object(adapter, "credential", return_value=object()), patch("bilibili_api.video.Video", return_value=video):
        result = asyncio.run(adapter.video_info("BV1234567890"))
    assert result["content_basis"] == "metadata" and result["subtitle_text"] == ""
    assert "should-not-leak" not in str(result)


def test_sdk_comments_and_reply_target_match_installed_sdk_contract(tmp_path):
    from bilibili_adapter import BilibiliAdapter
    from bilibili_api.comment import CommentResourceType
    adapter = BilibiliAdapter(tmp_path / "unused.json")
    comments = {"replies": [{"rpid": 12, "root": 10, "member": {"mid": "7", "uname": "观众"},
                             "content": {"message": "实际评论"}, "token": "should-not-leak"}]}
    with patch.object(adapter, "credential", return_value=object()), \
            patch("bilibili_api.comment.get_comments", new=AsyncMock(return_value=comments)), \
            patch("bilibili_api.comment.send_comment", new=AsyncMock(return_value={"reply": {"rpid": 13}})) as send:
        result = asyncio.run(adapter.comments(42))
        receipt = asyncio.run(adapter.send_comment(42, "回应", root=10, parent=12))
    assert result["comments"][0]["text"] == "实际评论"
    assert "should-not-leak" not in str(result)
    assert receipt["rpid"] == "13"
    assert send.call_args.args == ("回应", 42, CommentResourceType.VIDEO)
    assert send.call_args.kwargs["root"] == 10 and send.call_args.kwargs["parent"] == 12


def test_sdk_signature_update_preserves_account_name_and_identity(tmp_path):
    from bilibili_adapter import BilibiliAdapter
    adapter = BilibiliAdapter(tmp_path / "unused.json")
    identity = {"name": "原来的昵称", "mid": 7, "birthday": "2000-01-01", "sex": "保密"}
    credential = object()
    with patch.object(adapter, "credential", return_value=credential), \
            patch("bilibili_api.user.get_self_info", new=AsyncMock(return_value=identity)), \
            patch("bilibili_api.user.edit_self_info", new=AsyncMock(return_value={})) as edit:
        result = asyncio.run(adapter.update_signature("新的签名"))
    edit.assert_awaited_once_with("2000-01-01", "保密", "原来的昵称", "新的签名", credential)
    assert result == {"signature": "新的签名", "mid": 7}


def test_persisted_hourly_cap_blocks_new_actions_before_network(account):
    import time
    _, _, service = account
    now = time.time()
    with service.store.connect() as db:
        db.executemany("INSERT INTO bilibili_action_limits VALUES(?,?,?,?,?)",
            [("old-" + str(i), "like", "other", "", now - 100 - i * 60) for i in range(6)])
    with pytest.raises(PermissionError, match="频率"):
        run(service, "like", {"bvid": "BV1234567890", "request_key": "over-quota"})
    service.adapter.like_video.assert_not_awaited()


def test_native_obs_virtual_camera_actions_map_to_protocol_requests(tmp_path):
    from vtuber_service import VTuberRuntime
    from vtuber_config import VTuberConfig
    runtime = VTuberRuntime(VTuberConfig(credentials_file=str(tmp_path / "avatar.json")))
    async def scenario():
        with patch.object(runtime.obs, "request", new=AsyncMock(return_value={"outputActive": True})) as request:
            await runtime.execute("obs.virtualcam_status")
            await runtime.execute("obs.virtualcam_start")
            await runtime.execute("obs.virtualcam_stop")
            assert [call.args[0] for call in request.await_args_list] == ["GetVirtualCamStatus", "StartVirtualCam", "StopVirtualCam"]
        await runtime.close()
    asyncio.run(scenario())


def test_livehime_requires_elevation_only_on_740(tmp_path):
    import bilibili_desktop as desktop
    path = tmp_path / "livehime.exe"
    path.touch()
    error = OSError("requires elevation")
    error.winerror = 740
    with patch.object(desktop, "detect_livehime", return_value=path), \
         patch.object(desktop.subprocess, "Popen", side_effect=error), \
         patch.object(desktop, "_request_livehime_elevation") as request:
        result = desktop.open_livehime(SimpleNamespace(executable=""))
    request.assert_called_once_with(path)
    assert result["launch_requested"] and not result["opened"] and not result["stream_started"]


def test_livehime_other_start_errors_are_not_elevated(tmp_path):
    import bilibili_desktop as desktop
    path = tmp_path / "livehime.exe"
    with patch.object(desktop, "detect_livehime", return_value=path), \
         patch.object(desktop.subprocess, "Popen", side_effect=OSError("other failure")), \
         patch.object(desktop, "_request_livehime_elevation") as request:
        with pytest.raises(OSError):
            desktop.open_livehime(SimpleNamespace(executable=""))
    request.assert_not_called()


def test_livehime_cancelled_uac_is_not_success(tmp_path):
    import bilibili_desktop as desktop
    path = tmp_path / "livehime.exe"
    error = OSError("requires elevation")
    error.winerror = 740
    with patch.object(desktop, "detect_livehime", return_value=path), \
         patch.object(desktop.subprocess, "Popen", side_effect=error), \
         patch.object(desktop, "_request_livehime_elevation", side_effect=PermissionError("cancelled")):
        with pytest.raises(PermissionError):
            desktop.open_livehime(SimpleNamespace(executable=""))


def test_livehime_normal_start_does_not_request_elevation(tmp_path):
    import bilibili_desktop as desktop
    path = tmp_path / "livehime.exe"
    with patch.object(desktop, "detect_livehime", return_value=path), \
         patch.object(desktop.subprocess, "Popen") as process, \
         patch.object(desktop, "_request_livehime_elevation") as request:
        result = desktop.open_livehime(SimpleNamespace(executable=""))
    process.assert_called_once()
    request.assert_not_called()
    assert result["opened"] and not result["stream_started"]
