"""File delivery limits and streaming ZIP bundles, independent of model work."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import threading
import uuid
import zipfile

from media_downloader import MediaError, sha256_file
from media_models import MediaAsset

MAX_FILE_BYTES = 1024 ** 3
ZIP_THRESHOLD = 20


class DeliveryTooLarge(MediaError):
    pass


def check_file_size(path, max_bytes=None):
    size = Path(path).stat().st_size
    limit = MAX_FILE_BYTES if max_bytes is None else min(MAX_FILE_BYTES, max_bytes)
    if size > limit:
        description = "1 GB" if limit == 1024 ** 3 else f"{limit / (1024 * 1024):g} MB"
        raise DeliveryTooLarge(f"{Path(path).name} 超过 {description}，已跳过发送。")
    return size


async def zip_assets(assets, directory, plan_key, active_paths, leased, *, max_bytes=None):
    """Bundle every file, splitting into ordinary ZIPs below the upload limit.

    Disk streaming bounds memory. Cancellation joins the writer before removing
    its paths, so cache cleanup cannot race an abandoned background thread.
    """
    cancelled = threading.Event()
    created = []
    directory = Path(directory)
    limit = MAX_FILE_BYTES if max_bytes is None else min(MAX_FILE_BYTES, max_bytes)

    def check_cancelled():
        if cancelled.is_set():
            raise InterruptedError("ZIP preparation cancelled")

    def build():
        directory.mkdir(parents=True, exist_ok=True)
        groups, group, estimate = [], [], 22
        for index, asset in enumerate(assets, 1):
            from media_processor import safe_name
            path = Path(asset.local_path)
            size = check_file_size(path, limit)
            # Numbered member names retain duplicate basenames safely. zlib's
            # compressBound plus ZIP64/UTF-8 headers gives a conservative bound.
            name = f"{index:05d}-" + safe_name(asset.title or path.name)
            if not Path(name).suffix and path.suffix:
                name += path.suffix
            bound = size + (size >> 12) + (size >> 14) + (size >> 25) + 13 + 256 + 2 * len(name.encode("utf-8"))
            if group and estimate + bound > limit:
                groups.append(group)
                group, estimate = [], 22
            group.append((asset, path, name))
            estimate += bound
        if group:
            groups.append(group)

        output = []
        def pack(items):
            check_cancelled()
            ordinal = len(output) + 1
            path = directory / f"任务结果-{ordinal:03d}.zip"
            partial = path.with_suffix(".zip.part")
            for target in (partial, path):
                active_paths.add(str(target))
                leased.add(str(target))
                created.append(target)
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
                for asset, source, name in items:
                    digest, total = hashlib.sha256(), 0
                    with source.open("rb") as stream, archive.open(name, "w", force_zip64=True) as member:
                        while True:
                            check_cancelled()
                            chunk = stream.read(1024 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > limit:
                                raise MediaError("打包中的成果发生了大小变化，请重新核对。")
                            digest.update(chunk)
                            member.write(chunk)
                    if asset.sha256 and digest.hexdigest() != asset.sha256:
                        raise MediaError("打包中的成果发生了内容变化，请重新核对。")
            check_cancelled()
            if partial.stat().st_size > limit:
                partial.unlink(missing_ok=True)
                if len(items) > 1:
                    middle = len(items) // 2
                    pack(items[:middle])
                    pack(items[middle:])
                else:
                    # A valid file can fit exactly while ZIP overhead cannot.
                    # Keep the original deliverable instead of losing the file.
                    output.append(items[0][0])
                return
            partial.replace(path)
            output.append(MediaAsset(asset_id=uuid.uuid5(uuid.NAMESPACE_URL, plan_key + ":" + str(ordinal)).hex,
                kind="file", title=path.name, local_path=str(path), size=check_file_size(path, limit),
                mime="application/zip", sha256=sha256_file(path), provider="delivery_zip",
                metadata={"send_as_file": True, "bundled_files": [{"asset_id": item.asset_id,
                    "title": item.title, "member": name, "source_url": item.source_url} for item, _, name in items]}))

        for items in groups:
            pack(items)
        return output

    writer = asyncio.create_task(asyncio.to_thread(build))
    try:
        return await asyncio.shield(writer)
    except BaseException:
        cancelled.set()
        # Reset/shutdown can cancel the same job more than once. Keep the
        # writer shielded while joining, including during repeated cancellation.
        while not writer.done():
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                cancelled.set()
            except BaseException:
                break
        if not writer.cancelled():
            writer.exception()
        for path in created:
            path.unlink(missing_ok=True)
        active_paths.difference_update(leased)
        raise
    # Successful output leases are released by the service after the durable
    # plan and job asset list have been committed.

