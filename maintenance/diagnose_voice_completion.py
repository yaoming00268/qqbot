"""Compare local full and streamed speech; no chat or account access."""
from pathlib import Path
import argparse
import asyncio
import json
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPORT = ROOT / "maintenance_reports/voice-completion-20261116-131025"


async def generate():
    import aiohttp
    from config import AppConfig
    from voice_engine import backend_config, backend_endpoint
    from voice_model_cache import select_models
    from voice_service import engine_lock, load_catalog
    from voice_stream import native_sovits_stream
    from voice_text import japanese_tokens
    config = AppConfig.load(str(ROOT / "config.yaml")).voice
    native = backend_config(config)
    catalog = load_catalog(config)["characters"]
    selected = catalog["chieri-experimental"]
    reference = selected["emotions"]["neutral"]
    texts = ["こんにちは。今日は一緒に本を読みましょう。",
             "今日は図書館で新しい本を探して、そのあと一緒に温かいお茶を飲みながら、ゆっくりお話ししましょう。最後まで聞いてくれてありがとう。"]
    results = []
    REPORT.mkdir(parents=True, exist_ok=True)
    async with engine_lock(backend_endpoint(config)), aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as client:
        async def select(route, params):
            async with client.get(native.endpoint + route, params=params) as response:
                content = await response.read()
                if response.status != 200:
                    raise ValueError(str(response.status) + content[:300].decode(errors="replace"))
                return content
        try:
            await select_models(selected, native.endpoint, select, reuse=False)
            for name, stream, parallel, seed, repetition, text in [
                ("full-short", False, True, 42, 1.35, texts[0]),
                ("stream-short", True, True, 42, 1.35, texts[0]),
                ("full-long", False, True, 42, 1.35, texts[1]),
                ("stream-long", True, True, 42, 1.35, texts[1]),
                ("serial-long", False, False, 42, 1.35, texts[1]),
                ("sample-long", False, False, 17, 1.1, texts[1]),
            ]:
                body = {"text": "".join(japanese_tokens(text, config.dictionary)), "text_lang": "ja",
                    "ref_audio_path": reference["reference_audio"], "prompt_text": "".join(japanese_tokens(reference["prompt_text"], config.dictionary)),
                    "prompt_lang": "ja", "text_split_method": "cut0", "batch_size": 1, "speed_factor": 1,
                    "parallel_infer": parallel, "seed": seed, "repetition_penalty": repetition,
                    "media_type": "wav", "streaming_mode": False}
                output = REPORT / (name + ".wav")
                start = time.perf_counter()
                if stream:
                    await native_sovits_stream(client, native.endpoint, body, output, config)
                else:
                    async with client.post(native.endpoint + "/tts", json=body) as response:
                        content = await response.read()
                        if response.status != 200:
                            raise ValueError(str(response.status) + content[:400].decode(errors="replace"))
                        output.write_bytes(content)
                with wave.open(str(output)) as source:
                    duration = source.getnframes() / source.getframerate()
                row = {"name": name, "file": str(output), "text": text, "kana": body["text"],
                    "duration": duration, "seconds": round(time.perf_counter()-start, 2), "parallel": parallel, "seed": seed}
                results.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                (REPORT / "completion-comparison.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            await select_models(catalog[config.character], native.endpoint, select, reuse=False)


async def verify_fix():
    from unittest.mock import patch
    import aiohttp
    from config import AppConfig
    from voice_engine import backend_config, backend_endpoint
    from voice_model_cache import select_models
    from voice_service import VoiceService, engine_lock, load_catalog
    saved = AppConfig.load(str(ROOT / "config.yaml")).voice
    config = backend_config(saved)
    config.enabled, config.auto_start, config.reuse_audio = True, False, False
    config.character = "chieri-experimental"
    config.cache_dir = str(REPORT / "fixed-audio")
    text = "今日は図書館で新しい本を探して、そのあと一緒に温かいお茶を飲みながら、ゆっくりお話しして、最後にはおいしいケーキを食べてから、おうちに帰りましょうね。"
    rows = []
    try:
        for mode, stream, limit in (("baseline-full", False, None), ("baseline-stream", True, None),
                                    ("fixed-full", False, 32), ("fixed-stream", True, 32)):
            config.streaming = stream
            with patch("voice_synthesis.completion_limit", return_value=limit):
                result = await VoiceService(config).synthesize(text, "neutral")
            row = {"name": mode, "file": result["wav_path"],
                "duration": result["duration"], "text": text, "chunks": [c["text"] for c in result["chunks"]],
                "metrics": result["metrics"]}
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        (REPORT / "fixed-comparison.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        native = backend_config(saved)
        async with engine_lock(backend_endpoint(saved)), aiohttp.ClientSession() as client:
            async def select(route, params):
                async with client.get(native.endpoint+route, params=params) as response:
                    return await response.read()
            await select_models(load_catalog(saved)["characters"][saved.character], native.endpoint, select, reuse=False)


async def corpus():
    from unittest.mock import patch
    from config import AppConfig
    from voice_engine import backend_config
    from voice_service import VoiceService
    config = backend_config(AppConfig.load(str(ROOT / "config.yaml")).voice)
    config.enabled, config.auto_start, config.reuse_audio = True, False, False
    config.character = "chieri-experimental"
    config.cache_dir = str(REPORT / "corpus-audio")
    texts = [
        "お兄ちゃん、世界で一番だよ！宇宙で一番かっこいいんだから！",
        "ねえ、お兄ちゃん、今日はとても楽しかったから明日も明後日もずっと一緒にいたいし、どこへ行くときも隣で手をつないでいてほしいんだけど、最後に一つだけ約束してくれるかな。",
        "あのねおにいちゃんきょうはすごくたのしかったからあしたもあさってもずっといっしょにいたいなどこへいくときもとなりでてをつないでいてほしいんだけどさいごにひとつだけやくそくしてくれるかな。",
        "ここまで聞いてくれて本当にありがとう、今日はあなたとたくさんお話しできてとても嬉しかったから、明日もまた会えるのを心から楽しみにしているので、帰り道は気をつけて、ゆっくり休んでくださいね。",
    ]
    rows = []
    try:
        for index, text in enumerate(texts):
            for name, limit in (("baseline",None),("fixed",32)):
                with patch("voice_synthesis.completion_limit", return_value=limit):
                    result = await VoiceService(config).synthesize(text, "neutral")
                row={"name":f"{name}-{index}","file":result["wav_path"],"text":text,
                     "duration":result["duration"],"chunks":[c["text"] for c in result["chunks"]]}
                rows.append(row)
                print(json.dumps({k:row[k] for k in ("name","duration","chunks")},ensure_ascii=False),flush=True)
        (REPORT / "corpus-comparison.json").write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf-8")
    finally:
        import aiohttp
        from voice_engine import backend_endpoint
        from voice_service import engine_lock, load_catalog
        from voice_model_cache import select_models
        saved=AppConfig.load(str(ROOT / "config.yaml")).voice
        async with engine_lock(backend_endpoint(saved)),aiohttp.ClientSession() as client:
            async def select(route,params):
                async with client.get(backend_config(saved).endpoint+route,params=params) as response:
                    return await response.read()
            await select_models(load_catalog(saved)["characters"][saved.character],backend_config(saved).endpoint,select,reuse=False)


def transcribe(fixed=False, corpus_mode=False, live=False):
    from faster_whisper import WhisperModel
    import numpy as np
    import subprocess
    model = WhisperModel(str(ROOT / "tools/voice-engine/asr-models/Systran--faster-whisper-small"), device="cpu", compute_type="int8")
    name = "live-completion-verification" if live else "corpus-comparison" if corpus_mode else "fixed-comparison" if fixed else "completion-comparison"
    saved = json.loads((REPORT / (name+".json")).read_text(encoding="utf-8"))
    rows = saved["cases"] if live else saved
    for row in rows:
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", row["file"], "-ar", "16000", "-ac", "1", "-f", "f32le", "pipe:1"], capture_output=True, check=True).stdout
        segments, _ = model.transcribe(np.frombuffer(pcm, dtype="<f4"), language="ja", beam_size=5, vad_filter=False)
        row["asr"] = "".join(s.text for s in segments)
        print(json.dumps({k:row[k] for k in ("name", "duration", "asr")}, ensure_ascii=False), flush=True)
    (REPORT / (name+"-asr.json")).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--asr", action="store_true")
    args.add_argument("--fixed", action="store_true")
    args.add_argument("--corpus", action="store_true")
    args.add_argument("--live", action="store_true")
    parsed = args.parse_args()
    if parsed.asr:
        transcribe(parsed.fixed, parsed.corpus, parsed.live)
    elif parsed.corpus:
        asyncio.run(corpus())
    elif parsed.fixed:
        asyncio.run(verify_fix())
    else:
        asyncio.run(generate())
