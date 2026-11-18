"""Thin, lazily loaded bilibili-api-python 17.4.2 adapter.

This community provider is optional; it is isolated from the agent and GUI.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
from bilibili_credentials import SECRET_FIELDS, load_credentials


SDK_VERSION = "17.4.2"


class BilibiliAdapter:
    def __init__(self, credentials_file):
        self.credentials_file = Path(credentials_file)
        self.listener = None

    def credentials(self):
        data = load_credentials(self.credentials_file)
        return {key: os.environ.get("BILIBILI_" + key.upper()) or data.get(key) or None for key in SECRET_FIELDS}

    def status(self):
        try:
            version = importlib.metadata.version("bilibili-api-python")
        except importlib.metadata.PackageNotFoundError:
            version = ""
        secrets = self.credentials()
        return {"sdk_version": version, "sdk_expected": SDK_VERSION, "sdk_ready": version == SDK_VERSION,
                "credentials_present": bool(secrets.get("sessdata") and secrets.get("bili_jct")),
                "provider": "bilibili-api-python",
                "credentials_source": "environment" if os.environ.get("BILIBILI_SESSDATA") else "local_file" if self.credentials_file.is_file() else "missing"}

    def credential(self, *, required=True):
        if not self.status()["sdk_ready"]:
            raise RuntimeError("请安装 bilibili-api-python==" + SDK_VERSION + "；使用独立的运营依赖清单。")
        from bilibili_api import Credential
        data = self.credentials()
        if required and not (data.get("sessdata") and data.get("bili_jct")):
            raise ValueError("尚未配置 B 站本机凭据（SESSDATA / bili_jct）。")
        return Credential(**data)

    async def account(self):
        credential = self.credential()
        from bilibili_api import user
        info = await user.get_self_info(credential)
        return {key: info[key] for key in ("mid", "name", "uname", "face", "level") if key in info}

    @staticmethod
    def video_summary(item):
        import html
        import re
        owner = item.get("owner") or {}
        return {"bvid": str(item.get("bvid", "")), "aid": int(item.get("aid") or item.get("id") or 0),
                "title": html.unescape(re.sub(r"<[^>]+>", "", str(item.get("title", ""))))[:300],
                "description": str(item.get("desc", item.get("description", "")))[:4000],
                "author": str(owner.get("name", item.get("author", "")))[:100],
                "url": "https://www.bilibili.com/video/" + str(item.get("bvid") or "av" + str(item.get("aid", 0)))}

    async def feed(self, limit=10):
        from bilibili_api import homepage
        data = await homepage.get_videos(credential=self.credential())
        return {"videos": [self.video_summary(row) for row in data.get("item", [])[:limit]], "source": "recommendations"}

    async def search(self, query, page=1, limit=10):
        from bilibili_api import search
        self.credential()
        data = await search.search_by_type(query, search_type=search.SearchObjectType.VIDEO,
                                          page=page, page_size=min(20, limit))
        return {"videos": [self.video_summary(row) for row in data.get("result", [])[:limit]],
                "page": page, "total": data.get("numResults", 0)}

    async def video_info(self, bvid):
        from bilibili_api import video
        item = video.Video(bvid=bvid, credential=self.credential())
        data = await item.get_info()
        result = {**self.video_summary(data), "cid": data.get("cid"), "duration": data.get("duration"),
                  "statistics": {key: data.get("stat", {}).get(key, 0) for key in ("view", "like", "reply", "favorite")},
                  "subtitle_text": "", "subtitle_status": "unavailable", "content_basis": "metadata"}
        # Only fetch the subtitle URL supplied by the SDK, without account cookies.
        try:
            import aiohttp
            from urllib.parse import urlsplit
            subtitles = await item.get_subtitle(cid=data.get("cid")) or {}
            entries = subtitles.get("subtitles", [])
            if entries:
                url = str(entries[0].get("subtitle_url", ""))
                if url.startswith("//"):
                    url = "https:" + url
                parsed = urlsplit(url)
                host = (parsed.hostname or "").lower()
                allowed = any(host == domain or host.endswith("." + domain) for domain in ("hdslb.com", "bilibili.com"))
                if parsed.scheme == "https" and allowed and not parsed.username and not parsed.password and parsed.port in (None, 443):
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False) as session:
                        async with session.get(url, allow_redirects=False) as response:
                            response.raise_for_status()
                            raw = await response.content.read(1048577)
                            if len(raw) <= 1048576:
                                body = json.loads(raw).get("body", [])
                                result["subtitle_text"] = "\n".join(str(row.get("content", "")) for row in body)[:12000]
                                result["subtitle_status"] = "available"
                                result["content_basis"] = "metadata_and_subtitles"
        except Exception:
            pass  # Metadata remains usable; never pretend to have watched missing content.
        return result

    async def comments(self, oid, resource_type="video", page=1, limit=10, root=0):
        from bilibili_api import comment
        kind = comment.CommentResourceType.VIDEO if resource_type == "video" else comment.CommentResourceType.DYNAMIC
        credential = self.credential()
        if root:
            data = await comment.Comment(oid, kind, root, credential=credential).get_sub_comments(page_index=page, page_size=limit)
        else:
            data = await comment.get_comments(oid, kind, page_index=page, credential=credential)
        def row(item):
            member = item.get("member") or {}
            return {"rpid": int(item.get("rpid") or 0), "root": int(item.get("root") or 0),
                    "parent": int(item.get("parent") or 0), "uid": str(member.get("mid", "")),
                    "name": str(member.get("uname", ""))[:100], "text": str((item.get("content") or {}).get("message", ""))[:3000],
                    "likes": item.get("like", 0), "replies": [row(child) for child in (item.get("replies") or [])[:3]]}
        return {"oid": oid, "resource_type": resource_type, "root": root, "page": page,
                "comments": [row(item) for item in (data.get("replies") or [])[:limit]]}

    async def notifications(self, limit=10):
        from bilibili_api import session
        credential = self.credential()
        replies = await session.get_replies(credential)
        mentions = await session.get_at(credential)
        def summary(row):
            item = row.get("item") or {}
            user = row.get("user") or {}
            return {"id": str(row.get("id", "")), "uid": str(user.get("mid", "")),
                    "name": str(user.get("nickname", ""))[:100], "text": str(item.get("source_content", item.get("title", "")))[:3000],
                    "oid": int(item.get("subject_id") or 0), "root": int(item.get("root_id") or 0),
                    "parent": int(item.get("target_id") or item.get("source_id") or 0),
                    "resource_type": "video" if item.get("business_id") == 1 else "dynamic" if item.get("business_id") == 17 else "unsupported",
                    "url": str(item.get("uri", ""))[:1000]}
        return {"replies": [summary(row) for row in replies.get("items", [])[:limit]],
                "mentions": [summary(row) for row in mentions.get("items", [])[:limit]]}

    async def send_comment(self, oid, text, *, resource_type="video", root=0, parent=0):
        from bilibili_api import comment
        kind = comment.CommentResourceType.VIDEO if resource_type == "video" else comment.CommentResourceType.DYNAMIC
        result = await comment.send_comment(text, oid, kind, root=root or None, parent=parent or None, credential=self.credential())
        rpid = (result.get("reply") or {}).get("rpid") or result.get("rpid")
        if not rpid:
            raise RuntimeError("平台未返回评论 ID。")
        return {"oid": oid, "rpid": str(rpid), "resource_type": resource_type, "root": root, "parent": parent}

    async def like_video(self, bvid, active=True):
        from bilibili_api import video
        await video.Video(bvid=bvid, credential=self.credential()).like(status=active)
        return {"bvid": bvid, "liked": active}

    async def update_signature(self, text):
        from bilibili_api import user
        credential = self.credential()
        info = await user.get_self_info(credential)
        await user.edit_self_info(info.get("birthday", ""), info.get("sex", "保密"),
                                  info.get("name", info.get("uname", "")), text, credential)
        return {"signature": text, "mid": info.get("mid")}

    def room(self, room_id):
        credential = self.credential()
        from bilibili_api import live
        if not room_id:
            raise ValueError("请先填写直播间号。")
        return live.LiveRoom(room_id, credential=credential)

    async def room_info(self, room_id):
        info = await self.room(room_id).get_room_info()
        room = info.get("room_info", info)
        return {key: room[key] for key in ("room_id", "uid", "title", "live_status", "area_id", "parent_area_id", "area_name", "live_start_time") if key in room}

    async def start_live(self, room_id, area_id):
        if not area_id:
            raise ValueError("开播需要填写 B 站分区 ID。")
        # The response may contain an RTMP secret; keep it outside all receipts.
        result = await self.room(room_id).start(area_id)
        return {"platform": "bilibili", "room_id": room_id, "changed": result.get("change"), "live_key_present": bool(result.get("rtmp"))}

    async def stop_live(self, room_id):
        result = await self.room(room_id).stop()
        return {"platform": "bilibili", "room_id": room_id, "changed": result.get("change")}

    async def send_danmaku(self, room_id, text):
        from bilibili_api import Danmaku
        await self.room(room_id).send_danmaku(Danmaku(text))
        return {"platform": "bilibili", "room_id": room_id, "text": text}

    async def publish_dynamic(self, body):
        from bilibili_api import dynamic
        info = dynamic.BuildDynamic().add_text(body["text"])
        result = await dynamic.send_dynamic(info, self.credential())
        dynamic_id = str(result.get("dynamic_id", result.get("id_str", result.get("id", ""))))
        if not dynamic_id:
            raise RuntimeError("平台未返回动态 ID，发布结果需人工核对。")
        return {"dynamic_id": dynamic_id, "url": "https://t.bilibili.com/" + dynamic_id}

    async def publish_video(self, body):
        from bilibili_api import video_uploader
        page = video_uploader.VideoUploaderPage(body["video_path"], title=body["title"], description=body["text"])
        original = body.get("copyright", 1) == 1
        meta = video_uploader.VideoMeta(tid=body["category_id"], title=body["title"], desc=body["text"],
                                        cover=body["cover_path"], tags=body["tags"], original=original,
                                        source=None if original else body.get("source", ""))
        uploader = video_uploader.VideoUploader([page], meta, self.credential())
        result = await uploader.start()
        bvid, aid = result.get("bvid"), result.get("aid")
        if not bvid and not aid:
            raise RuntimeError("平台未返回稿件 ID，发布结果需人工核对。")
        return {"bvid": bvid, "aid": aid, "url": "https://www.bilibili.com/video/" + str(bvid or "av" + str(aid))}

    async def listen(self, room_id, on_event):
        from bilibili_api import live
        self.listener = live.LiveDanmaku(room_id, credential=self.credential(), max_retry=5)
        for event_name in ("DANMU_MSG", "SEND_GIFT", "GUARD_BUY", "SUPER_CHAT_MESSAGE", "INTERACT_WORD"):
            self.listener.on(event_name)(on_event)
        try:
            await self.listener.connect()
        finally:
            self.listener = None

    async def disconnect(self):
        if self.listener:
            try:
                await self.listener.disconnect()
            except Exception:
                pass  # Disconnected/connecting SDK clients have no active socket to close.
