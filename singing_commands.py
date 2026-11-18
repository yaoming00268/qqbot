"""Bound console singing requests; inputs remain scoped and results require deliver."""
from __future__ import annotations
import json
from pathlib import Path
import re

from console_tools.contracts import capabilities
from console_tools.files import outgoing_files
from singing_config import SingingRequest
from voice_config import VoiceConfig


def operate(broker, token, operation, **values):
    context = broker._chat_context(token, allow_committed=True)
    if operation not in capabilities(context).get("sing", []):
        raise PermissionError("当前上下文不能发起唱歌任务。")
    from singing_service import singing_context
    voice = VoiceConfig.model_validate(context.get("voice_config") or {})
    if operation == "status":
        return singing_context(voice)
    if not voice.singing.enabled:
        raise ValueError("唱歌功能尚未开启，请在语音工作室启用。")
    key = values.pop("request_key", "")
    request = SingingRequest.model_validate({**values, "mode": operation})
    if len(request.lyrics) > voice.singing.max_lyrics_chars or not request.lyrics.strip():
        raise ValueError("歌词为空或超过配置上限。")
    if request.mode == "compose" and (request.vocals_only or request.accompaniment):
        raise ValueError("compose 的 song 是伴奏，不能再指定干声或另一份伴奏。")
    inputs = {}
    for name in ("song", "accompaniment"):
        value = getattr(request, name)
        if not value:
            continue
        if value == "latest" or re.fullmatch(r"[0-9a-f]{24}", value):
            if value == "latest":
                files = broker.inbox(token, 100)
                current = [item for item in files if item["source_message_id"] == context.get("source_message_id")]
                choices = current or files[:1]
                if len(choices) != 1:
                    raise ValueError("请使用 inbox 中的明确歌曲 file_id。")
                value = choices[0]["file_id"]
            attachment = broker.attachment(token, value)
            inputs[name] = {"file_id": attachment["id"]}
        elif value.startswith(("http://", "https://")):
            from media_downloader import public_url
            inputs[name] = {"url": public_url(value)}
        else:
            inputs[name] = outgoing_files(context, [value])[0]
            if inputs[name]["size"] > voice.singing.max_input_mb * 1024 * 1024:
                raise ValueError("歌曲文件超过输入大小上限。")
    task = json.dumps(request.model_dump(), ensure_ascii=False, sort_keys=True)
    return broker._enqueue(token, context, "singing", task,
        {"singing": request.model_dump(), "singing_inputs": inputs}, key)


async def execute_singing(main, request, ctx, metadata, compatibility, skill_context):
    from console_files import receive_qq_file
    from media_downloader import http_request
    from media_models import MediaAsset
    from singing_service import SingingService
    from skills.task_skills import RunTaskSkill
    voice = main.client.app_config.voice
    values = dict(compatibility["singing"])
    inputs = compatibility["singing_inputs"]
    input_root = Path(metadata["output_dir"]).resolve() / ("singing-input-" + request["id"])
    for name, descriptor in inputs.items():
        ctx.check_current()
        if descriptor.get("file_id"):
            received = await receive_qq_file(main.client, main.broker, request["token"], descriptor["file_id"], ctx)
            values[name] = received["local_path"]
        elif descriptor.get("url"):
            path = input_root / (name + ".audio")
            await http_request(descriptor["url"], target=path,
                max_bytes=voice.singing.max_input_mb * 1024 * 1024,
                timeout=min(300, voice.singing.timeout_seconds))
            values[name] = str(path)
        else:
            files = outgoing_files(metadata, [descriptor["path"]])
            current = files[0]
            if (current["size"], current["mtime_ns"]) != (descriptor["size"], descriptor["mtime_ns"]):
                raise ValueError("歌曲文件在排队后已变更，请重新提交。")
            values[name] = current["path"]
    result = await SingingService(voice).render(values, check_current=ctx.check_current,
        progress=lambda event: main.broker.update_running_result(request["id"], singing_progress=event))
    service, media_ctx = skill_context.media_service, skill_context.media_context
    if not service.config.enabled:
        raise ValueError("媒体交付未开启，演唱文件已保存在唱歌任务目录。")
    if any(item["kind"] == "file" for item in result["files"]) and not service.config.file_enabled:
        raise ValueError("歌曲已生成，但文件交付未开启；请开启后领取已有文件。")
    job, created = service.store.create_job(media_ctx.media_owner, media_ctx.media_scope, media_ctx.message_id)
    if not created:
        raise ValueError("这条演唱成果已有交付记录，请查询 status，不要重复提交。")
    import asyncio
    import shutil
    directory = Path(service.config.cache_dir).resolve() / "tasks" / ("singing-" + request["id"])
    directory.mkdir(parents=True, exist_ok=True)
    try:
        assets = []
        for item in result["files"]:
            destination = directory / Path(item["path"]).name
            await asyncio.to_thread(shutil.copyfile, item["path"], destination)
            asset = await service.processor.accept_artifact(MediaAsset(kind=item["kind"], title=item["name"],
                local_path=str(destination), sha256=item["sha256"], provider="singing", duration=result["duration"],
                license=__import__("voice_service").load_catalog(voice)["characters"][voice.character].get("license", ""),
                metadata={"send_as_file": item["kind"] == "file", "singing_manifest": result["manifest_path"]}), directory)
            asset.mime = item["mime"]
            assets.append(asset)
        return await RunTaskSkill()._deliver_result(skill_context, "演唱歌曲", directory, job,
            {"result": {key: val for key, val in result.items() if key != "files"}}, [], assets,
            "演唱已完成。" if values["mode"] == "cover" else "已根据伴奏和歌词创作演唱。",
            params={"_defer_delivery": True})
    except BaseException as error:
        import asyncio
        service.update(job, "cancelled" if isinstance(error, asyncio.CancelledError) else "failed", str(error))
        raise
