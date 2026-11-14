"""Deliver only the immutable result batch explicitly approved by the main agent."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from media_models import MediaAsset, ReplyWindowExpired
from messaging import OutboundText, SendError, SendUnknown
from runtime_control import generation_is_current


async def deliver_console_result(client, request_id, ctx, metadata):
    console = client._get_main_coordinator()
    row = console.broker.record(request_id)
    if row is None or row["state"] != "ready" or row["scope"] != ctx.scope:
        return

    def check_task():
        if (not ctx.guard() or not generation_is_current(client.ai, metadata["session_key"], metadata["generation"])
                or console.broker.record(request_id)["state"] not in ("ready",)):
            raise asyncio.CancelledError("任务已取消或会话已重置。")
        rt_id = payload.get("runtime_job_id")
        if rt_id and hasattr(client, "_get_runtime"):
            rt = client._get_runtime()
            if rt:
                rt_job = rt.store.db.execute("SELECT state FROM work WHERE id=?", (rt_id,)).fetchone()
                if rt_job and rt_job[0] == "cancelled":
                    console.broker.update(request_id, "cancelled")
                    raise asyncio.CancelledError("任务已取消或会话已重置。")
        job_id = (payload.get("result") or {}).get("job_id") or payload.get("job_id")
        if job_id and hasattr(client, "_get_media_service"):
            srv = client._get_media_service()
            if srv:
                try:
                    m_job = srv.store.job(ctx.media_scope + ":" + metadata["sender_id"], job_id)
                    if m_job and m_job.get("state") == "cancelled":
                        console.broker.update(request_id, "cancelled")
                        raise asyncio.CancelledError("任务已取消或会话已重置。")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass

    payload = json.loads(row["result"])
    service, assets = None, []
    plan = payload.get("delivery") or {}
    if not plan.get("approved_by"):
        return

    def save(state="ready"):
        if service is not None and assets:
            states = service.store.sends((payload.get("result") or {}).get("job_id", ""))
            plan["files"] = [{"asset_id": asset.asset_id, "name": asset.title, "local_path": asset.local_path,
                              "size": asset.size, "sha256": asset.sha256,
                              "state": states.get(asset.asset_id, "pending")} for asset in assets]
        payload["delivery"] = plan
        console.broker.update(request_id, state, payload)

    def record_text(text):
        persona = client.personas.get_persona(client.app_config.bot.get_persona_name(ctx.conversation_id)) or client.personas.get_active_persona()
        client._record_console_text(ctx, persona.name, text)
        client._record_dialogue_reply(ctx, persona.name, text, message_id="bot:task:" + request_id,
            reply_to=plan.get("reply_to", ""))
        print("[FEED_ACTION] " + json.dumps({"time": time.strftime("%H:%M:%S"), "group_id": ctx.conversation_id,
            "action": "task_complete", "reply": text, "target": metadata["sender_id"],
            "reason": "控制台任务交付"}, ensure_ascii=False), flush=True)

    try:
        check_task()
        # The worker's completion request is persisted before dispatch. Read
        # the full console result file, retaining progress only from the broker.
        if payload.get("full_result_file"):
            path = console.directory(metadata["session_key"]) / ("result-" + request_id + ".json")
            completed = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
            if completed.get("request_id") != request_id:
                raise ValueError("任务完成文件与当前任务不一致。")
            result = completed.get("result") or {}
        else:
            # Older prepared records already contain their validated assets.
            result = payload.get("result") or {}
        assets = [MediaAsset.model_validate(item) for item in result.get("files", [])]
        if not any(plan.get(key) for key in (
                "notice_sent", "notice_chunks", "outbox_id", "outbox_ids", "sending_text")):
            # Upgrade a pre-fix approval before its first send. An in-flight
            # notice retains its original chunks and receipts to avoid replay.
            plan["text"] = console.broker.command_result_text(row["kind"],
                {**payload, "result": result}, plan.get("text", ""))
        # Older main-agent file selections are retired: the console sends the
        # complete validated batch, in manifest order.
        plan.update(mode="main_agent", asset_ids=[a.asset_id for a in assets])
        save()
        if plan.get("sending_text"):
            save("unknown")
            return
        service = client._get_media_service()
        if assets:
            if hasattr(client, "send_action") and not client._is_ws_connected():
                # Keep a ready batch on disk while offline. The console watcher
                # resumes it on reconnect; chat and work need not wait here.
                return
            job = service.store.job(ctx.media_scope + ":" + metadata["sender_id"], result["job_id"])
            states = service.store.sends(job["id"])
            if any(states.get(a.asset_id) == "unknown" for a in assets):
                save("unknown")
                return
            unsent = [a for a in assets if states.get(a.asset_id) not in ("accepted", "link", "skipped")]
            if unsent:
                check_task()
                await service.send_assets(ctx, job, unsent, check_current=check_task)
            states = service.store.sends(job["id"])
            if any(states.get(a.asset_id) not in ("accepted", "link", "skipped") for a in assets):
                raise SendError("文件尚未获得平台回执，保留成果，未发送完成通知。")

        pending = plan.get("outbox_ids") or ([plan["outbox_id"]] if plan.get("outbox_id") else [])
        if pending:
            marks = ",".join("?" for _ in pending)
            states = [item[0] for item in client._get_runtime().store.db.execute(
                "SELECT state FROM outbox WHERE id IN (" + marks + ")", pending)]
            if len(states) != len(pending) or any(state in ("unknown", "cancelled", "failed") for state in states):
                # Earlier chunks may already be accepted. Do not replay the
                # whole notice because one part failed or lost its receipt.
                save("unknown")
                return
            if any(state != "accepted" for state in states):
                return
            else:
                record_text(plan.pop("outbox_text", "任务完成"))
                plan.pop("outbox_id", None)
                plan.pop("outbox_ids", None)
                plan["notice_sent"] = True
                save()

        if not plan.get("notice_sent"):
            check_task()
            ctx.check_reply_available()
            text = plan.get("text", "")
            if not text:
                plan["notice_sent"] = True
                save("delivered")
                client._finish_console_work(payload)
                return
            if payload.get("success"):
                skipped = len(result.get("skipped_files") or [])
                if assets:
                    skipped += sum(service.store.sends(result["job_id"]).get(a.asset_id) == "skipped" for a in assets)
                if skipped:
                    text += f"（{skipped} 个文件超过交付大小上限，已跳过）"
            plan["sending_text"] = True
            save()
            if hasattr(client, "send_action"):
                text_message = OutboundText(text, quote=plan.get("reply_to", ""), mention=plan.get("mention", ""), reply_quote=False)
                response = await (client.send_group_message(ctx.conversation_id, text_message) if ctx.is_group
                                  else client.send_private_message(ctx.conversation_id, text_message))
                plan.pop("sending_text", None)
                if isinstance(response, dict) and response.get("status") == "queued":
                    data = response["data"]
                    plan.update(outbox_id=data["outbox_id"],
                                outbox_ids=data.get("outbox_ids") or [data["outbox_id"]], outbox_text=text)
                    save()
                    return
                if isinstance(response, dict) and response.get("status") in ("unknown", "cancelled"):
                    save("unknown" if response["status"] == "unknown" else "cancelled")
                    return
                if isinstance(response, dict) and (response.get("status") == "failed" or response.get("retcode") not in (None, 0)):
                    payload["delivery_error"] = str(response)
                    save("ready")
                    return
            else:
                from messaging import split_text
                chunks = plan.get("notice_chunks") or split_text(text, client.app_config.bot.max_reply_length)
                next_chunk = plan.get("notice_next_chunk", 0)
                remaining = chunks[next_chunk:]
                slots = 5 - ctx.sequence
                if len(remaining) > slots:
                    remaining = remaining[:slots - 1] + ["".join(remaining[slots - 1:])]
                    chunks = chunks[:next_chunk] + remaining
                plan["notice_chunks"] = chunks
                for index in range(next_chunk, len(chunks)):
                    check_task()
                    sequence = ctx.allocate_sequence()
                    options = {"message_reference": {"message_id": plan["reply_to"], "ignore_get_message_error": True}} if plan.get("reply_to") and index == 0 else {}
                    body = (f"<@{plan['mention']}> " if plan.get("mention") and index == 0 else "") + chunks[index]
                    plan["sending_text"] = True
                    save()
                    await ctx.send_text(body, msg_seq=sequence, **options)
                    plan.pop("sending_text", None)
                    plan["notice_next_chunk"] = index + 1
                    save()
                plan.pop("sending_text", None)
            record_text(text)
            plan["notice_sent"] = True
        save("delivered")
        client._finish_console_work(payload)
    except ReplyWindowExpired:
        plan.pop("sending_text", None)
        save()
    except SendUnknown:
        save("unknown")
    except SendError as error:
        plan.pop("sending_text", None)
        payload["delivery_error"] = str(error)[:1000]
        save("ready")
        raise
    except asyncio.CancelledError:
        uncertain = plan.get("sending_text") or (service and assets and
            "unknown" in service.store.sends((payload.get("result") or {}).get("job_id", "")).values())
        if uncertain:
            save("unknown")
        elif not console.closed:
            save("cancelled")
        # During shutdown, ready output remains durable for the next process.
        raise
    except Exception as error:
        plan.pop("sending_text", None)
        payload["delivery_error"] = str(error)[:1000]
        uncertain = service and assets and "unknown" in service.store.sends((payload.get("result") or {}).get("job_id", "")).values()
        save("unknown" if uncertain else "ready")
        raise
