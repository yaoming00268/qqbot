"""Real native/bridge and all training-reference smoke; no QQ transport."""
from pathlib import Path
import asyncio
import io
import json
import sys
import socket
import subprocess
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
REPORT=ROOT/"maintenance_reports/voice-completion-20261116-131025"


async def main():
    import aiohttp
    from config import AppConfig
    from pysilk import decode
    from voice_dataset import atomic_json,load_dataset
    from voice_engine import backend_config,backend_endpoint
    from voice_model_cache import select_models
    from voice_references import training_reference
    from voice_service import VoiceService,engine_lock,load_catalog
    saved=AppConfig.load(str(ROOT/"config.yaml")).voice
    original_bytes=(ROOT/"data/voice/catalog.json").read_bytes()
    config=saved.model_copy(deep=True)
    config.enabled,config.auto_start,config.reuse_audio=True,False,False
    config.character="chieri-experimental"
    config.cache_dir=str(REPORT/"live-audio")
    config.catalog_path=str(REPORT/"live-catalog.json")
    catalog=load_catalog(saved)
    atomic_json(config.catalog_path,catalog)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0));port=listener.getsockname()[1]
    config.provider="voicestudio"
    config.endpoint=f"http://127.0.0.1:{port}"
    settings=REPORT/"live-bridge-config.json"
    atomic_json(settings,config.model_dump())
    log=(REPORT/"live-bridge.log").open("wb")
    process=subprocess.Popen([str(ROOT/config.engine_python),"-X","utf8",str(ROOT/"voicestudio_bridge.py"),
        "--home",str(ROOT),"--config",str(settings),"--port",str(port)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    rows=[]
    try:
        async with aiohttp.ClientSession() as client:
            for _ in range(100):
                try:
                    async with client.get(config.endpoint+"/health") as response:
                        if response.status==200:
                            break
                except aiohttp.ClientError:
                    pass
                if process.poll() is not None:
                    raise RuntimeError("Temporary bridge exited; see live-bridge.log")
                await asyncio.sleep(.2)
            else:
                raise TimeoutError("Temporary bridge was not ready")
        for streaming in (False,True):
            config.streaming=streaming
            events=[]
            result=await VoiceService(config).synthesize(
                "一緒に図書館へ行って、新しい本を探して、温かいお茶を飲みながらゆっくりお話ししましょう。最後まで聞いてくれてありがとう。",
                "neutral",progress=events.append)
            raw=io.BytesIO()
            decode(io.BytesIO(Path(result["silk_path"]).read_bytes()),raw,sample_rate=24000)
            decoded_seconds=len(raw.getvalue())/48000
            assert abs(decoded_seconds-result["duration"])<.06,(decoded_seconds,result["duration"])
            assert result["chunks"][-1]["text"]=="さいごまできいてくれてありがとう。"
            assert all(c["duration_checks"][-1]["suspected_early_end"] is False for c in result["chunks"])
            assert result["effective_provider"]=="voicestudio"
            if streaming:
                assert any(e.get("type")=="audio" for e in events)
            rows.append({"name":"bridge-stream" if streaming else "bridge-full","file":result["wav_path"],
                "text":result["spoken_text"],"duration":result["duration"],"silk_decoded_seconds":decoded_seconds,
                "chunks":[{"text":c["text"],"duration_checks":c["duration_checks"]} for c in result["chunks"]],"metrics":result["metrics"]})
        data=load_dataset(ROOT/"data/voice/training","dataset-ea33a1df09de",verify=True)
        character=catalog["characters"]["chieri-experimental"]
        for row in data["rows"]:
            source=ROOT/"data/voice/training/datasets"/data["id"]/row["audio"]
            ref=training_reference(source,row,REPORT/"reference-candidates",data["id"])
            assert ref["prompt_text"]==row["text"]
            character["emotions"]["neutral"]=ref
            atomic_json(config.catalog_path,catalog)
            config.streaming=True
            result=await VoiceService(config).synthesize("さいごまできいてくれてありがとう。","neutral")
            rows.append({"name":"reference-"+row["id"],"file":result["wav_path"],"text":result["spoken_text"],
                         "duration":result["duration"],"reference_original_duration":ref["original_duration"],
                         "reference_padding_seconds":ref["padding_seconds"],"reviewed":ref["reviewed"]})
            print(json.dumps({"sample":row["id"],"duration":result["duration"],"padding":ref["padding_seconds"]},ensure_ascii=False),flush=True)
        result={"ok":True,"cases":rows,"all_eight_references_synthesized":True,"silk_tail_duration_verified":True,
                "saved_character":saved.character,"catalog_preserved":original_bytes==(ROOT/"data/voice/catalog.json").read_bytes(),"no_qq_send":True}
        atomic_json(REPORT/"live-completion-verification.json",result)
        print(json.dumps({k:v for k,v in result.items() if k!="cases"},ensure_ascii=False),flush=True)
    finally:
        if process.poll() is None:
            process.terminate();process.wait(timeout=10)
        log.close()
        native=backend_config(saved)
        async with engine_lock(backend_endpoint(saved)),aiohttp.ClientSession() as client:
            async def select(route,params):
                async with client.get(native.endpoint+route,params=params) as response:
                    return await response.read()
            await select_models(load_catalog(saved)["characters"][saved.character],native.endpoint,select,reuse=False)


if __name__=="__main__":
    asyncio.run(main())
