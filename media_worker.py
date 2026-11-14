"""Script-friendly media downloads and safe previews; output contains paths only."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from media_downloader import MediaError, download_url, image_metadata, public_url, sha256_file
from media_previews import _atomic, prepare_image_preview


def worker_command():
    from runtime_paths import module_command
    return module_command("media")


def worker_instructions(directory):
    # JSON argv is unambiguous for scripts and independent of shell quoting.
    command = json.dumps(worker_command(), ensure_ascii=False)
    return ("\n媒体下载与验图：用终端/脚本下载到磁盘，不把网址响应、base64 或全部图片塞入模型上下文。"
        "可用已安装的媒体助手，其 argv 前缀为 " + command + "。"
        "脚本用 subprocess.run(argv列表,check=True)，不要将列表拼成 shell 命令。"
        f"先在 {Path(directory).as_posix()} 内写下载清单 JSON 数组："
        '[{"url":"真实图片地址","path":"相对任务目录的文件名.jpg","referer":"实际来源页URL"}]。'
        '在 argv 后追加 ["download","--manifest","清单绝对路径","--directory","任务目录绝对路径"]，各项都是独立字符串。'
        "助手带 Referer/User-Agent 流式下载，检查 HTTP、空文件、HTML/JSON 伪图片及完整解码，"
        "原子保存原图，生成 RGB 预览并写 media_downloads.json；重复执行只复用哈希一致且仍能解码的原图。"
        "失败项保留具体错误，修正来源/Referer后仅重试这些项，不能拿错误页当成果。"
        '已有文件可追加 ["preview","--path","原图绝对路径","--directory","任务目录绝对路径"] 生成预览。'
        "只用 read_file/view_file 打开助手返回的 preview_path，按需抽查；不要直接打开灰度、CMYK、"
        "WebP、AVIF 或未经检查的下载响应。预览是同尺寸首帧 RGB 静态图片，动画需要另行核对其他帧。"
        "交付 files 仍写原图 path，保留原始质量、完整内容与来源；预览、缓存及内部清单不打进交付包。")


async def download_images(items, directory, *, timeout=45):
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / "media_downloads.json"
    try:
        old = json.loads(ledger.read_text(encoding="utf-8"))
        old = old if isinstance(old, dict) else {}
        known = {row["path"]: row for row in old.get("files", []) if isinstance(row, dict) and (row.get("ok") or row.get("validated"))}
    except (OSError, ValueError, TypeError, KeyError):
        known = {}
    rows, seen = [], set()
    for item in items:
        row = {"ok": False}
        try:
            if not isinstance(item, dict):
                raise MediaError("下载清单每项必须为对象。")
            url = public_url(item.get("url", ""))
            referer = public_url(item["referer"]) if item.get("referer") else url
            target = (root / str(item.get("path") or "")).resolve()
            if (not target.is_relative_to(root) or target in (root, ledger)
                    or any(part in (".media_previews", ".contexts") for part in target.relative_to(root).parts)):
                raise MediaError("下载目标必须是本任务目录内的原图文件。")
            if target in seen:
                raise MediaError("同一清单包含重复目标，请为不同图片设置不同文件名。")
            seen.add(target)
            row.update(url=url, referer=referer, path=str(target))
            cached = known.get(str(target), {})
            reuse = (target.is_file() and cached.get("url") == url and cached.get("referer") == referer
                     and cached.get("sha256") == await asyncio.to_thread(sha256_file, target))
            if reuse:
                try:
                    await asyncio.to_thread(image_metadata, target)
                except MediaError:
                    reuse = False
            if not reuse:
                mime, final_url = await download_url(url, target, max_bytes=sys.maxsize, timeout=timeout,
                    headers={"Referer": referer, "Accept": "image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5"},
                    expected_kind="image")
                row.update(content_type=mime, final_url=final_url)
            else:
                row.update({key: cached[key] for key in ("content_type", "final_url") if key in cached})
            row.update(validated=True, sha256=await asyncio.to_thread(sha256_file, target))
            known[str(target)] = row
            row.update(await asyncio.to_thread(prepare_image_preview, target, root))
            row.update(ok=True, reused=bool(reuse), size=target.stat().st_size)
            known[str(target)] = row
        except (MediaError, OSError, ValueError, TypeError) as error:
            row["error"] = str(error)
        rows.append(row)
        # Checkpoint each completed item so interrupted batches resume without
        # redownloading validated originals. Retain earlier, unrelated items.
        combined = {**known, **{r["path"]: r for r in rows if r.get("path")}}
        _atomic(ledger, json.dumps({"files": list(combined.values())}, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"ok": all(row["ok"] for row in rows), "manifest": str(ledger), "files": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("download", "preview"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--directory", type=Path, required=True)
        cmd.add_argument("--manifest" if name == "download" else "--path", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "preview":
            original, root = args.path.resolve(strict=True), args.directory.resolve()
            if not original.is_relative_to(root):
                raise MediaError("预览原图必须位于任务目录内。")
            result = {"ok": True, **prepare_image_preview(original, root)}
        else:
            manifest = args.manifest.resolve(strict=True)
            if not manifest.is_relative_to(args.directory.resolve()):
                raise MediaError("下载清单必须位于任务目录内。")
            raw = manifest.read_bytes()
            items = json.loads(raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"))
            if not isinstance(items, list) or not items:
                raise MediaError("下载清单必须是非空 JSON 数组。")
            result = asyncio.run(download_images(items, args.directory))
    except (OSError, ValueError, TypeError) as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
