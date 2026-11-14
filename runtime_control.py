"""A local persisted control generation, polled by both bot processes and GUI."""
from __future__ import annotations

import asyncio
import json
import hashlib
import os
from pathlib import Path
import time
import uuid


async def finish_thread(function, *args, **kwargs):
    """Join an already started IO operation before releasing its resources."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


CONTROL_PATH = Path("data/runtime_control.json")


def request_clear_all() -> str:
    CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    temporary = CONTROL_PATH.with_suffix("." + request_id + ".tmp")
    temporary.write_text(json.dumps({"id": request_id, "action": "clear_all_memory", "time": time.time()}), encoding="utf-8")
    os.replace(temporary, CONTROL_PATH)
    return request_id


def request_stop() -> str:
    CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    temporary = CONTROL_PATH.with_suffix("." + request_id + ".tmp")
    temporary.write_text(json.dumps({"id": request_id, "action": "stop", "time": time.time()}), encoding="utf-8")
    os.replace(temporary, CONTROL_PATH)
    return request_id


def session_generation(ai, key) -> int:
    generation = getattr(ai.get_or_create_session(key), "generation", 0)
    return generation if isinstance(generation, int) else 0


def generation_is_current(ai, key, generation) -> bool:
    sessions = getattr(ai, "_sessions", None)
    if isinstance(sessions, dict):
        session = sessions.get(key)
        return session is not None and session.generation == generation
    return session_generation(ai, key) == generation


class RuntimeControl:
    def __init__(self, namespace="default"):
        self.namespace = namespace
        self.created_at = time.time()
        digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:20]
        self.ack_path = CONTROL_PATH.parent / ("control_applied_" + digest + ".json")
        try:
            self.seen = json.loads(self.ack_path.read_text(encoding="utf-8")).get("id", "")
        except (OSError, ValueError):
            self.seen = ""
        current_msg = self._read()
        if current_msg.get("action") == "stop" and current_msg.get("id"):
            msg_time = current_msg.get("time", 0)
            if msg_time <= self.created_at:
                self.seen = current_msg.get("id")
                try:
                    CONTROL_PATH.unlink(missing_ok=True)
                except Exception:
                    pass

    @staticmethod
    def _read():
        try:
            return json.loads(CONTROL_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def poll(self, client):
        message = self._read()
        request_id = message.get("id", "")
        if not request_id or request_id == self.seen:
            return
        if message.get("action") == "clear_all_memory":
            client.ai.reset_all_sessions()
            client.context_buffer.clear()
            client.memory_mgr.clear_all_memories()
            client._recent_messages_cache.clear()
            client._last_chat_replies.clear()
            clear_images = getattr(client, "_clear_image_context", None)
            if clear_images:
                clear_images()
            clear_emotion = getattr(client, "_get_emotion_history", None)
            if clear_emotion:
                clear_emotion().clear(self.namespace)
            client._cancel_all_media()
            self.ack_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.ack_path.with_suffix("." + uuid.uuid4().hex + ".tmp")
            temporary.write_text(json.dumps({"id": request_id}), encoding="utf-8")
            os.replace(temporary, self.ack_path)
            self.seen = request_id
            print("[CONTROL_ACK] " + json.dumps({"id": request_id, "action": message["action"], "status": "applied"}), flush=True)
        elif message.get("action") == "stop":
            msg_time = message.get("time", 0)
            if msg_time < self.created_at:
                self.seen = request_id
                try:
                    CONTROL_PATH.unlink(missing_ok=True)
                except Exception:
                    pass
                return
            self.ack_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.ack_path.with_suffix("." + uuid.uuid4().hex + ".tmp")
            temporary.write_text(json.dumps({"id": request_id}), encoding="utf-8")
            os.replace(temporary, self.ack_path)
            self.seen = request_id
            print("[CONTROL_ACK] " + json.dumps({"id": request_id, "action": message["action"], "status": "stopping"}), flush=True)
            if hasattr(client, "graceful_stop"):
                try:
                    loop = asyncio.get_running_loop()
                    stop_task = loop.create_task(client.graceful_stop())
                    client._graceful_stop_task = stop_task
                except RuntimeError:
                    asyncio.run(client.graceful_stop())

    async def watch(self, client):
        while True:
            self.poll(client)
            # Detect persona edits while a generation is in progress as well.
            client.sync_system_prompt()
            if hasattr(client, "_tick_memory_archive"):
                client._tick_memory_archive()
            retry_archives = getattr(client.ai, "tick_archives", None)
            if retry_archives is not None:
                retry_archives()
            if hasattr(client, "_proactive_chat"):
                client._proactive_chat.tick()
            await asyncio.sleep(0.5)
