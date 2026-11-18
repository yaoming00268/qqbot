"""Local voice setup, diagnostics and preview. Never sends a QQ message."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import subprocess
import wave
import aiohttp


def main(argv=None):
    from config import AppConfig
    from runtime_paths import application_home, configure_runtime
    from voice_service import VoiceService, voice_context
    from voice_text import VoiceError, prepare_chunks
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--config", default="config.yaml")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("check")
    commands.add_parser("studio-setup", help="安装并检查固定版本 VoiceStudio 流式组件；复用现有角色模型")
    training = commands.add_parser("train", help="角色声音训练、数据复核、验证与注册；不会发送 QQ 消息")
    training.add_argument("action", choices=("status", "setup", "assets", "fetch-chieri", "prepare", "import", "dataset", "use-reference", "run", "validate", "publish", "log"))
    training.add_argument("--dataset", default="")
    training.add_argument("--run", default="")
    training.add_argument("--path", type=Path)
    training.add_argument("--audio", type=Path, action="append")
    training.add_argument("--speaker", default="")
    training.add_argument("--name", default="")
    training.add_argument("--character", default="")
    training.add_argument("--sample", default="")
    training.add_argument("--replace-reference", action="store_true", help="明确替换目标角色已存在的同情绪参考绑定")
    training.add_argument("--language", choices=("ja", "zh", "en", "ko", "yue"), default="ja")
    training.add_argument("--allow-unreviewed", action="store_true")
    training.add_argument("--resume", default="")
    training.add_argument("--version", choices=("v2ProPlus", "v4"))
    training.add_argument("--batch-size", type=int)
    training.add_argument("--sovits-epochs", type=int)
    training.add_argument("--gpt-epochs", type=int)
    training.add_argument("--download-only", action="store_true")
    for name in ("prepare", "preview"):
        command = commands.add_parser(name)
        content = command.add_mutually_exclusive_group(required=True)
        content.add_argument("--text")
        content.add_argument("--text-file", type=Path)
        command.add_argument("--language", choices=("ja", "zh", "en", "mixed"))
        if name == "preview":
            command.add_argument("--emotion", required=True)
            command.add_argument("--spoken-text", default="")
            command.add_argument("--stream", action=argparse.BooleanOptionalAction, default=None)
            command.add_argument("--no-cache", action="store_true")
            command.add_argument("--no-denoise", action="store_true")
    commands.add_parser("engine").add_argument("action", choices=("start", "stop", "status"))
    sing = commands.add_parser("sing", help="歌曲与歌词演唱；本地生成，不发 QQ")
    sing.add_argument("action", choices=("status", "check", "setup", "cover", "compose"))
    sing.add_argument("--song", help="原唱、导唱或伴奏的本机音频路径")
    lyrics = sing.add_mutually_exclusive_group()
    lyrics.add_argument("--lyrics")
    lyrics.add_argument("--lyrics-file", type=Path)
    sing.add_argument("--accompaniment", default="")
    sing.add_argument("--vocals-only", action="store_true")
    sing.add_argument("--emotion", default="neutral")
    sing.add_argument("--language", choices=("ja", "zh", "en", "unknown"), default="ja")
    sing.add_argument("--style", default="Expressive clear solo female singing, melodic and natural.")
    sing.add_argument("--pitch-shift", type=int, default=0)
    sing.add_argument("--output", choices=("file", "voice", "both"), default="file")
    sing.add_argument("--seed", type=int, default=42)
    sing.add_argument("--python", type=Path)
    sing.add_argument("--torch-site-packages", type=Path)
    sing.add_argument("--skip-install", action="store_true")
    setup = commands.add_parser("setup")
    setup.add_argument("--python", type=Path, help="Python 3.10–3.12 executable; required by the frozen installer")
    setup.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    setup.add_argument("--torch-site-packages", type=Path, help="Read-only reuse of an existing matching torch/torchaudio installation")
    setup.add_argument("--torch-index-url", default="", help="Default: official cu128 or cpu PyTorch wheel index")
    setup.add_argument("--skip-install", action="store_true", help="Validate existing environment and rebuild model/catalog paths after moving home")
    args = parser.parse_args(argv)
    try:
        home = configure_runtime(args.home or application_home())
        if args.command == "setup":
            from voice_setup import setup_voice
            result = setup_voice(home, python=args.python, device=args.device, reuse=args.torch_site_packages,
                                 index_url=args.torch_index_url, skip_install=args.skip_install)
        else:
            config = AppConfig.load(str(home / args.config)).voice
            if getattr(args, "language", None) and args.command != "train":
                config.default_language = args.language
            if args.command == "train":
                from voice_training import VoiceTraining
                from voice_training_config import VoiceTrainingConfig
                from voice_dataset import file_hash
                overrides = {k:getattr(args,k) for k in ("version", "batch_size", "sovits_epochs", "gpt_epochs") if getattr(args,k) is not None}
                config.training = VoiceTrainingConfig.model_validate({**config.training.model_dump(), **overrides})
                manager = VoiceTraining(config, home)
                progress = lambda event: print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)
                if args.action == "status":
                    result = manager.status()
                elif args.action == "setup":
                    from voice_training_setup import setup_training
                    result = setup_training(home, config, progress=progress)
                elif args.action == "assets":
                    result = manager.worker("assets", {"version": config.training.version}, progress=progress)
                elif args.action == "fetch-chieri":
                    result = manager.acquire_chieri(progress=progress)
                    if not args.download_only:
                        result = manager.prepare_audio(result["sources"], speaker="chieri", name="雪村千绘莉 · 官方普通试听样本",
                            source="https://www.cabbage-soft.com/products/amachoco/", license_text=result["license"],
                            attribution=result["attribution"], progress=progress)
                elif args.action == "prepare":
                    if not args.audio:
                        raise ValueError("请用 --audio 指定本地音频，可重复指定多个文件。")
                    result = manager.prepare_audio([{"audio":str(p.resolve()), "source":str(p.resolve())} for p in args.audio],
                        speaker=args.speaker, language=args.language, name=args.name, progress=progress)
                elif args.action == "import":
                    if not args.path:
                        raise ValueError("请用 --path 指定 CSV、JSONL 或 .list。")
                    result = manager.import_annotations(args.path, speaker=args.speaker, name=args.name, progress=progress)
                elif args.action == "dataset":
                    result = manager.dataset(args.dataset)
                elif args.action == "use-reference":
                    result = manager.use_reference(args.dataset, args.sample, args.character,
                        dataset_revision=file_hash(manager.root/"datasets"/args.dataset/"manifest.json"),
                        catalog_revision=file_hash(manager.path(config.catalog_path)), replace=args.replace_reference)
                elif args.action == "run":
                    result = manager.run(args.dataset, allow_unreviewed=args.allow_unreviewed, resume=args.resume, progress=progress)
                elif args.action == "validate":
                    result = manager.validate(args.run, progress=progress)
                elif args.action == "publish":
                    catalog = manager.path(config.catalog_path)
                    result = manager.publish(args.run, args.character, expected_revision=file_hash(catalog) if catalog.exists() else "")
                else:
                    result = manager.log(args.run)
            elif args.command == "sing":
                from singing_service import SingingService, singing_context
                if args.action == "setup":
                    from singing_setup import setup_singing
                    result = setup_singing(home, python=args.python, reuse=args.torch_site_packages, skip_install=args.skip_install)
                elif args.action == "status":
                    result = singing_context(config)
                elif args.action == "check":
                    result = asyncio.run(SingingService(config).check())
                else:
                    from singing_config import SingingRequest
                    if not args.song or args.lyrics is None and args.lyrics_file is None:
                        raise ValueError("唱歌需要 --song 与 --lyrics 或 --lyrics-file。")
                    text = args.lyrics if args.lyrics is not None else args.lyrics_file.read_text(encoding="utf-8-sig")
                    request = SingingRequest(mode=args.action, song=args.song, lyrics=text, accompaniment=args.accompaniment,
                        vocals_only=args.vocals_only, emotion=args.emotion, language=args.language, style=args.style,
                        pitch_shift=args.pitch_shift, song_output=args.output, seed=args.seed)
                    result = asyncio.run(SingingService(config).render(request, allow_disabled=True,
                        progress=lambda value: print(json.dumps(value, ensure_ascii=False), file=sys.stderr, flush=True)))
            elif args.command == "studio-setup":
                from voicestudio_setup import setup_studio
                result = setup_studio(config)
            elif args.command == "status":
                from voice_engine import engine_status, studio_status
                result = {"voice": voice_context(config), "engine": engine_status(), "studio": studio_status()}
            elif args.command == "check":
                result = asyncio.run(VoiceService(config).check())
            elif args.command in ("prepare", "preview"):
                text = args.text if args.text is not None else args.text_file.read_text(encoding="utf-8-sig")
                if args.command == "prepare":
                    result = {"chunks": [chunk.to_dict() for chunk in prepare_chunks(text, config)]}
                else:
                    config.enabled = True  # Explicit local preview is independent of the QQ output switch.
                    if args.stream is not None:
                        config.streaming = args.stream
                    if args.no_cache:
                        config.reuse_audio = False
                    if args.no_denoise:
                        config.denoise.enabled = False
                    result = asyncio.run(VoiceService(config).synthesize(text, args.emotion, spoken_text=args.spoken_text,
                        progress=lambda event: print(json.dumps({k:v for k,v in event.items() if k != "pcm_base64"}, ensure_ascii=False), file=sys.stderr, flush=True)))
            else:
                from voice_engine import engine_status, ready, stop_engine, stop_studio
                result = (asyncio.run(ready(config)) if args.action == "start" else
                          {"studio": stop_studio(), "engine": stop_engine()} if args.action == "stop" else engine_status())
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError, aiohttp.ClientError, wave.Error) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
