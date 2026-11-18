"""Keep the retry loop local to a single phrase and discard rejected stream PCM."""
from pathlib import Path

path=Path(__file__).resolve().parents[1]/"voice_synthesis.py"
text=path.read_text(encoding="utf-8")
start=text.index('                        if effective == "voicestudio":',text.index('for index, chunk in enumerate(chunks):'))
end=text.index('                        info["wav"]',start)
old=text[start:end]
old=old.replace('progress=report if config.streaming else None','progress=emit if config.streaming else None')
old=old.replace('progress=report, index=index','progress=emit, index=index')
old=old.replace('"speed": config.speed, "engine": config.voicestudio.engine}', '"speed": config.speed, "engine": config.voicestudio.engine,\n                                    "seed": seed, "parallel_infer": attempt == 0}')
old=old.replace('"sample_steps": config.sample_steps, "parallel_infer": True,','"sample_steps": config.sample_steps, "parallel_infer": attempt == 0,')
old=old.replace('"streaming_mode": False, "seed": 42}', '"streaming_mode": False, "seed": seed}')
header='''                        guarded = bool(character.get("experimental"))
                        for attempt in range(2 if guarded else 1):
                            current()
                            seed = 42 if attempt == 0 else 17
                            pending = []
                            emit = pending.append if guarded else report
'''
old=''.join('    '+line if line.strip() else line for line in old.splitlines(keepends=True))
footer='''                            if guarded:
                                from voice_completion import inspect_chunk
                                check = inspect_chunk(target, chunk.text, chunk.language, config.speed)
                                info.setdefault("duration_checks", []).append({"attempt": attempt+1, "seed": seed, **check})
                                if check["suspected_early_end"]:
                                    save()
                                    if attempt == 0:
                                        report({"type": "stage", "stage": "retrying", "index": index,
                                                "message": "片段异常短，丢弃本次音频并重新合成完整台词。"})
                                        continue
                                    raise VoiceError("模型连续生成异常短的片段，疑似提前结束；未提交半段语音，请更换参考切片或核对台词。")
                                for event in pending:
                                    report(event)
                            break
'''
text=text[:start]+header+old+footer+text[end:]
path.write_text(text,encoding="utf-8")
