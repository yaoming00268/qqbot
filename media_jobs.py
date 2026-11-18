"""Scoped media workflows, cancellation, bounded concurrency and send ledger."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import hashlib
import logging
from pathlib import Path
import time
import urllib.parse

from media_downloader import MediaError, clean_cache, contained_path
from media_crawler import needs_resolution
from media_models import MediaAsset, MediaIntent, MessageEnvelope, ReplyWindowExpired
from media_processor import MediaProcessor
from media_router import parse_media_intent
from media_search import MediaSearcher
from media_sender import MediaSender
from media_store import MediaStore, TERMINAL
from messaging import SendError, SendUnknown, split_text

logger = logging.getLogger("MediaJobs")

STATE_NAMES = {"queued": "排队中", "searching": "搜索中", "downloading": "下载和校验中", "verifying": "分析核对图片中", "uploading": "发送中", "ready": "资源已准备，等待领取",
               "accepted": "平台已受理", "failed": "失败", "cancelled": "已取消", "unknown": "发送状态未知"}


class MediaService:
    def __init__(self, config, *, namespace=""):
        if namespace:
            cache_key = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:20]
            config = config.model_copy(update={"cache_dir": str(Path(config.cache_dir) / cache_key)})
        self.config = config
        self.searcher = MediaSearcher(config)
        self.processor = MediaProcessor(config)
        self.sender = MediaSender(config)
        self.store = MediaStore(config.database_path)
        self.semaphore = asyncio.Semaphore(config.max_concurrent_downloads)
        self.tasks: dict[str, tuple[asyncio.Task, str, str]] = {}
        self.active_leases: dict[str, str] = {}
        self.recovered = set()
        self.namespace = namespace
        self._delivery_locks = {}
        if config.gemini_web.transport == "cookie":
            # Keep receiving login updates from existing Cookie connections.
            from gemini_browser_link import resume_browser_link
            try:
                resume_browser_link(config.gemini_web)
            except (OSError, ValueError):
                logger.info("Gemini 浏览器连接未恢复，可在媒体页重新连接；其他媒体功能继续可用。")

    def register_task_directory(self, job_id: str, directory: Path | str):
        path_str = str(Path(directory).resolve())
        self.active_leases[job_id] = path_str
        self.store.register_lease(job_id, path_str, ttl=self.config.cache_ttl_hours * 3600)

    def release_task_directory(self, job_id: str):
        self.active_leases.pop(job_id, None)
        self.store.release_lease(job_id)

    def remember_reply(self, ctx, text):
        self.store.remember_reply(ctx.media_owner, str(text))

    def cancel_scope(self, scope, *, clear=True):
        for job_id, (task, _, task_scope) in list(self.tasks.items()):
            if task_scope == scope:
                task.cancel()
                self.release_task_directory(job_id)
        if clear:
            self.store.clear_scope(scope)

    def cancel_all(self):
        for job_id, (task, _, scope) in list(self.tasks.items()):
            task.cancel()
            self.release_task_directory(job_id)
            self.store.clear_scope(scope)
        self.store.clear_namespace(self.namespace)

    async def emit(self, ctx, text):
        ctx.check_current()
        chunks = split_text(text, ctx.max_text_length, 1 if ctx.transport == "official" else None)
        for chunk in chunks:
            ctx.check_current()
            if ctx.transport == "official":
                await ctx.send_text(chunk, msg_seq=ctx.allocate_sequence())
            else:
                await ctx.send_text(chunk)
        return "".join(chunks)

    def update(self, job, state, detail="", assets=None):
        if state == "failed":
            job["failure_stage"] = job.get("_stage", "")
        job["_stage"] = state
        self.store.update_job(job["id"], state, detail, assets)
        if state in TERMINAL:
            self.release_task_directory(job["id"])
        print("[MEDIA_JOB] " + json.dumps({"id": job["id"], "group_id": job["scope"].split("::persona:", 1)[0].rsplit(":", 1)[-1],
            "state": state, "detail": detail, "time": time.strftime("%H:%M:%S")}, ensure_ascii=False), flush=True)

    def _fill_outcome(self, ctx, job, outcome):
        if outcome is None:
            return
        current = self.store.job(ctx.media_owner, job["id"])
        sends = self.store.sends(job["id"])
        accepted = sum(state == "accepted" for state in sends.values())
        linked = sum(state == "link" for state in sends.values())
        reply_handled = bool(job.get("_reply_handled")) or bool(outcome.get("reply_handled")) or ("候选" in str(current.get("detail", "")))
        outcome.update(state=current["state"], message=current["detail"], job_id=job["id"],
                       failure_stage=job.get("failure_stage", ""),
                       agent_session_key=job.get("_agent_session_key", ""),
                       accepted=accepted, linked=linked,
                       reply_handled=reply_handled, reply_text=job.get("_reply_text", "") or outcome.get("reply_text", ""),
                       success=current["state"] == "accepted" and (accepted + linked > 0 or reply_handled))

    async def handle(self, ctx: MessageEnvelope, intent: Optional[MediaIntent] = None, *, outcome=None):
        if outcome is not None:
            outcome.update(success=False, state="failed", message="媒体请求未完成。", accepted=0, linked=0)
        if intent is None:
            intent = parse_media_intent(ctx.text, self.config.default_image_count)
        if intent is None:
            return False
        if not self.config.enabled:
            if outcome is not None:
                outcome.update(message="媒体功能当前已关闭。")
            await self.emit(ctx, "媒体功能当前已关闭。")
            return True
        if intent.operation == "help":
            await self.emit(ctx, "媒体功能：搜三张橘猫图片发我；搜一分钟以内的猫咪视频；发第二个；下载这个链接；引用图片说“原图作为文件发我”；把上一条回答整理成 txt；任务进度；取消下载。也支持 /image、/video、/file、/library、/groupfile、/pick、/next。")
            return True
        if ctx.media_scope not in self.recovered:
            self.store.recover(ctx.media_scope)
            self.recovered.add(ctx.media_scope)
        try:
            if intent.operation in ("status", "cancel"):
                await self.control(ctx, intent)
                return True
            job, created = self.store.create_job(ctx.media_owner, ctx.media_scope, ctx.message_id)
            if not created:
                self._fill_outcome(ctx, job, outcome)
                text = await self.emit(ctx, f"任务 {job['id']}：{STATE_NAMES.get(job['state'], job['state'])}，不会重复发送。")
                if outcome is not None:
                    outcome.update(reply_handled=True, reply_text=text)
                return True
            self.update(job, "queued")
            task = asyncio.create_task(self.execute(ctx, intent, job))
            self.tasks[job["id"]] = (task, ctx.media_owner, ctx.media_scope)
            try:
                await task
            except asyncio.CancelledError:
                if "unknown" not in self.store.sends(job["id"]).values():
                    self.update(job, "cancelled", "任务已取消。")
                # A user reset/explicit cancel must not send an old response.
            finally:
                self.tasks.pop(job["id"], None)
            self._fill_outcome(ctx, job, outcome)
            return True
        except (MediaError, SendError) as error:
            if outcome is not None:
                outcome.update(message=str(error))
            await self.emit(ctx, str(error))
            return True

    async def _clean_cache_if_needed(self):
        try:
            protected = self.store.active_paths(self.config.cache_ttl_hours * 3600)
            await asyncio.to_thread(clean_cache, self.config.cache_dir,
                max_bytes=self.config.cache_max_mb * 1024 * 1024,
                ttl_seconds=self.config.cache_ttl_hours * 3600,
                protected=lambda: [*protected, *self.processor.active_paths, *self.active_leases.values()])
        except Exception as e:
            logger.debug("Cache cleaning exception: %s", e)

    async def execute(self, ctx, intent, job):
        assets = []
        await self._clean_cache_if_needed()
        try:

            if self.config.search_provider == "native":
                native_intent = intent
                if intent.operation == "next":
                    previous = self.store.results(ctx.media_owner, intent.result_id)
                    original = MediaIntent.model_validate_json(previous["query"])
                    if original.operation in ("search", "search_send", "send_url"):
                        prior = [self.store.asset(ctx.media_scope, aid) for aid in json.loads(previous["assets"])]
                        native_intent = original.model_copy(update={"request_text":
                            (original.request_text or original.query) + "\n用户现在要求换一批，请继续寻找不同的合适资源。"
                            + "\n避免重复之前的成果：" + "\n".join(a.source_url or a.title for a in prior)})
                if native_intent.operation in ("search", "search_send", "send_url"):
                    from skills.base import SkillContext
                    from skills.task_skills import RunTaskSkill, media_task
                    key = ctx.ai_session_key or ctx.media_scope
                    key = key if key.startswith("__work__:") else "__work__:" + key
                    result = await RunTaskSkill().execute({"task": media_task(native_intent, ctx),
                        "output_kind": native_intent.kind, "_requires_network": True,
                        "_as_file": native_intent.as_file, "_intent": native_intent, "_job": job},
                        SkillContext(key, ctx.conversation_id, ctx.sender_id, is_group=ctx.is_group,
                                     ai_client=ctx.ai_client, media_context=ctx, media_service=self))
                    job["_reply_handled"] = result.reply_handled
                    job["_reply_text"] = result.message if result.reply_handled else ""
                    if result.action_type == "error":
                        if self.store.job(ctx.media_owner, job["id"])["state"] not in TERMINAL:
                            self.update(job, "failed", result.message)
                        await self.safe_error(ctx, result.message)
                    return
            async with asyncio.timeout(self.config.task_timeout_seconds or None):
                async with self.semaphore:
                    ctx.check_current()
                    protected = self.store.active_paths(self.config.cache_ttl_hours * 3600)
                    await asyncio.to_thread(clean_cache, self.config.cache_dir,
                        max_bytes=self.config.cache_max_mb * 1024 * 1024,
                        ttl_seconds=self.config.cache_ttl_hours * 3600,
                        protected=lambda: [*protected, *self.processor.active_paths])
                    target_count = None
                    if intent.operation == "export":
                        body = ctx.last_reply or self.store.last_reply(ctx.media_owner)
                        # Allow explicit tabular/text content following a colon.
                        if "：" in ctx.text or ":\n" in ctx.text:
                            body = ctx.text.split("：", 1)[-1] if "：" in ctx.text else ctx.text.split(":\n", 1)[-1]
                        assets = [self.processor.export(body, intent.extension)]
                    elif intent.operation == "quote":
                        for image in ctx.quoted_images:
                            path = image.get("local_path") or ""
                            if image.get("capture_mode") == "unavailable":
                                raise MediaError("QQ 截图暂不可用，尚未获得这条消息的画面；未自动下载原图。")
                            if path:
                                path = str(contained_path(path, ["data/cache/images", self.config.cache_dir]))
                            assets.append(MediaAsset(kind="image", title=Path(path).name if path else "原图", local_path=path,
                                                     url=image.get("url", ""), provider="quoted",
                                                     metadata={"capture_mode": image.get("capture_mode", "")}))
                        if not assets:
                            raise MediaError("请引用包含图片的消息；目前没有找到可获取的原图。")
                    elif intent.operation == "send_url":
                        assets = [MediaAsset(kind=intent.kind, url=intent.url, source_url=intent.url,
                            title=Path(urllib.parse.unquote(urllib.parse.urlsplit(intent.url).path)).name, provider="user_url")]
                    elif intent.operation == "pick":
                        result = self.store.results(ctx.media_owner, intent.result_id)
                        original = MediaIntent.model_validate_json(result["query"])
                        intent.query = original.query
                        intent.max_duration = original.max_duration
                        intent.as_file = intent.as_file or original.as_file
                        ids = json.loads(result["assets"])
                        if intent.index > len(ids):
                            raise MediaError(f"本次结果只有 {len(ids)} 项，请选择其中的编号。")
                        assets = [self.store.asset(ctx.media_scope, ids[intent.index - 1])]
                    else:
                        self.update(job, "searching")
                        page = 0
                        if intent.operation == "next":
                            result = self.store.results(ctx.media_owner)
                            replacement = intent.send_requested
                            intent = MediaIntent.model_validate_json(result["query"])
                            page = result["page"] + 1
                            if intent.operation == "search_send":
                                intent.operation = "search"
                            if replacement:
                                intent.operation = "search_send"
                                intent.count = 1
                        if intent.operation == "library":
                            assets = await asyncio.to_thread(self.searcher.library, intent, page=page)
                            if not assets and intent.kind == "image":
                                assets = await self.searcher.search(intent, page=page,
                                    ai_client=ctx.ai_client, session_key=ctx.ai_session_key)
                        elif intent.operation == "group_files":
                            assets = await self.group_files(ctx, intent, page)
                        else:
                            assets = await self.searcher.search(intent, page=page,
                                ai_client=ctx.ai_client, session_key=ctx.ai_session_key)
                        if not assets:
                            raise MediaError("没有找到符合条件且可获取的资源，请换个关键词或调整尺寸、时长条件。")
                        auto_send = (intent.operation == "search_send" or
                            intent.operation in ("library", "group_files") and intent.send_requested and
                            (len(assets) == 1 or intent.latest or intent.kind == "image"))
                        if not auto_send:
                            # Candidate pages contain only the visible options;
                            # this does not limit an actual file delivery batch.
                            assets = assets[:5]
                        for asset in assets:
                            self.store.put_asset(ctx.media_scope, asset)
                        rid = self.store.put_results(ctx.media_owner, intent.model_dump_json(), intent.kind, page, assets,
                                                     self.config.result_ttl_minutes * 60)
                        if not auto_send:
                            lines = [f"搜索结果 {rid}："]
                            for index, asset in enumerate(assets, 1):
                                duration = f"，{asset.duration:.0f}秒" if asset.duration else ""
                                size = f"，{asset.size / 1048576:.1f}MB" if asset.size else ""
                                size += "，仅原页面" if not asset.url and not asset.local_path else ""
                                lines.append(f"{index}. {asset.title[:90]}{duration}{size}\n{asset.source_url or '已登记资料库/本群文件'}")
                            lines.append("回复“发第二个”选择，或“下一页”。")
                            summary = "\n".join(lines)
                            await self.emit(ctx, summary)
                            job["_reply_handled"], job["_reply_text"] = True, summary
                            self.store.remember_reply(ctx.media_owner, summary)
                            self.update(job, "accepted", f"已返回 {min(5, len(assets))} 条候选。")
                            return
                        target_count = intent.count
                    for asset in assets:
                        asset.metadata["send_as_file"] = intent.as_file
                        self.store.put_asset(ctx.media_scope, asset)
                    self.update(job, "downloading", assets=[a.asset_id for a in assets])
                    completed = []
                    link_fallbacks = []
                    errors = []
                    if intent.as_file and not self.config.file_enabled:
                        raise MediaError("文件发送功能当前已关闭。")
                    for asset in assets:
                        if target_count is not None and len(completed) >= target_count:
                            break
                        if not getattr(self.config, asset.kind + "_enabled"):
                            raise MediaError("这种媒体类型当前已关闭。")
                        ctx.check_current()
                        try:
                            if asset.provider == "group_files":
                                await self.resolve_group_file(ctx, asset)
                            if asset.provider == "agent_task":
                                ready = await self.processor.accept_artifact(asset, self.config.cache_dir)
                            else:
                                ready = await self.processor.prepare(asset, max_duration=intent.max_duration, force_file=intent.as_file)
                            if ready.kind == "image" and asset.provider != "agent_task" and intent.operation not in ("quote", "send_url"):
                                if not self.searcher.matches_dimensions(ready, intent):
                                    raise MediaError("图片实际尺寸不符合本次方向或比例要求。")
                                from visual_verification import verify_media_image
                                self.update(job, "verifying", ready.title)
                                await verify_media_image(ctx, ready, intent)
                            self.store.put_asset(ctx.media_scope, ready)
                            completed.append(ready)
                        except MediaError as error:
                            errors.append(str(error))
                            if asset.kind == "video" and asset.source_url and needs_resolution(asset):
                                asset.url = ""
                                asset.metadata["download_error"] = str(error)
                                link_fallbacks.append(asset)
                    if not completed and link_fallbacks:
                        completed.append(link_fallbacks[0])
                    if not completed:
                        raise MediaError(errors[0] if errors else "没有可发送的资源。")
                    completed, skipped = await self.prepare_delivery(ctx, job, completed)
                    self.update(job, "ready", assets=[a.asset_id for a in completed])
                    summary = f"准备发送 {len(completed)} 项资源。"
                    if intent.operation == "quote":
                        summary += ("发送的是 QQ 当前显示画面的截图。" if any(a.metadata.get("capture_mode") for a in completed)
                                    else "发送当前可获取的图片文件，清晰度取决于原消息提供的资源。")
                    if intent.operation == "search_send" and len(completed) < intent.count:
                        summary += f"本次只找到 {len(completed)} 项可用结果（请求 {intent.count} 项）。"
                    sources = list(dict.fromkeys(a.source_url for a in completed if a.source_url))
                    if sources:
                        summary += "\n来源：" + "\n".join(sources)
                    credits = list(dict.fromkeys(" / ".join(filter(None, (a.author[:80], a.license))) for a in completed))
                    if any(credits):
                        summary += "\n" + "；".join(c for c in credits if c)
                    if errors:
                        summary += f"\n另有 {len(errors)} 项未能获取：{errors[0]}"
                    if not getattr(intent, "silent_summary", False):
                        await self.emit(ctx, summary)
                        job["_reply_handled"], job["_reply_text"] = True, summary
                    else:
                        logger.info("🔇 [MediaService] 静默摘要模式生效，不向聊天推送准备提示: %s", summary.replace("\n", " "))
                    await self.send_assets(ctx, job, completed)
                    self.store.remember_reply(ctx.media_owner, summary)
        except asyncio.CancelledError:
            # send ledger remains unknown if submission already started.
            state = "unknown" if "unknown" in self.store.sends(job["id"]).values() else "cancelled"
            self.update(job, state, "任务已取消，已提交的发送请核对聊天记录。")
            raise
        except ReplyWindowExpired as error:
            self.update(job, "ready", str(error))
        except SendUnknown as error:
            self.update(job, "unknown" if "unknown" in self.store.sends(job["id"]).values() else "failed", str(error))
            await self.safe_error(ctx, str(error))
        except TimeoutError:
            state = "unknown" if "unknown" in self.store.sends(job["id"]).values() else "failed"
            self.update(job, state, "媒体任务超过总时间限制。")
            await self.safe_error(ctx, "媒体任务超时，请查询任务进度后再试。")
        except (MediaError, SendError) as error:
            self.update(job, "unknown" if "unknown" in self.store.sends(job["id"]).values() else "failed", str(error))
            if not intent.defer_errors or job.get("failure_stage") not in ("searching", "downloading", "verifying"):
                await self.safe_error(ctx, str(error))
        except Exception:
            logger.exception("Media task failed: %s", job["id"])
            self.update(job, "unknown" if "unknown" in self.store.sends(job["id"]).values() else "failed", "媒体处理失败。")
            await self.safe_error(ctx, "媒体处理失败，请调整资源或关键词后重试。")

    async def prepare_delivery(self, ctx, job, assets):
        """Persist one reusable delivery plan; there is no file count ceiling."""
        from delivery_policy import DeliveryTooLarge, check_file_size, zip_assets
        threshold = self.config.delivery_batch_size
        max_bytes = self.config.delivery_max_file_mb * 1024 * 1024
        assets = list({asset.asset_id: asset for asset in assets}.values())
        key = hashlib.sha256(json.dumps([a.asset_id for a in assets]).encode()).hexdigest()
        async with self._delivery_locks.setdefault(job["id"], asyncio.Lock()):
            cached = self.store.delivery_plan(job["id"], key)
            if cached is not None:
                job["_delivery_skipped"] = cached["skipped"]
                return [MediaAsset.model_validate(item) for item in cached["files"]], cached["skipped"]
            eligible, local, links, skipped, leased = [], [], [], [], set()
            roots = [self.config.cache_dir, "data/cache/images", "data/cache/exports", *self.config.local_library_roots]
            try:
                for asset in assets:
                    ctx.check_current()
                    if asset.local_path:
                        path = contained_path(asset.local_path, roots)
                        try:
                            asset.size = check_file_size(path, max_bytes)
                        except DeliveryTooLarge as error:
                            skipped.append({"asset_id": asset.asset_id, "title": asset.title or path.name,
                                            "size": path.stat().st_size, "reason": str(error)})
                            continue
                        asset.local_path = str(path)
                        leased.add(str(path))
                        self.processor.active_paths.add(str(path))
                        local.append(asset)
                    else:
                        links.append(asset)
                    eligible.append(asset)
                if len(local) > threshold:
                    if not self.config.file_enabled:
                        raise MediaError(f"超过 {threshold} 个文件需要 ZIP 交付，请启用文件发送。")
                    directory = Path(self.config.cache_dir).resolve() / "deliveries" / job["id"] / key
                    eligible = await zip_assets(local, directory, job["id"] + key, self.processor.active_paths, leased,
                                                max_bytes=max_bytes)
                    eligible.extend(links)
                ctx.check_current()
                for asset in eligible:
                    asset.metadata["delivery_prepared"] = job["id"]
                self.store.save_delivery_plan(job, key, eligible, skipped)
                job["_delivery_skipped"] = skipped
                return eligible, skipped
            finally:
                self.processor.active_paths.difference_update(leased)

    async def send_assets(self, ctx, job, assets, *, check_current=None):
        from delivery_policy import DeliveryTooLarge
        if not all(asset.metadata.get("delivery_prepared") == job["id"] for asset in assets):
            assets, skipped = await self.prepare_delivery(ctx, job, assets)
        else:
            skipped = job.get("_delivery_skipped", [])
        for asset in assets:
            if check_current:
                check_current()
            # Already submitted items, including uncertain sends, are never
            # repeated when a prepared batch is resumed.
            previous = self.store.sends(job["id"]).get(asset.asset_id)
            if previous == "unknown":
                raise SendUnknown("这项文件的发送状态不明，请先核对聊天记录。")
            if previous in ("accepted", "link", "skipped"):
                continue
            if previous:
                raise SendError("这项文件已有失败回执，请检查任务记录。")
            ctx.check_reply_available()
            self.update(job, "uploading", asset.title)
            try:
                receipt = await self.sender.send(ctx, asset, as_file=asset.metadata.get("send_as_file", False),
                    before_submit=lambda: self.store.begin_send(job["id"], asset.asset_id))
            except DeliveryTooLarge as error:
                self.store.begin_send(job["id"], asset.asset_id)
                self.store.receipt(job["id"], asset.asset_id, "skipped", str(error))
                skipped.append({"title": asset.title, "reason": str(error)})
                continue
            except SendUnknown:
                raise
            except SendError:
                if asset.asset_id in self.store.sends(job["id"]):
                    self.store.receipt(job["id"], asset.asset_id, "failed")
                raise
            self.store.receipt(job["id"], asset.asset_id, receipt.status, json.dumps(asdict(receipt)))
            print("[MEDIA_ASSET] " + json.dumps({"job_id": job["id"], "title": asset.title,
                "kind": asset.kind, "path": asset.local_path, "mime": asset.mime, "size": asset.size,
                "duration": asset.duration, "state": receipt.status}, ensure_ascii=False), flush=True)
        states = self.store.sends(job["id"])
        linked = sum(value == "link" for value in states.values())
        accepted = sum(value == "accepted" for value in states.values())
        omitted = f"另有 {len(skipped)} 个文件超过交付大小上限，已跳过。" if skipped else ""
        self.update(job, "accepted", f"平台已受理 {accepted} 项媒体，返回 {linked} 项来源链接。" + omitted)

    async def collect_ready(self, ctx, job):
        current = self.store.job(ctx.media_owner, job["id"])
        if current.get("state") == "cancelled":
            return
        try:
            async with asyncio.timeout(self.config.task_timeout_seconds or None):
                async with self.semaphore:
                    states = self.store.sends(job["id"])
                    assets = [self.store.asset(ctx.media_scope, aid) for aid in json.loads(job["assets"]) if aid not in states]
                    if assets:
                        await self.send_assets(ctx, job, assets)
        except ReplyWindowExpired as error:
            self.update(job, "ready", str(error))
        except asyncio.CancelledError:
            self.update(job, "unknown" if "unknown" in self.store.sends(job["id"]).values() else "cancelled", "领取已取消。")
            raise
        except Exception as error:
            self.update(job, "unknown" if "unknown" in self.store.sends(job["id"]).values() else "failed", "领取失败，请检查资源和通道配置。")
            await self.safe_error(ctx, str(error) if isinstance(error, (MediaError, SendError)) else "领取失败，请稍后重试。")

    async def safe_error(self, ctx, text):
        if ctx.guard():
            try:
                await self.emit(ctx, text)
            except Exception:
                logger.info("Could not report media task state")

    async def control(self, ctx, intent):
        job = self.store.job(ctx.media_owner, intent.job_id)
        if intent.operation == "cancel":
            running = self.tasks.get(job["id"])
            if running:
                running[0].cancel()
                await asyncio.gather(running[0], return_exceptions=True)
                sends = self.store.sends(job["id"])
                if "unknown" in sends.values():
                    self.update(job, "unknown", "任务已取消，但已有提交状态未知，请核对聊天记录。")
                    await self.emit(ctx, "任务已取消，但已有提交状态未知，请核对聊天记录。")
                else:
                    self.update(job, "cancelled", "已取消任务。")
                    await self.emit(ctx, "已取消任务；已被平台受理的消息不会自动撤回。")
            elif job["state"] == "ready":
                sends = self.store.sends(job["id"])
                if "unknown" in sends.values():
                    self.update(job, "unknown", "任务已取消，但已有提交状态未知，请核对聊天记录。")
                    await self.emit(ctx, "任务已取消，但已有提交状态未知，请核对聊天记录。")
                else:
                    self.update(job, "cancelled", "已取消待领取的任务。")
                    await self.emit(ctx, "已取消待领取的任务；未发送的资源不会继续发送。")
            else:
                await self.emit(ctx, "该任务已结束，已发送的消息无法通过取消下载撤回。")
            return

        current = self.store.job(ctx.media_owner, job["id"])
        await self.emit(ctx, f"任务 {current['id']}：{STATE_NAMES.get(current['state'], current['state'])}。{current['detail']}")
        # A fresh request renews the passive reply window. Only deliver items
        # with no previous submission; never automatically retry unknown sends.
        states = self.store.sends(current["id"])
        if current["state"] == "ready" and "unknown" not in states.values() and current["id"] not in self.tasks:
            task = asyncio.create_task(self.collect_ready(ctx, current))
            self.tasks[current["id"]] = (task, ctx.media_owner, ctx.media_scope)
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                self.tasks.pop(current["id"], None)

    async def group_files(self, ctx, intent, page):
        if ctx.transport != "onebot" or not ctx.is_group:
            raise MediaError("群文件查询目前需要在 OneBot 群聊中使用。")
        result = await ctx.send_action("get_group_root_files", {"group_id": int(ctx.conversation_id)})
        files = (result.get("data") or {}).get("files", [])
        # Bound folder traversal; do not cross the current group.
        for folder in ((result.get("data") or {}).get("folders", []))[:10]:
            nested = await ctx.send_action("get_group_files_by_folder", {"group_id": int(ctx.conversation_id),
                "folder_id": folder.get("folder_id", "")})
            files.extend(((nested.get("data") or {}).get("files", []))[:100])
        words = intent.query.lower().split()
        matched = [f for f in files if all(w in str(f.get("file_name", "")).lower() for w in words)]
        matched.sort(key=lambda f: f.get("upload_time", 0), reverse=True)
        return [MediaAsset(kind="file", title=f.get("file_name", "群文件"), size=f.get("file_size", 0), provider="group_files",
            metadata={"group_id": ctx.conversation_id, "file_id": f.get("file_id"), "busid": f.get("busid", 0)})
            for f in matched[page * 5:(page + 1) * 5]]

    async def resolve_group_file(self, ctx, asset):
        if ctx.transport != "onebot" or not ctx.is_group or asset.metadata.get("group_id") != ctx.conversation_id:
            raise MediaError("该群文件不属于当前群聊。")
        result = await ctx.send_action("get_group_file_url", {"group_id": int(ctx.conversation_id),
            "file_id": asset.metadata["file_id"], "busid": asset.metadata["busid"]})
        asset.url = (result.get("data") or {}).get("url", "")
        if not asset.url:
            raise MediaError("群文件链接已失效。")
