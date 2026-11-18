"""Bounded PCM streaming for VoiceStudio WebSocket and native api_v2 WAV."""
from __future__ import annotations

import asyncio
import base64
import json
import struct
import time
import wave

import aiohttp

from voice_text import VoiceError


def completed_pcm_events(path, config, *, index=0, first_audio_ms=0):
    """Replay validated output rather than retaining unverified transport frames."""
    with wave.open(str(path), "rb") as stream:
        rate, channels = stream.getframerate(), stream.getnchannels()
        if stream.getsampwidth() != 2 or channels not in (1, 2):
            raise VoiceError("已验证的音频必须为单／双声道 PCM16。")
        count, sequence = max(1, rate * config.stream_chunk_ms // 1000), 0
        while data := stream.readframes(count):
            if len(data) % (channels * 2):
                raise VoiceError("已验证的音频包含不完整采样。")
            sequence += 1
            yield {"type": "audio", "index": index, "sample_rate": rate,
                   "channels": channels, "format": "pcm16", "sequence": sequence,
                   "pcm_base64": base64.b64encode(data).decode("ascii"), "first_audio_ms": first_audio_ms}


class WavStreamDecoder:
    """Incremental RIFF parser, including api_v2's zero-sized streaming data header."""
    def __init__(self):
        self.buffer = bytearray()
        self.riff = False
        self.format = None
        self.started = False
        self.remaining = None
        self.padding = 0

    def feed(self, data):
        self.buffer.extend(data)
        if not self.riff:
            if len(self.buffer) < 12:
                return b""
            if self.buffer[:4] != b"RIFF" or self.buffer[8:12] != b"WAVE":
                raise VoiceError("流式接口没有返回 RIFF/WAVE 音频。")
            del self.buffer[:12]
            self.riff = True
        while not self.started:
            if len(self.buffer) < 8:
                return b""
            name, size = struct.unpack("<4sI", self.buffer[:8])
            if name == b"data":
                if self.format is None:
                    raise VoiceError("WAV 的 data 出现在 fmt 之前。")
                del self.buffer[:8]
                self.remaining = size if 0 < size < 0xFFFFFF00 else None
                self.started = True
                break
            if size > 65536:
                raise VoiceError("WAV 头部超过大小限制。")
            if len(self.buffer) < 8 + size + size % 2:
                return b""
            if name == b"fmt ":
                if size < 16:
                    raise VoiceError("WAV fmt 数据不完整。")
                encoding, channels, rate, _, align, bits = struct.unpack("<HHIIHH", self.buffer[8:24])
                if encoding != 1 or channels not in (1, 2) or bits != 16 or align != channels * 2 or not 8000 <= rate <= 96000:
                    raise VoiceError("流式 WAV 必须为 8–96kHz、单／双声道 PCM16。")
                self.format = (rate, channels)
            del self.buffer[:8 + size + size % 2]
        size = len(self.buffer) if self.remaining is None else min(len(self.buffer), self.remaining)
        # Preserve partial samples across arbitrary HTTP frame boundaries.
        align = self.format[1] * 2
        size -= size % align
        output = bytes(self.buffer[:size])
        del self.buffer[:size]
        if self.remaining is not None:
            self.remaining -= size
        return output

    def finish(self):
        if not self.started or self.buffer or self.remaining not in (None, 0):
            raise VoiceError("流式 WAV 在完成前中断或包含不完整 PCM 数据。")


class PCMWriter:
    def __init__(self, path, config, *, progress=None, index=0, emit_events=False):
        self.path, self.config, self.progress, self.index = path, config, progress, index
        self.emit_events = emit_events
        self.stream, self.rate, self.channels = None, None, None
        self.bytes, self.blocks, self.pending = 0, 0, b""
        self.started_at, self.first_audio_at = time.perf_counter(), None

    def start(self, rate, channels, format="pcm16"):
        if self.stream or format != "pcm16" or type(channels) is not int or channels not in (1, 2) or type(rate) is not int or not 8000 <= rate <= 96000:
            raise VoiceError("流式音频起始帧无效。")
        self.rate, self.channels = rate, channels
        self.stream = wave.open(str(self.path), "wb")
        self.stream.setparams((channels, 2, rate, 0, "NONE", "not compressed"))

    def write(self, data):
        if not self.stream:
            raise VoiceError("PCM 到达前没有有效音频起始帧。")
        self.pending += data
        align = self.channels * 2
        count = len(self.pending) // align * align
        if not count:
            return
        data, self.pending = self.pending[:count], self.pending[count:]
        self.bytes += len(data)
        if self.bytes > self.config.max_audio_mb * 1024 * 1024 or self.bytes / align / self.rate > self.config.max_duration_seconds:
            raise VoiceError("流式音频超过大小或时长上限。")
        self.stream.writeframesraw(data)
        now = time.perf_counter()
        if self.first_audio_at is None:
            self.first_audio_at = now
        self.blocks += 1
        if not self.progress and not self.emit_events:
            return None
        event = {"type": "audio", "index": self.index, "sample_rate": self.rate,
                 "channels": self.channels, "format": "pcm16", "sequence": self.blocks,
                 "pcm_base64": base64.b64encode(data).decode("ascii"),
                 "first_audio_ms": (self.first_audio_at - self.started_at) * 1000}
        if self.progress:
            self.progress(event)
        return event

    def close(self, *, complete=False):
        if self.stream:
            self.stream.close()
            self.stream = None
        if complete and (self.pending or not self.bytes):
            raise VoiceError("流式音频为空或包含不完整采样。")
        return {"bytes": self.bytes, "blocks": self.blocks, "sample_rate": self.rate,
                "channels": self.channels, "first_audio_ms": None if self.first_audio_at is None else
                round((self.first_audio_at - self.started_at) * 1000, 2)}


async def native_sovits_stream(client, url, body, target, config, *, progress=None, index=0, audio_sink=None):
    decoder = WavStreamDecoder()
    writer = PCMWriter(target, config, progress=progress, index=index, emit_events=audio_sink is not None)
    try:
        async with client.post(url + "/tts", json={**body, "streaming_mode": True, "media_type": "wav"}, allow_redirects=False) as response:
            if response.status != 200:
                detail = await response.content.read(800)
                raise VoiceError(f"流式 TTS 返回 HTTP {response.status}：" + detail.decode("utf-8", errors="replace"))
            async for block in response.content.iter_chunked(16384):
                pcm = decoder.feed(block)
                if decoder.format and not writer.rate:
                    writer.start(*decoder.format)
                if pcm:
                    event = writer.write(pcm)
                    if event and audio_sink:
                        await audio_sink(event)
            decoder.finish()
        return writer.close(complete=True)
    finally:
        writer.close()


async def voicestudio_stream(ws, body, target, config, *, progress=None, index=0, audio_sink=None):
    """Exact upstream start / binary PCM / done contract; EOF is not success."""
    writer = PCMWriter(target, config, progress=progress, index=index, emit_events=audio_sink is not None)
    await ws.send_json(body)
    notices = []
    try:
        while True:
            message = await ws.receive()
            if message.type == aiohttp.WSMsgType.BINARY:
                event = writer.write(message.data)
                if event and audio_sink:
                    await audio_sink(event)
            elif message.type == aiohttp.WSMsgType.TEXT:
                if len(message.data) > 32768:
                    raise VoiceError("VoiceStudio 控制帧超过大小限制。")
                event = json.loads(message.data)
                if event.get("type") == "start":
                    writer.start(event.get("sample_rate"), event.get("channels"), event.get("format"))
                elif event.get("type") == "done":
                    info = writer.close(complete=True)
                    if event.get("audio_bytes") is not None and event["audio_bytes"] != info["bytes"]:
                        raise VoiceError("VoiceStudio 音频长度校验失败。")
                    return {**info, "server": event, "notices": notices}
                elif event.get("type") == "error":
                    raise VoiceError("VoiceStudio：" + str(event.get("detail", "合成失败"))[:800])
                elif event.get("type") == "routing":
                    notices.append(event)
            elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                raise VoiceError("VoiceStudio 在完成帧到达前断开；未把半段音频当作成功。")
    finally:
        writer.close()
