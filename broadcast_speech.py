"""Independent live PCM delivery with bounded buffering and sample-clock pacing."""
from __future__ import annotations

import asyncio
from array import array
import base64
import re
import sys
import time
import weakref

from broadcast_tts import BroadcastVoiceService
from vtuber_host import ensure_server, execute

_LOOPS = weakref.WeakKeyDictionary()
_SENTINEL = object()
_AHEAD_SECONDS = 3.5
_EMOTIONS = {"neutral", "happy", "sad", "angry", "surprised", "shy", "thinking", "excited"}


def avatar_emotion(label, fallback="neutral"):
    value = str(label).lower()
    if value in _EMOTIONS:
        return value
    for emotion, words in (
        ("excited", ("excit", "兴奋", "興奮", "激动")),
        ("happy", ("happ", "joy", "开心", "快樂", "快乐", "喜", "嬉")),
        ("sad", ("sad", "悲", "难过", "寂", "泣")),
        ("angry", ("ang", "怒", "生气")),
        ("surprised", ("surpris", "惊", "驚")),
        ("shy", ("shy", "羞", "照れ")),
        ("thinking", ("think", "思", "疑", "困惑")),
    ):
        if any(word in value for word in words):
            return emotion
    return fallback


class _Clock:
    def __init__(self, *, now=None, sleep=None):
        self.lock = asyncio.Lock()
        self.until = 0.0
        self.now = now or time.monotonic
        self.sleep = sleep or asyncio.sleep

    async def pace(self, seconds, limit=_AHEAD_SECONDS):
        delay = max(0.0, self.until + seconds - self.now() - limit)
        if delay:
            await self.sleep(delay)
        return delay

    def submitted(self, seconds):
        now = self.now()
        self.until = max(now + .025, self.until) + seconds
        return max(0.0, self.until - now)


def _clock(config):
    clocks = _LOOPS.setdefault(asyncio.get_running_loop(), {})
    return clocks.setdefault((config.host, config.port), _Clock())


async def _execute(action, payload, config):
    # Drain in-flight HTTP before stopping, so late PCM cannot restart playback.
    task = asyncio.create_task(asyncio.to_thread(execute, action, payload, config, ensure=False))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


