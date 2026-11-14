"""Prepare scoped media selections for the console without sending to QQ."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import uuid

from media_downloader import MediaError
from media_models import MediaIntent
from skills.base import SkillResult
from skills.task_skills import RunTaskSkill, media_task


async def prepare_quoted_files(context):
    """Copy only peer-checked cached originals, then wait for main approval."""
    from console_tools.files import outgoing_files
    ctx, service = context.media_context, context.media_service
    roots = [str(Path(service.config.cache_dir).resolve()), str(Path("data/cache/images").resolve())]
    paths = [item["local_path"] for item in ctx.quoted_images if item.get("local_path")]
    if not paths:
        raise MediaError("引用消息没有可用的本地原图，请引用一条实际图片消息。")
    files = outgoing_files({"artifact_roots": roots}, paths)
    return await RunTaskSkill().deliver_files(files, context, roots=roots)


async def prepare_media_command(request, context):
    ctx, service = context.media_context, context.media_service
    if not service.config.enabled:
        raise MediaError("媒体功能当前已关闭。")
    payload = json.loads(request["result"])
    name, argument = payload["command"], payload.get("argument", "")
    page = 0
    if name in ("pick", "next"):
        parts = argument.split()
        previous = service.store.results(ctx.media_owner, parts[1] if name == "pick" and len(parts) > 1 else "")
        intent = MediaIntent.model_validate_json(previous["query"])
        page = previous["page"]
        if name == "pick":
            identities = json.loads(previous["assets"])
            index = int(parts[0])
            if index > len(identities):
                raise MediaError(f"本次结果只有 {len(identities)} 项，请选择其中的编号。")
            assets = [service.store.asset(ctx.media_scope, identities[index - 1])]
        else:
            page += 1
            if intent.operation not in ("library", "group_files"):
                prior = [service.store.asset(ctx.media_scope, identity) for identity in json.loads(previous["assets"])]
                task = media_task(intent, ctx) + "\n继续下一批，避免重复：\n" + "\n".join(
                    asset.source_url or asset.title for asset in prior)
                return await RunTaskSkill().execute({"task": task, "output_kind": intent.kind,
                    "_requires_network": True, "_intent": intent, "_result_page": page,
                    "_defer_delivery": True}, context)
    else:
        intent = MediaIntent(operation="library" if name == "library" else "group_files", kind="file",
                             query=argument, request_text=request["task"])
    ctx.check_current()
    if name != "pick":
        if intent.operation == "library":
            assets = await asyncio.to_thread(service.searcher.library, intent, page=page)
        else:
            assets = await service.group_files(ctx, intent, page)
        ctx.check_current()
        if not assets:
            raise MediaError("没有更多符合条件的资源，请调整关键词。")
        assets = assets[:5]
        for asset in assets:
            service.store.put_asset(ctx.media_scope, asset)
        result_id = service.store.put_results(ctx.media_owner, intent.model_dump_json(), intent.kind, page, assets,
                                               service.config.result_ttl_minutes * 60)
        answer = f"搜索结果 {result_id}（第 {page + 1} 页）：\n" + "\n".join(
            f"{index}. {asset.title}" + (f"\n{asset.source_url}" if asset.source_url else "")
            for index, asset in enumerate(assets, 1)) + "\n用 /pick 编号 选择，或 /next 查看下一页。"
        return SkillResult(True, answer, data={"answer": answer, "files": [], "result_id": result_id}, reply_handled=True)
    if not getattr(service.config, assets[0].kind + "_enabled"):
        raise MediaError("这种媒体类型当前已关闭。")
    job, created = service.store.create_job(ctx.media_owner, ctx.media_scope, ctx.message_id)
    if not created:
        raise MediaError("该资源选择已有记录，请查询任务进度。")
    directory = Path(service.config.cache_dir).resolve() / "tasks" / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=True)
    service.register_task_directory(job["id"], directory)
    try:
        asset = assets[0].model_copy(deep=True)
        # A selection is a new delivery, while retries of that selection keep
        # the same persisted job and receipts.
        asset.asset_id = uuid.uuid4().hex
        if asset.provider == "group_files":
            await service.resolve_group_file(ctx, asset)
        ready = await service.processor.prepare(asset, max_duration=intent.max_duration, force_file=intent.as_file)
        ready.metadata["send_as_file"] = intent.as_file
        return await RunTaskSkill()._deliver_result(context, request["task"], directory, job,
            {}, [], [ready], "已准备：" + ready.title,
            params={"_intent": intent, "_result_page": page, "_preserve_results": True, "_defer_delivery": True})
    except BaseException as error:
        service.update(job, "cancelled" if isinstance(error, asyncio.CancelledError) else "failed", str(error))
        raise
    finally:
        service.release_task_directory(job["id"])
