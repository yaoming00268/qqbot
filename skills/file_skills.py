# -*- coding: utf-8 -*-
"""
File Transfer & Document Export Skills.
Enables generating, exporting, and sending documents/files to QQ Groups and Private chats.
"""

from __future__ import annotations
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict

from skills.base import BaseSkill, SkillParameter, SkillContext, SkillResult
from media_models import MediaAsset

logger = logging.getLogger("FileSkills")


class SendFileSkill(BaseSkill):
    """文件传输与文档发送技能"""
    name = "send_file"
    display_name = "发送文件/文档"
    description = "将文本内容整理为文档（txt、md、csv）或发送已有文件到群文件或私聊中。"
    parameters = [
        SkillParameter(name="file_name", param_type="string", description="文件名（如 report.txt、data.csv、summary.md）", required=True),
        SkillParameter(name="content", param_type="string", description="需要写入并发送的文件文本内容", required=False, default=""),
        SkillParameter(name="file_path", param_type="string", description="已存在的安全本地文件路径（可选）", required=False, default="")
    ]

    async def execute(self, params: Dict[str, Any], context: SkillContext) -> SkillResult:
        file_name = str(params.get("file_name") or "document.txt").strip()
        content = str(params.get("content") or "")
        file_path = str(params.get("file_path") or "").strip()

        # 安全限制：净化文件名，防止路径穿越及非法设备名
        clean_name = os.path.basename(file_name).replace("\\", "").replace("/", "").strip()
        safe_name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', '_', clean_name).strip(". ")
        reserved_devices = {"con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9"}
        if not safe_name or safe_name in (".", ".."):
            safe_name = "document.txt"
        elif safe_name.lower().split(".")[0] in reserved_devices:
            safe_name = f"file_{safe_name}"

        if not file_path and not content:
            return SkillResult(success=False, message="缺少文件内容或文件路径", action_type="error")

        target_path = None
        from config import MediaConfig
        app_cfg = getattr(context, "app_config", None)
        media_cfg = getattr(app_cfg, "media", None) or MediaConfig()

        if not getattr(media_cfg, "enabled", True):
            return SkillResult(success=False, message="媒体服务已禁用", action_type="error")
        if not getattr(media_cfg, "file_enabled", True):
            return SkillResult(success=False, message="文件发送功能已禁用", action_type="error")

        cache_dir = Path(media_cfg.cache_dir) / "exports"
        cache_dir.mkdir(parents=True, exist_ok=True)

        max_file_bytes = getattr(media_cfg, "max_file_mb", 100) * 1024 * 1024

        if content:
            encoded = content.encode("utf-8")
            if len(encoded) > max_file_bytes:
                return SkillResult(success=False, message=f"文件大小超出限制 ({media_cfg.max_file_mb}MB)", action_type="error")
            target_path = (cache_dir / safe_name).resolve()
            if not target_path.is_relative_to(cache_dir.resolve()):
                target_path = (cache_dir / "document.txt").resolve()
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(content)
        elif file_path:
            p = Path(file_path).expanduser().resolve()
            if not p.is_file():
                return SkillResult(success=False, message=f"文件不存在: {file_path}", action_type="error")

            authorized_roots = [
                Path(media_cfg.cache_dir).expanduser().resolve(),
                Path("data/cache/images").expanduser().resolve(),
                Path("data/cache/exports").expanduser().resolve(),
                (Path(media_cfg.cache_dir) / "exports").expanduser().resolve(),
                *[Path(r).expanduser().resolve() for r in getattr(media_cfg, "local_library_roots", [])]
            ]
            if not any(p.is_relative_to(r) for r in authorized_roots):
                return SkillResult(success=False, message=f"源文件不在授权资料库或媒体缓存内: {file_path}", action_type="error")

            if p.stat().st_size > max_file_bytes:
                return SkillResult(success=False, message=f"文件大小超出限制 ({media_cfg.max_file_mb}MB)", action_type="error")

            # 确保文件进入已允许的安全媒体缓存目录
            target_path = (cache_dir / (p.name or safe_name)).resolve()
            if not target_path.is_relative_to(cache_dir.resolve()):
                target_path = (cache_dir / safe_name).resolve()
            if p.resolve() != target_path.resolve():
                import shutil
                shutil.copy2(p, target_path)

        logger.info(f"📁 [SendFileSkill] 准备发送文件: {safe_name} (路径: {target_path})")

        if context and getattr(context, "media_context", None):
            try:
                from media_sender import MediaSender

                sender = MediaSender(media_cfg)
                asset = MediaAsset(
                    kind="file",
                    title=safe_name,
                    local_path=str(target_path),
                    mime="text/plain" if safe_name.endswith(".txt") else "application/octet-stream"
                )
                service = getattr(context, "media_service", None)
                if service and hasattr(service, "store"):
                    service.store.put_asset(context.media_context.media_scope, asset)
                receipt = await sender.send(context.media_context, asset, as_file=True)
                return SkillResult(
                    success=True,
                    message=f"已成功将文件「{safe_name}」发送到会话中",
                    action_type="file",
                    data={"file_name": safe_name, "receipt": receipt}
                )
            except Exception as e:
                logger.error(f"[SendFileSkill] 发送失败: {e}", exc_info=True)
                return SkillResult(success=False, message=f"发送文件失败: {e}", action_type="error")

        return SkillResult(success=False, message="缺少有效传输上下文", action_type="error")