class _PCMAssembler:
    """Preserve pauses and emotion crossfades with at most one short PCM tail."""
    def __init__(self, config):
        self.config = config
        self.index, self.format = -1, None
        self.tail, self.tail_info = b"", None
        self.overlap, self.pending, self.overlap_info = b"", b"", None

    def blocks(self, data, info):
        size = max(1, info["sample_rate"] * self.config.stream_chunk_ms // 1000) * info["channels"] * 2
        for offset in range(0, len(data), size):
            yield data[offset:offset + size], info

    def blend(self, previous_data, data, channels):
        count = min(len(previous_data), len(data)) // (channels * 2)
        previous, current = array("h"), array("h")
        previous.frombytes(previous_data)
        current.frombytes(data[:count * channels * 2])
        if sys.byteorder != "little":
            previous.byteswap()
            current.byteswap()
        for offset in range(count * channels):
            alpha = (offset // channels + 1) / (count + 1)
            previous[-count * channels + offset] = round(previous[-count * channels + offset] * (1 - alpha) + current[offset] * alpha)
        if sys.byteorder != "little":
            previous.byteswap()
        return previous.tobytes(), data[count * channels * 2:]

    def finish_overlap(self):
        if self.overlap:
            data, _ = self.blend(self.overlap, self.pending, self.overlap_info["channels"])
            yield from self.blocks(data, self.overlap_info)
            self.overlap, self.pending = b"", b""

    def feed(self, event):
        if event.get("type") != "audio" or event.get("format") != "pcm16":
            raise ValueError("直播只接收带格式信息的 PCM16 音频。")
        rate, channels, index = event["sample_rate"], event["channels"], event["index"]
        if type(rate) is not int or not 8000 <= rate <= 96000 or type(channels) is not int or channels not in (1, 2) or type(index) is not int:
            raise ValueError("直播 PCM 格式无效。")
        data = base64.b64decode(event["pcm_base64"], validate=True)
        if not data or len(data) % (channels * 2):
            raise ValueError("直播 PCM 为空或包含不完整采样。")
        current_format = (rate, channels)
        if index != self.index:
            if index != self.index + 1:
                raise ValueError("直播语音片段缺失或顺序无效。")
            yield from self.finish_overlap()
            pause = int(event.get("pause_before_ms", 0))
            if not 0 <= pause <= 1000:
                raise ValueError("直播片段停顿无效。")
            if self.tail and (pause or current_format != self.format):
                yield from self.blocks(self.tail, self.tail_info)
                self.tail = b""
            if pause:
                yield from self.blocks(b"\0\0" * (rate * pause // 1000) * channels, event)
            if self.tail:
                self.overlap, self.overlap_info = self.tail, event
                self.tail = b""
            self.index, self.format = index, current_format
        elif current_format != self.format:
            raise ValueError("直播同一片段的 PCM 格式发生变化。")
        if self.overlap:
            self.pending += data
            if len(self.pending) < len(self.overlap):
                return
            mixed, data = self.blend(self.overlap, self.pending, channels)
            yield from self.blocks(mixed, event)
            self.overlap, self.pending = b"", b""
        data = self.tail + data
        hold = min(rate * self.config.crossfade_ms // 1000 * channels * 2, len(data))
        if len(data) > hold:
            yield from self.blocks(data[:-hold] if hold else data, event)
        self.tail, self.tail_info = (data[-hold:] if hold else b""), event

    def end(self):
        yield from self.finish_overlap()
        if self.tail:
            yield from self.blocks(self.tail, self.tail_info)
            self.tail = b""


async def speak(app_config, params):
    voice, avatar = app_config.broadcast_voice, app_config.vtuber
    if not voice.enabled:
        raise ValueError("请先在虚拟主播页面启用直播专用语音。")
    text, emotion = str(params.get("text", "")), str(params.get("emotion", ""))
    if not text.strip() or not emotion:
        raise ValueError("直播语音需要台词和默认情绪标签。")
    default = params.get("avatar_emotion") or avatar_emotion(emotion)
    if default not in _EMOTIONS:
        raise ValueError("直播表情无效。")
    from voice_text import strip_controls
    caption = strip_controls(text)
    clock = _clock(avatar)
    async with clock.lock:
        started = time.perf_counter()
        await asyncio.to_thread(ensure_server, avatar)
        first, blocks, ahead, queued, waiting, duration, byte_count, last = None, 0, 0.0, 0, 0.0, 0.0, 0, {}
        queue = asyncio.Queue(maxsize=voice.queue_blocks)
        assembler = _PCMAssembler(voice)

        async def put(data, info):
            nonlocal queued
            await queue.put((data, info))
            queued = max(queued, queue.qsize())

        async def audio_sink(event):
            for data, info in assembler.feed(event):
                await put(data, info)

        async def synthesize():
            output = await BroadcastVoiceService(voice).synthesize(text, emotion,
                spoken_text=params.get("spoken_text", ""), audio_sink=audio_sink)
            if output.get("state") != "ready":
                raise ValueError("直播TTS尚未完整结束。")
            for data, info in assembler.end():
                await put(data, info)
            await queue.put(_SENTINEL)
            return output

        async def play():
            nonlocal first, blocks, ahead, waiting, duration, byte_count, last
            while (item := await queue.get()) is not _SENTINEL:
                data, info = item
                seconds = len(data) / (info["sample_rate"] * info["channels"] * 2)
                duration += seconds
                byte_count += len(data)
                if duration > voice.max_duration_seconds or byte_count > voice.max_audio_mb * 1024 * 1024:
                    raise ValueError("直播音频超过大小或时长上限。")
                waiting += await clock.pace(seconds, voice.max_ahead_seconds)
                last = await _execute("pcm", {"pcm_base64": base64.b64encode(data).decode("ascii"),
                    "sample_rate": info["sample_rate"], "channels": info["channels"], "sample_width": 2,
                    "text": caption if not blocks else "", "emotion": default if info.get("emotion") == emotion else avatar_emotion(info.get("emotion"), default)}, avatar)
                ahead = max(ahead, clock.submitted(seconds))
                blocks += 1
                if first is None:
                    first = (time.perf_counter() - started) * 1000

        tasks = [asyncio.create_task(work()) for work in (synthesize, play)]
        try:
            output, _ = await asyncio.gather(*tasks)
            if not blocks:
                raise ValueError("直播推理没有提交可播放音频。")
            ending = await _execute("event", {"type": "pcm_end"}, avatar)
            metrics = {**output.get("metrics", {}), "pipeline": "broadcast", "broadcast_first_pcm_ms": first,
                "broadcast_total_ms": round((time.perf_counter() - started) * 1000, 2),
                "broadcast_blocks": blocks, "broadcast_max_queue_blocks": queued,
                "broadcast_max_ahead_ms": round(ahead * 1000, 2),
                "broadcast_backpressure_ms": round(waiting * 1000, 2),
                "broadcast_submitted_seconds": duration, "broadcast_source": "live-" + voice.delivery_mode}
            return {"state": "ready", "pipeline": "broadcast", "duration": duration, "voice_id": output["id"],
                "manifest_path": output.get("manifest_path"),
                "playback": {**last, "mode": "pcm", "queued": bool(last.get("clients")), "completion": ending}, "metrics": metrics}
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            clock.until = 0.0
            try:
                await _execute("event", {"type": "speech_stop"}, avatar)
            except Exception:
                pass
            raise
