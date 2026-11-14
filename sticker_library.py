"""User-managed tagged stickers; selection uses tags, delivery uses real image bytes."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time

from PIL import Image


class StickerLibrary:
    def __init__(self, directory="data/stickers"):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS stickers(id TEXT PRIMARY KEY,path TEXT,tags TEXT,description TEXT,created REAL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "library.sqlite3", timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_file(self, path, tags, description=""):
        source = Path(path).resolve(strict=True)
        tags = list(dict.fromkeys(str(tag).strip()[:40] for tag in tags if str(tag).strip()))[:20]
        if not tags:
            raise ValueError("请至少填写一个表情包标签，例如 开心、疑惑、安慰。")
        if source.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("表情包超过 10 MB。")
        data = source.read_bytes()
        import io
        with Image.open(io.BytesIO(data)) as picture:
            extension = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}.get(picture.format)
            if not extension or picture.width * picture.height > 20_000_000:
                raise ValueError("只接收 PNG/JPEG/GIF/WebP 表情包，图像尺寸过大时请先缩小。")
            picture.verify()
        identity = hashlib.sha256(data).hexdigest()
        target = self.root / "assets" / (identity + extension)
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO stickers VALUES(?,?,?,?,?)",
                       (identity, str(target), json.dumps(tags, ensure_ascii=False), str(description)[:300], time.time()))
        return identity

    def search(self, query="", limit=6):
        query = str(query).casefold()
        words = set(re.findall(r"[\w]+", query))
        with self.connect() as db:
            rows = db.execute("SELECT * FROM stickers ORDER BY created DESC").fetchall()
        result = []
        for row in rows:
            tags = json.loads(row["tags"])
            score = sum(3 for tag in tags if tag.casefold() in query) + sum(
                1 for word in words if word in row["description"].casefold())
            if not query or score:
                result.append({"id": row["id"], "tags": tags, "description": row["description"], "score": score})
        return sorted(result, key=lambda item: -item["score"])[:max(1, min(50, int(limit)))]

    def remove(self, identity):
        with self.connect() as db:
            db.execute("DELETE FROM stickers WHERE id=?", (identity,))

    def copy_for_send(self, identity, directory):
        with self.connect() as db:
            row = db.execute("SELECT * FROM stickers WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("表情包不存在，请先搜索当前表情包库。")
        source = Path(row["path"]).resolve(strict=True)
        if not source.is_relative_to(self.root / "assets") or hashlib.sha256(source.read_bytes()).hexdigest() != identity:
            raise ValueError("表情包文件已变更，未发送。请重新导入。")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / source.name
        shutil.copyfile(source, target)
        return target.resolve(), json.loads(row["tags"])
