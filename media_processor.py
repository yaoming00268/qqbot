"""Download, inspect and convert media without passing network inputs to ffmpeg."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import mimetypes
from pathlib import Path
import re
import shutil
import uuid
import zipfile
import urllib.parse

from config import MediaConfig
from media_crawler import MediaCrawler, needs_resolution
from media_downloader import MediaError, contained_path, download_url, file_signature, image_metadata, public_url, sha256_file
from media_models import MediaAsset, MediaIntent


def safe_name(name, fallback="文件"):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(str(name)).name).strip(" .")[:160] or fallback


async def run_process(args, *, timeout=60, max_output=1024 * 1024):
    executable = shutil.which(args[0])
    if not executable:
        raise MediaError("视频处理需要安装 ffmpeg 和 ffprobe，或在媒体配置中指定路径。")
    proc = await asyncio.create_subprocess_exec(executable, *args[1:], stdout=asyncio.subprocess.PIPE,
                                               stderr=asyncio.subprocess.PIPE)
    async def read_bounded(stream):
        chunks, total = [], 0
        while True:
            chunk = await stream.read(32768)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > max_output:
                raise MediaError("视频处理输出异常。")
            chunks.append(chunk)
    readers = [asyncio.create_task(read_bounded(proc.stdout)), asyncio.create_task(read_bounded(proc.stderr))]
    try:
        async with asyncio.timeout(timeout):
            stdout, _ = await asyncio.gather(*readers)
            await proc.wait()
        if proc.returncode:
            raise MediaError("视频无法解码或转换。")
        return stdout
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


class MediaProcessor:
    def __init__(self, config: MediaConfig):
        self.config = config
        self.cache = Path(config.cache_dir).resolve()
        self.cache.mkdir(parents=True, exist_ok=True)
        self.active_paths = set()

    def limit(self, kind):
        return getattr(self.config, "max_" + kind + "_mb") * 1024 * 1024

    async def accept_artifact(self, asset: MediaAsset, directory):
        """Register completed agent output without downloading or transforming it."""
        def inspect():
            path = contained_path(asset.local_path, [directory])
            from delivery_policy import check_file_size
            check_file_size(path)
            if not path.stat().st_size:
                raise MediaError("任务生成的文件为空。")
            asset.mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if asset.kind == "image":
                from PIL import Image
                try:
                    with Image.open(path) as picture:
                        asset.width, asset.height = picture.size
                        asset.mime = Image.MIME.get(picture.format, asset.mime)
                        picture.verify()
                except (OSError, ValueError) as error:
                    raise MediaError("任务图片无法读取，请由 agent 修正成果。") from error
            elif asset.kind == "video":
                asset.mime = file_signature(path, "video")
            asset.local_path = str(path)
            asset.size = path.stat().st_size
            digest = sha256_file(path)
            if asset.sha256 and asset.sha256 != digest:
                raise MediaError("任务成果在核对后发生了变化，请重新执行核对。")
            asset.sha256 = digest
            asset.metadata["artifact_directory"] = str(Path(directory).resolve())
            return asset
        return await asyncio.to_thread(inspect)

    async def prepare(self, asset: MediaAsset, *, max_duration=0, force_file=False):
        seed, errors = asset, []
        if not needs_resolution(asset):
            try:
                return await self._prepare_download(asset, max_duration=max_duration, force_file=force_file)
            except MediaError as error:
                if (asset.local_path or asset.metadata.get("crawled") or not asset.source_url
                        or asset.source_url == asset.url):
                    raise
                errors.append(str(error))
                # A stale direct link should not hide resources still on its source page.
                seed = asset.model_copy(update={"url": asset.source_url})
        intent = MediaIntent(operation="send_url", kind=asset.kind, query=asset.title,
                             max_duration=max_duration)
        candidates = await MediaCrawler(self.config).discover([seed], intent, limit=12)
        for candidate in candidates:
            if errors and candidate.url == asset.url:
                continue
            if asset.metadata.get("send_as_file"):
                candidate.metadata["send_as_file"] = True
            try:
                return await self._prepare_download(candidate, max_duration=max_duration, force_file=force_file)
            except MediaError as error:
                errors.append(str(error))
        raise MediaError(errors[0] if errors else "已抓取来源页，未找到可下载的资源，页面可能需要登录或动态播放器。")

    async def _prepare_download(self, asset: MediaAsset, *, max_duration=0, force_file=False):
        limit = self.limit("file" if force_file else asset.kind)
        temporary = None
        leased = set()
        generated = set()
        try:
            if asset.local_path:
                path = contained_path(asset.local_path, [self.cache, "data/cache/images", *self.config.local_library_roots])
            else:
                supported = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff", ".mp4", ".webm", ".ogv", ".ogg", ".mov", ".pdf", ".zip", ".txt", ".csv", ".md", ".json", ".log", ".docx", ".xlsx", ".pptx")
                suffix = Path(urllib.parse.urlsplit(asset.url).path).suffix.lower()
                if suffix not in supported:
                    suffix = Path(asset.title).suffix.lower()
                inferred_suffix = suffix not in supported
                if inferred_suffix:
                    suffix = {"image": ".jpg", "video": ".mp4", "file": ".pdf"}[asset.kind]
                path = self.cache / (asset.asset_id + suffix)
                temporary = path
                leased.add(str(path))
                self.active_paths.add(str(path))
                generated.add(path)
                options = {}
                referer = asset.metadata.get("referer") or asset.source_url
                if referer:
                    options["headers"] = {"Referer": public_url(referer)}
                if asset.kind == "image":
                    options["expected_kind"] = "image"
                declared_mime, final_url = await download_url(asset.url, path, max_bytes=limit,
                    timeout=self.config.download_timeout_seconds, **options)
                asset.url = final_url
                if inferred_suffix and asset.kind == "file" and not asset.metadata.get("extract_pdf"):
                    extension = {"application/pdf": ".pdf", "application/zip": ".zip", "text/plain": ".txt",
                                 "text/csv": ".csv", "application/json": ".json",
                                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
                                 "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx"}.get(declared_mime)
                    if extension and path.suffix != extension:
                        renamed = path.with_suffix(extension)
                        path.replace(renamed)
                        path = renamed
                        generated.add(path)
                        leased.add(str(path))
                        self.active_paths.add(str(path))
            leased.add(str(path))
            self.active_paths.add(str(path))
            if path.stat().st_size > limit:
                raise MediaError("资源超过允许的大小。")
            if asset.metadata.get("extract_pdf"):
                extraction = asyncio.create_task(asyncio.to_thread(self._extract_pdf, path, asset.metadata["extract_pdf"], limit))
                try:
                    path = await asyncio.shield(extraction)
                except asyncio.CancelledError:
                    result = await asyncio.gather(extraction, return_exceptions=True)
                    if isinstance(result[0], Path):
                        self.active_paths.discard(str(result[0]))
                        result[0].unlink(missing_ok=True)
                    raise
                generated.add(path)
                leased.add(str(path))
                asset.title = safe_name(asset.title, "教程.pdf")
            asset.mime = await asyncio.to_thread(file_signature, path, asset.kind)
            if asset.kind == "video":
                old_path = path
                if force_file:
                    duration = 0
                else:
                    path, duration = await self.video(path, max_duration or self.config.default_video_max_seconds)
                if old_path != path:
                    asset.metadata["converted"] = True
                    generated.add(path)
                    leased.add(str(path))
                asset.duration = duration
                if not force_file:
                    asset.mime = "video/mp4"
            elif asset.kind == "image":
                meta = await asyncio.to_thread(image_metadata, path)
                asset.width, asset.height = meta["width"], meta["height"]
                suffix = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp", "BMP": ".bmp", "AVIF": ".avif", "TIFF": ".tiff"}[meta["format"]]
                if path.parent == self.cache and path.suffix.lower() != suffix:
                    renamed = path.with_suffix(suffix)
                    path.replace(renamed)
                    path = renamed
                    generated.add(path)
                    leased.add(str(path))
                    self.active_paths.add(str(path))
            if path.stat().st_size > limit:
                raise MediaError("转换后的资源超过允许的大小。")
            asset.local_path = str(path)
            asset.size = path.stat().st_size
            asset.sha256 = await asyncio.to_thread(sha256_file, path)
            if not asset.title:
                asset.title = "资源" + path.suffix
            if force_file and not Path(asset.title).suffix:
                asset.title += path.suffix
            if temporary and path != temporary:
                temporary.unlink(missing_ok=True)
            return asset
        except BaseException:
            for output in generated:
                output.unlink(missing_ok=True)
            raise
        finally:
            self.active_paths.difference_update(leased)

    def _extract_pdf(self, archive, wanted, limit):
        destination = self.cache / (uuid.uuid4().hex + ".pdf")
        self.active_paths.add(str(destination))
        try:
            with zipfile.ZipFile(archive) as zipped:
                matching = [i for i in zipped.infolist() if Path(i.filename).name == wanted and i.file_size <= limit]
                if not matching:
                    raise MediaError("官方文档包中没有找到教程 PDF。")
                with zipped.open(matching[0]) as source, destination.open("wb") as target:
                    total = 0
                    for chunk in iter(lambda: source.read(65536), b""):
                        total += len(chunk)
                        if total > limit:
                            raise MediaError("解压后的 PDF 超过大小限制。")
                        target.write(chunk)
            file_signature(destination, "file")
            return destination
        except BaseException:
            destination.unlink(missing_ok=True)
            self.active_paths.discard(str(destination))
            raise
        finally:
            if archive.parent == self.cache:
                archive.unlink(missing_ok=True)

    async def probe_video(self, path):
        probe = await run_process([self.config.ffprobe_path, "-v", "error", "-protocol_whitelist", "file,pipe",
            "-show_entries", "format=duration,format_name:stream=codec_type,codec_name", "-of", "json", str(path)], timeout=20)
        try:
            info = json.loads(probe)
            duration = float(info.get("format", {}).get("duration", 0))
            streams = info.get("streams", [])
            video = next(s for s in streams if s.get("codec_type") == "video")
        except (ValueError, StopIteration):
            raise MediaError("资源没有有效的视频画面或时长。") from None
        if not duration > 0 or duration == float("inf"):
            raise MediaError("视频时长无效。")
        return duration, video, streams

    async def video(self, path, max_duration):
        duration, video, streams = await self.probe_video(path)
        if duration > max_duration:
            raise MediaError(f"视频超过本次 {max_duration} 秒的时长限制，或时长无效。")
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        if path.suffix.lower() == ".mp4" and video.get("codec_name") == "h264" and all(s.get("codec_name") == "aac" for s in audio):
            return path, duration
        converted = self.cache / (uuid.uuid4().hex + ".mp4")
        self.active_paths.add(str(converted))
        try:
            await run_process([self.config.ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
                "-fs", str(self.limit("video")), str(converted)], timeout=self.config.upload_timeout_seconds)
            actual_duration, _, _ = await self.probe_video(converted)
            if converted.stat().st_size > self.limit("video") or abs(duration - actual_duration) > max(0.2, duration * 0.02):
                raise MediaError("视频转换后超过大小限制或未保留完整时长，请使用较小的视频。")
            return converted, actual_duration
        except BaseException:
            converted.unlink(missing_ok=True)
            self.active_paths.discard(str(converted))
            raise

    def export(self, body, extension):
        if not body.strip():
            raise MediaError("当前还没有可导出的回答。")
        path = self.cache / (uuid.uuid4().hex + "." + extension)
        if extension == "csv":
            lines = body.strip().splitlines()
            if lines and "|" in lines[0]:
                rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines if line.strip()]
                rows = [row for row in rows if not all(re.fullmatch(r"[-: ]+", cell or "-") for cell in row)]
            else:
                rows = list(csv.reader(io.StringIO(body)))
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                # Prevent spreadsheet formula execution when opened locally.
                csv.writer(stream).writerows([["'" + c if c.lstrip().startswith(("=", "+", "-", "@")) else c for c in row] for row in rows])
        else:
            path.write_text(body, encoding="utf-8")
        return MediaAsset(kind="file", title="回答." + extension, local_path=str(path), provider="generated")
