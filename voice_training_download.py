"""Pinned public model assets with resumable ranges and LFS digest validation."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import shutil
import time
from urllib.parse import quote


def download_model(repo, target, names, *, emit=lambda *a, **k: None):
    import requests
    response = requests.get("https://huggingface.co/api/models/" + repo, params={"blobs": "true"}, timeout=45)
    response.raise_for_status()
    info = response.json()
    revision = info["sha"]
    indexed = {r["rfilename"]:r for r in info["siblings"]}
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    records = {}
    def digest(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    for name in names:
        row = indexed.get(name)
        if row is None:
            if name in ("preprocessor_config.json", "vocabulary.json", "vocabulary.txt"):
                continue
            raise ValueError("官方模型仓库缺少资产：" + name)
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("模型仓库包含非法路径。")
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = row.get("lfs", {}).get("sha256")
        size = row.get("size") or row.get("lfs", {}).get("size")
        if not (path.is_file() and expected and path.stat().st_size == size and digest(path) == expected):
            emit("downloading", file=name, bytes=size)
            url = "https://huggingface.co/" + repo + "/resolve/" + revision + "/" + quote(name)
            assembled = path.with_name(path.name + ".assembled")
            if size and size > 16*1024*1024:
                parts = path.with_name(path.name + ".chunks")
                parts.mkdir(exist_ok=True)
                block = 8*1024*1024
                bounds = [(i,min(i+block,size)-1) for i in range(0,size,block)]
                def fetch(pair):
                    start,end = pair
                    final = parts / str(start)
                    if final.is_file() and final.stat().st_size == end-start+1:
                        return final
                    temporary = final.with_suffix(".part")
                    for attempt in range(6):
                        have = temporary.stat().st_size if temporary.exists() else 0
                        if have == end-start+1:
                            temporary.replace(final)
                            return final
                        try:
                            offset = start+have
                            with requests.get(url, params={"qqbot_range":f"{offset}-{end}"}, headers={"Range":f"bytes={offset}-{end}"}, stream=True, timeout=(20,30)) as response:
                                response.raise_for_status()
                                if response.status_code != 206 or response.headers.get("Content-Range") != f"bytes {offset}-{end}/{size}":
                                    raise ValueError("模型下载服务器返回了错误的范围。")
                                with temporary.open("ab") as output:
                                    for data in response.iter_content(1024*1024):
                                        have += len(data)
                                        if have > end-start+1:
                                            raise ValueError("模型下载数据超过请求范围。")
                                        output.write(data)
                            if have != end-start+1:
                                raise ValueError("模型下载数据块不完整。")
                            temporary.replace(final)
                            emit("download-progress", file=name, completed=sum(p.stat().st_size for p in parts.iterdir() if p.is_file() and p.suffix != ".part"), total=size)
                            return final
                        except (requests.RequestException, ValueError):
                            if attempt == 5:
                                raise
                            time.sleep(attempt+1)
                with ThreadPoolExecutor(max_workers=8) as pool:
                    pieces = list(pool.map(fetch,bounds))
                with assembled.open("wb") as output:
                    for piece in pieces:
                        with piece.open("rb") as source:
                            shutil.copyfileobj(source,output,1024*1024)
            else:
                with requests.get(url, timeout=(20,90),stream=True) as response:
                    response.raise_for_status()
                    with assembled.open("wb") as output:
                        for data in response.iter_content(1024*1024):
                            output.write(data)
            actual = digest(assembled)
            if size and assembled.stat().st_size != size or expected and actual != expected:
                raise ValueError("官方资产大小或 SHA256 校验失败："+name)
            assembled.replace(path)
        records[name] = {"path":str(path),"sha256":digest(path),"size":path.stat().st_size}
    return {"repository":repo,"revision":revision,"files":records}
