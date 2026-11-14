"""Fast local routing before a conversational model can start slow tool work."""
from __future__ import annotations

import logging
import re

from conversation_runtime import EventContext, current_event
from media_router import is_work_request, parse_media_intent
from messaging import SendError
from skills.base import SkillContext, SkillResult

logger = logging.getLogger("RequestRouting")


async def route_explicit_work(client, ctx, text, *, directed=True, is_master=False, sender_name=""):
    """All recognized commands enter the console before any executor runs."""
    if ctx is None or not directed and not str(text or "").strip().startswith(("/", "／")):
        return False
    from console_tools.chat_commands import parse_command
    try:
        command = parse_command(text, natural=True, is_group=ctx.is_group)
    except ValueError:
        command = True  # malformed slash commands get a queued error result
    if command is None:
        return False
    await client._get_main_coordinator().submit_chat_command(ctx, text)
    return True


async def prepare_slang_context(client, ctx, text):
    """Return only local guidance; resolve recorded gaps on the work queue."""
    from slang_glossary import default_slang_glossary
    try:
        pending = default_slang_glossary.pending_terms(text)
        matched = default_slang_glossary.match(text)
    except (OSError, ValueError) as error:
        logger.warning("话术表暂不可读: %s", error)
        pending, matched = [], []
    if pending and ctx is not None and getattr(client.app_config.bot, "search_enabled", True):
        scheduled = getattr(client, "_slang_pending", None)
        if scheduled is None:
            scheduled = client._slang_pending = {}
        runtime = client._get_runtime()
        for term, job_id in list(scheduled.items()):
            row = runtime.store.db.execute("SELECT state FROM work WHERE id=?", (job_id,)).fetchone()
            if not row or row[0] not in ("queued", "running"):
                scheduled.pop(term, None)
        terms = [term for term in pending if term not in scheduled]
        if terms:
            async def resolve():
                try:
                    entries = await default_slang_glossary.resolve_missing(
                        terms, client.ai, ctx.ai_session_key, check_current=ctx.check_current)
                    return SkillResult(True, "话术表释义已更新", data={"terms": [e.term for e in entries]})
                finally:
                    for term in terms:
                        scheduled.pop(term, None)

            try:
                event = EventContext(ctx.scope, ctx.message_id, ctx.received_at, "work", ctx.owner)
                job_id = runtime.submit_work(event, "补全话术表：" + "、".join(terms), resolve, ctx.guard)
                scheduled.update((term, job_id) for term in terms)
            except SendError as error:
                logger.info("话术表补全暂未排队: %s", error)
    if matched or not getattr(client.app_config.bot, "search_enabled", True):
        return None
    # Existing offline definitions remain useful without any network wait.
    from web_search import OFFLINE_MEME_KNOWLEDGE, default_meme_searcher
    term = default_meme_searcher.detect_meme_or_obscure_phrase(text)
    return OFFLINE_MEME_KNOWLEDGE.get(term)


def image_reply_text(images):
    """Reuse branch recognition in quotes instead of opening files in chat."""
    return "; ".join(str(image.get("visual_description") or image.get("summary") or "图片不可用")
                     for image in images)


async def recognize_cached_images(images, ai_client, session_key):
    from image_handler import process_multimodal_message
    result = []
    for image in images:
        if image.get("vision_verified") and image.get("visual_description"):
            result.append(image)
            continue
        data = {"file": image.get("local_path") or "", "url": image.get("url") or "",
                "summary": image.get("summary") or "引用图片", "subType": "1" if image.get("is_sticker") else "0"}
        _, recognized = await process_multimodal_message("", segments=[{"type": "image", "data": data}],
            ai_client=ai_client, vision_session_key=session_key)
        result.extend(recognized)
    return result
