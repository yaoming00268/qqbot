"""Send only service-generated, verified SILK assets through the normal receipt path."""
from __future__ import annotations

import json
import hashlib
import asyncio
from pathlib import Path

from media_models import MediaAsset
from media_sender import MediaSender
from voice_config import VoiceConfig
from voice_service import SILK_HEADER, VoiceService
from voice_text import VoiceError
from messaging import SendUnknown


async def deliver_voice(client, console, ctx, item, *, before_submit):
    binding = console.broker.context(ctx.console_token)
    from voice_live import current_voice
    config = current_voice(client, binding.get("voice_config", {}))

    def current():
        ctx.check_current()
        if client._rebatch_reply_if_needed(ctx):
            raise asyncio.CancelledError("消息积压，语音在提交 QQ 前已取消。")
        # Switching during generation must not submit an asset in the old voice.
        from voice_live import voice_revision
        if voice_revision(current_voice(client, binding.get("voice_config", {}))) != voice_revision(config):
            raise VoiceError("语音设置已切换，旧声音已取消；请查询 voice 后用当前角色重新合成。")

    manifest = await VoiceService(config).synthesize(item["text"], item["emotion"],
        spoken_text=item.get("spoken_text", ""), check_current=current)
    def audit(state, **values):
        manifest.update(state=state, **values)
        try:
            Path(manifest["manifest_path"]).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    try:
        current()
    except asyncio.CancelledError:
        audit("cancelled")
        raise
    path = Path(manifest["silk_path"]).resolve()
    root = Path(config.cache_dir).resolve()
    data = path.read_bytes() if path.is_relative_to(root) else b""
    if not data.startswith(SILK_HEADER) or hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise VoiceError("语音产物未通过路径和 SILK 头校验。")
    asset = MediaAsset(kind="voice", title=config.character, local_path=str(path), mime="audio/silk",
        size=path.stat().st_size, sha256=manifest["sha256"], duration=manifest["duration"],
        license=manifest.get("license", ""), author=manifest.get("attribution", ""),
        metadata={"voice_manifest": manifest["manifest_path"], "emotion": item["emotion"], "converted": True})
    media = client.app_config.media.model_copy(update={"local_library_roots": [
        *client.app_config.media.local_library_roots, str(root)]})
    submitted = False
    def submitting():
        nonlocal submitted
        current()
        before_submit()
        submitted = True
        audit("submitting")
    try:
        receipt = await MediaSender(media).send(ctx, asset, before_submit=submitting,
            reply_to=item.get("reply_to", ""), mention=item.get("mention", ""))
    except BaseException as error:
        audit("unknown" if submitted and isinstance(error, (SendUnknown, asyncio.CancelledError)) else
              "cancelled" if isinstance(error, asyncio.CancelledError) else "failed", error=str(error)[:1000])
        raise
    result = {**vars(receipt), "voice_manifest": manifest["manifest_path"], "duration": manifest["duration"],
              "emotion": item["emotion"], "language": config.default_language}
    # A failed local audit update cannot turn a confirmed remote receipt into a
    # send failure. The broker remains the authoritative delivery ledger.
    audit("accepted", delivery=result)
    return result
