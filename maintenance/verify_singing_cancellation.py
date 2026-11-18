"""Kill an owned nested Python worker fixture; never touches bot/TTS processes."""
import asyncio
import json
from pathlib import Path
import sys
import time
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def verify():
    from singing_service import SingingService
    from voice_config import VoiceConfig
    folder = ROOT / "maintenance_reports/singing-20261114/cancel-fixture"
    folder.mkdir(parents=True, exist_ok=True)
    child = folder / "sleep_fixture.py"
    child.write_text("import time\ntime.sleep(300)\n", encoding="utf-8")
    parent = folder / "parent_fixture.py"
    parent.write_text("import subprocess,sys,time,os,json\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,sys.argv[1]])\nPath(sys.argv[2]).write_text(json.dumps([os.getpid(),p.pid]))\ntime.sleep(300)\n", encoding="utf-8")
    pids = folder / "pids.json"
    pids.unlink(missing_ok=True)
    service = SingingService(VoiceConfig())
    task = asyncio.create_task(service._run([ROOT / "tools/singing-engine/.venv/Scripts/python.exe", parent, child, pids], folder, "fixture"))
    started = time.monotonic()
    while not pids.is_file():
        if task.done():
            await task
        if time.monotonic() - started > 15:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise TimeoutError("fixture startup")
        await asyncio.sleep(0.1)
    ids = json.loads(pids.read_text())
    owned = {p.pid: p.create_time() for pid in ids for p in [psutil.Process(pid), *psutil.Process(pid).children(recursive=True)]}
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    alive = []
    for pid, created in owned.items():
        try:
            if psutil.Process(pid).create_time() == created:
                alive.append(pid)
        except psutil.NoSuchProcess:
            pass
    result = {"ok": not alive, "workers_checked": len(owned), "alive": alive, "seconds": time.monotonic() - started}
    (folder.parent / "cancellation-verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    assert result["ok"], result
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(verify())
