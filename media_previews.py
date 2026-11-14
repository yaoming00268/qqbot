"""Validated, metadata-free RGB views, kept separate from delivered originals."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import uuid

from PIL import Image

from media_downloader import MediaError, image_metadata, sha256_file


def _preview_bytes(path):
    image_metadata(path)
    with Image.open(path) as picture:
        picture.seek(0)
        # Keep the original coordinate system for OCR and desktop control.
        # Copy pixels into a clean image so ICC/EXIF and uncommon modes never
        # enter the model request. Do not alter the delivered original.
        rgba = picture.convert("RGBA")
        rgb = Image.new("RGB", picture.size, "white")
        rgb.paste(rgba, mask=rgba.getchannel("A"))
        output = io.BytesIO()
        rgb.save(output, "PNG")
        if output.tell() > 16 * 1024 * 1024:
            output = io.BytesIO()
            rgb.save(output, "JPEG", quality=90, progressive=False)
            suffix = ".jpg"
        else:
            suffix = ".png"
        if output.tell() > 18 * 1024 * 1024:
            raise MediaError("图片预览超过模型可接受的大小；请按区域生成预览，原文件已保留。")
        return output.getvalue(), suffix, picture.size


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_image_preview(path, directory=None):
    original = Path(path).resolve(strict=True)
    digest = sha256_file(original)
    data, suffix, size = _preview_bytes(original)
    if sha256_file(original) != digest:
        raise MediaError("图片在生成预览时发生变化，请重新核对。")
    root = Path(directory).resolve() if directory is not None else original.parent
    # A path identity avoids collisions when identical bytes occur in several
    # delivered files; evidence must retain the precise original path.
    identity = hashlib.sha256(str(original).casefold().encode("utf-8")).hexdigest()[:16]
    preview = root / ".media_previews" / (digest + "-" + identity + suffix)
    if not preview.parent.resolve().is_relative_to(root):
        raise MediaError("预览目录指向了任务目录之外，原图已保留。")
    _atomic(preview, data)
    result = {"path": str(original), "sha256": digest, "preview_path": str(preview),
              "preview_sha256": hashlib.sha256(data).hexdigest(), "width": size[0], "height": size[1],
              "mode": "RGB", "frame": 0}
    _atomic(preview.with_suffix(".json"), json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
    return result


def original_for_preview(preview, directory):
    """Resolve view evidence only when current original pixels reproduce it.

    The manifest is a locator, not proof: task scripts can edit it. Re-encoding
    the original prevents a viewed unrelated picture being attached to a file.
    """
    try:
        root, preview = Path(directory).resolve(), Path(preview).resolve(strict=True)
        if preview.parent.name != ".media_previews" or not preview.is_relative_to(root):
            return None
        record = json.loads(preview.with_suffix(".json").read_text(encoding="utf-8"))
        original = Path(record["path"]).resolve(strict=True)
        if not original.is_relative_to(root) or sha256_file(original) != record["sha256"]:
            return None
        actual = sha256_file(preview)
        if actual != record["preview_sha256"]:
            return None
        expected, _, _ = _preview_bytes(original)
        if hashlib.sha256(expected).hexdigest() != actual:
            return None
        return original, record["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
