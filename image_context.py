"""Persist confirmed image observations independently of disposable AI branches.

The store never opens an image, follows a URL, or calls a model. Callers pass
only recognition results from their own conversation and choose when to inject
the bounded data context into the ordinary chat session.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid


def _scope(value):
    value = str(value or "")
    parts = value.split(":")
    if len(parts) != 4 or not parts[0] or not parts[1] or parts[2] not in ("group", "private", "c2c") or not parts[3]:
        raise ValueError("Image scope must be transport:account:group/private/c2c:conversation")
    return value


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _path(value):
    value = _text(value)
    if not value or re.match(r"(?:https?|data|base64)[:/]+", value, re.I):
        return ""
    return value


def _terms(query):
    terms = set(re.findall(r"[a-z0-9_]{2,}", query.casefold()))
    for part in re.findall(r"[\u3400-\u9fff]{2,}", query):
        terms.add(part)
        for size in (2, 3):
            terms.update(part[i:i + size] for i in range(len(part) - size + 1))
    return terms - {"这个", "那个", "刚才", "一下", "上面", "里面", "什么", "多少", "图片", "图中", "这张", "那张", "看看"}


def _score(query, fact):
    haystack = (fact.visual_description + "\n" + fact.uncertainties).casefold()
    return sum(min(len(term), 8) for term in _terms(query) if term in haystack)


def _excerpt(text, budget, query):
    if len(text) <= budget:
        return text
    if budget < 32:
        return text[:budget]
    tokens = sorted(_terms(query), key=lambda value: (-len(value), value))
    relevant = []
    lowered = text.casefold()
    for token in tokens:
        position = lowered.find(token.casefold())
        if position >= 0 and not any(abs(position - old) < 150 for old in relevant):
            relevant.append(position)
        if len(relevant) >= 3:
            break
    if not relevant:
        return text[:budget - 1] + "…"
    # Retain some overall context and the queried detail even if it occurs near
    # the end of a long OCR/description result. The complete artifact is intact.
    blocks = [text[:min(250, budget // 4)]]
    per_match = max(1, (budget - len(blocks[0]) - 12) // len(relevant))
    for position in relevant:
        start = max(0, position - min(150, per_match // 3))
        blocks.append(text[start:start + per_match])
    return "\n…\n".join(blocks)[:budget]


@dataclass(frozen=True)
class ImageFact:
    scope: str
    original_message_id: str
    image_index: int
    visual_description: str
    uncertainties: str
    view_evidence: dict
    local_path: str
    sender_id: str
    sender_name: str
    timestamp: float
    query: str
    artifact_path: str
    updated_at: float
    media_type: str = "image"
    emotion: str = ""
    gesture: str = ""
    meaning: str = ""

    @property
    def message_id(self):
        return self.original_message_id

    @property
    def summary(self):
        return self.visual_description.replace("\n", " ")[:240]

    def as_image_analysis(self):
        """Restore only confirmed data for quote reuse, without new vision work."""
        return {"vision_verified": True, "visual_description": self.visual_description,
            "uncertainties": self.uncertainties, "local_path": self.local_path,
            "original_message_id": self.original_message_id, "origin_message_id": self.original_message_id,
            "origin_scope": self.scope, "image_index": self.image_index,
            "source_file": self.local_path, "source_url": "",
            "image_context_path": self.artifact_path,
            "is_sticker": self.media_type == "sticker", "media_type": self.media_type,
            "emotion": self.emotion, "gesture": self.gesture,
            "visual_verification": {"description": self.visual_description,
                "uncertainties": self.uncertainties, "view_evidence": dict(self.view_evidence)},
            "meaning_understood": True, "is_ambiguous": False,
            "semantic_meaning": self.meaning or self.visual_description}

    def to_analysis(self):
        return self.as_image_analysis()


class ImageContextStore:
    def __init__(self, path="data/image_context.sqlite3", artifacts_dir=None):
        self.path = Path(path).resolve()
        self.artifacts_dir = Path(artifacts_dir).resolve() if artifacts_dir else self.path.parent / "image_facts"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS image_context (
                    scope TEXT NOT NULL, original_message_id TEXT NOT NULL,
                    image_index INTEGER NOT NULL, payload TEXT NOT NULL,
                    timestamp REAL NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(scope, original_message_id, image_index));
                CREATE INDEX IF NOT EXISTS image_context_recent
                    ON image_context(scope, timestamp DESC, updated_at DESC);
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save_images(self, scope, original_message_id, images, *, sender_id="", sender_name="", timestamp=0, query=""):
        scope = _scope(scope)
        message_id = str(original_message_id or "")
        if not message_id:
            raise ValueError("The original image message ID is required")
        saved = []
        now = time.time()
        timestamp = float(timestamp or now)
        namespace = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]
        message_hash = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:24]
        directory = self.artifacts_dir / namespace / message_hash
        with self._connect() as connection:
            for position, image in enumerate(images):
                if not isinstance(image, dict) or image.get("vision_verified") is not True:
                    continue
                verification = image.get("visual_verification")
                verification = verification if isinstance(verification, dict) else {}
                description = _text(image.get("visual_description")) or _text(verification.get("description"))
                if not description:
                    continue
                index = image.get("image_index", position)
                if type(index) is not int or index < 0:
                    raise ValueError("Image index must be a nonnegative integer")
                uncertainties = _text(image.get("uncertainties")) or _text(verification.get("uncertainties"))
                evidence = verification.get("view_evidence") or image.get("view_evidence")
                evidence = evidence if isinstance(evidence, dict) else {}
                # Keep actual view evidence; source URLs/tool prose/queries are
                # never promoted into the visual observation.
                allowed_evidence = {key: _path(evidence.get(key)) for key in ("path", "archive") if _path(evidence.get(key))}
                sha256 = _text(evidence.get("sha256"))
                if re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
                    allowed_evidence["sha256"] = sha256
                artifact = directory / (str(index) + "_" + uuid.uuid4().hex + ".json")
                payload = {"scope": scope, "original_message_id": message_id,
                    "image_index": index, "visual_description": description,
                    "uncertainties": uncertainties, "view_evidence": allowed_evidence,
                    "local_path": _path(image.get("local_path")), "sender_id": str(sender_id or ""),
                    "sender_name": str(sender_name or ""), "timestamp": timestamp,
                    "query": str(query or ""), "artifact_path": str(artifact), "updated_at": now,
                    "media_type": "sticker" if image.get("is_sticker") else "image",
                    "emotion": _text(image.get("emotion")), "gesture": _text(image.get("gesture")),
                    "meaning": _text(image.get("semantic_meaning"))}
                directory.mkdir(parents=True, exist_ok=True)
                temporary = artifact.with_suffix(".tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, artifact)
                connection.execute("INSERT OR REPLACE INTO image_context VALUES(?,?,?,?,?,?)",
                    (scope, message_id, index, json.dumps(payload, ensure_ascii=False), timestamp, now))
                saved.append(ImageFact(**payload))
        return saved

    async def save_images_async(self, *args, **kwargs):
        return await asyncio.to_thread(self.save_images, *args, **kwargs)

    def get(self, scope, original_message_id, image_index=None):
        scope = _scope(scope)
        sql = "SELECT payload FROM image_context WHERE scope=? AND original_message_id=?"
        values = [scope, str(original_message_id)]
        if image_index is not None:
            sql += " AND image_index=?"
            values.append(image_index)
        with self._connect() as connection:
            rows = connection.execute(sql + " ORDER BY image_index", values)
            return [ImageFact(**json.loads(row[0])) for row in rows]

    def recent(self, scope, limit=6):
        scope = _scope(scope)
        limit = min(32, max(0, int(limit)))
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM image_context WHERE scope=? ORDER BY timestamp DESC, updated_at DESC, image_index LIMIT ?",
                                      (scope, limit))
            return [ImageFact(**json.loads(row[0])) for row in rows]

    def build_context(self, scope, *, message_id="", query="", limit=4, max_chars=14000, known_versions=None,
                      excluded_versions=None, included_versions=None):
        scope = _scope(scope)
        limit = min(8, max(0, int(limit)))
        max_chars = min(14000, max(0, int(max_chars)))
        if limit == 0 or max_chars < 400 or not (message_id or _text(query)):
            return ""
        if message_id:
            facts = self.get(scope, message_id)[:limit]
        else:
            # A shared word (e.g. a name/game) in ordinary conversation is not
            # a request to replay old OCR. Explicit quotes use message_id.
            if not re.search(r"图|照片|截图|画面|表情|刚才.*(?:分数|成绩)|(?:上面|之前).*(?:分数|成绩)", query):
                return ""
            candidates = self.recent(scope, 24)
            relevant = [(fact, _score(query, fact)) for fact in candidates]
            facts = [fact for fact, score in sorted(relevant, key=lambda item: (item[1], item[0].timestamp), reverse=True) if score > 0][:limit]
            if not facts and re.search(r"这张|那张|刚才.*(?:图|照片|截图)|(?:图|照片|截图).*(?:里|中|上|细节)|(?:看|瞧|解释|分析).{0,4}(?:图|照片|截图)|上面.*(?:数|分|时长)|再.*(?:看|解释).*图", query):
                facts = [fact for fact in candidates if 0 <= time.time() - fact.timestamp <= 1800][:limit]
        facts = [fact for fact in facts
                 if (excluded_versions or {}).get(f"{fact.message_id}:{fact.image_index}") != fact.updated_at]
        if not facts:
            return ""
        instruction = "【已核验图片事实】\n"
        rows = []
        available = max_chars - len(instruction) - 4
        for offset, fact in enumerate(facts):
            row = {"original_message_id": fact.original_message_id, "image_index": fact.image_index,
                   "sender_id": fact.sender_id, "sender_name": fact.sender_name, "timestamp": fact.timestamp,
                   "description": "", "uncertainties": "", "full_result_file": fact.artifact_path}
            row.update({key: value for key, value in {"media_type": fact.media_type, "emotion": fact.emotion,
                "gesture": fact.gesture}.items() if value})
            if fact.meaning and fact.meaning.strip() != fact.visual_description.strip():
                row["meaning"] = _excerpt(fact.meaning, 280, query)
            if (known_versions or {}).get(f"{fact.message_id}:{fact.image_index}") == fact.updated_at:
                for key in ("emotion", "gesture", "meaning"):
                    row.pop(key, None)
                row.update(already_in_main=True)
                rows.append(row)
                available -= len(json.dumps(row, ensure_ascii=False)) + 2
                continue
            overhead = len(json.dumps(row, ensure_ascii=False)) + 2
            allocation = max(0, available // (len(facts) - offset) - overhead)
            if allocation < 80:
                allocation = max(0, available - overhead)
                if allocation < 80:
                    break
            uncertainty_budget = min(len(fact.uncertainties), max(80, allocation // 5))
            row["uncertainties"] = _excerpt(fact.uncertainties, uncertainty_budget, query)
            row["description"] = _excerpt(fact.visual_description, allocation - len(row["uncertainties"]), query)
            rows.append(row)
            available -= len(json.dumps(row, ensure_ascii=False)) + 2
        if not rows:
            return ""
        # JSON escaping can add characters (newlines/quotes); shrink content,
        # keeping the original artifact path and message binding intact.
        while rows:
            result = instruction + json.dumps(rows, ensure_ascii=False)
            if len(result) <= max_chars:
                if included_versions is not None:
                    versions = {(fact.message_id, fact.image_index): fact.updated_at for fact in facts}
                    included_versions.update({(row["original_message_id"], row["image_index"]):
                        versions[(row["original_message_id"], row["image_index"])] for row in rows})
                return result
            last = rows[-1]
            over = len(result) - max_chars
            if len(last["description"]) > over + 40:
                last["description"] = last["description"][:-over - 4] + "…"
            else:
                rows.pop()
        return ""

    async def build_context_async(self, *args, **kwargs):
        return await asyncio.to_thread(self.build_context, *args, **kwargs)

    def clear_scope(self, scope):
        scope = _scope(scope)
        with self._connect() as connection:
            connection.execute("DELETE FROM image_context WHERE scope=?", (scope,))
        # Durable per-image artifacts intentionally remain for inspection; the
        # cleared conversation can no longer retrieve them through this index.

    def clear_namespace(self, namespace):
        parts = str(namespace).split(":")
        if len(parts) != 2 or not all(parts):
            raise ValueError("Image namespace must be transport:account")
        prefix = namespace + ":"
        with self._connect() as connection:
            connection.execute("DELETE FROM image_context WHERE substr(scope,1,?)=?", (len(prefix), prefix))
