"""Save/stop via the live desktop, preserve sessions, activate a verified build."""
from pathlib import Path
import argparse
import ctypes
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

import psutil

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
REPORT=ROOT/"maintenance_reports/voice-completion-20261116-131025"
RELEASE=ROOT/"runtime/releases/voice-completion-20261116/QQBot"


def save(name,data):
    (REPORT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")


def stop():
    import uiautomation as automation
    from config import AppConfig
    from maintenance.package_runtime import runtime_files
    runtime_files(RELEASE)
    assert "96 passed" in (REPORT/"tests-final.log").read_text(encoding="utf-8-sig")
    assert json.loads((REPORT/"portable/verification-summary.json").read_text(encoding="utf-8"))["ok"]
    old=(ROOT/"runtime/active-runtime.txt").read_text(encoding="utf-8-sig").strip()
    old_exe=(ROOT/"runtime"/old/"QQBotStudio.exe").resolve()
    guis=[p for p in psutil.process_iter(["exe","cmdline"]) if p.info["exe"] and Path(p.info["exe"]).resolve()==old_exe]
    assert len(guis)==1,[p.pid for p in guis]
    gui=guis[0]
    windows=[c for c in automation.GetRootControl().GetChildren() if c.ProcessId==gui.pid and c.Name=="QQBot Studio"]
    assert len(windows)==1
    window=windows[0]
    ctypes.windll.user32.ShowWindow(window.NativeWindowHandle,9)
    def button(name):
        control=window.ButtonControl(Name=name,searchDepth=30)
        assert control.Exists(2),name
        return control
    controls=[{"name":c.Name,"enabled":c.IsEnabled} for c,d in automation.WalkControl(window,maxDepth=30)
              if c.ControlTypeName=="ButtonControl" and c.Name in ("保存更改","停止 Bot","停止任务","关闭")]
    save("desktop-before-stop.json",{"pid":gui.pid,"controls":controls})
    save_button=button("保存更改")
    if save_button.IsEnabled:
        save_button.GetInvokePattern().Invoke()
        deadline=time.monotonic()+15
        while save_button.IsEnabled:
            if time.monotonic()>deadline:
                raise TimeoutError("Pending edits did not save; desktop and Core left running")
            time.sleep(.2)
    # Only save files after Studio's own revision-checked save has completed.
    config_bytes=(ROOT/"config.yaml").read_bytes()
    children=gui.children(recursive=False)
    cores=[p for p in children if p.name()=="QQBot.exe"]
    stop_button=button("停止 Bot")
    assert stop_button.IsEnabled
    stop_button.GetInvokePattern().Invoke()
    deadline=time.monotonic()+120
    while any(p.is_running() for p in cores):
        if time.monotonic()>deadline:
            raise TimeoutError("Normal Core stop timed out; no process was forced to stop")
        time.sleep(.25)
    backup=REPORT/"activation-backup"
    backup.mkdir(exist_ok=False)
    files=[ROOT/"config.yaml",ROOT/"personas.yaml",ROOT/"runtime/active-runtime.txt",ROOT/"data/voice/catalog.json",
           ROOT/"data/studio/appearance.json",*sorted((ROOT/"data").glob("*sessions*.json"))]
    snapshots=[]
    for source in files:
        if source.is_file():
            target=backup/source.relative_to(ROOT)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target)
            snapshots.append({"file":source.relative_to(ROOT).as_posix(),"sha256":hashlib.sha256(source.read_bytes()).hexdigest()})
    config=AppConfig.load(str(ROOT/"config.yaml"))
    runtime=Path(config.bot.runtime_database_path)
    runtime=runtime if runtime.is_absolute() else ROOT/runtime
    for source in (runtime,runtime.parent/"main_chat/identity.sqlite3"):
        if source.is_file() and source.resolve().is_relative_to(ROOT):
            target=backup/source.relative_to(ROOT)
            target.parent.mkdir(parents=True,exist_ok=True)
            with sqlite3.connect(source) as live,sqlite3.connect(target) as copy:
                live.backup(copy)
    assert config_bytes==(ROOT/"config.yaml").read_bytes()
    button("关闭").GetInvokePattern().Invoke()
    deadline=time.monotonic()+30
    while gui.is_running():
        if time.monotonic()>deadline:
            raise TimeoutError("Desktop has not closed through the normal UI")
        # A final status poll can lag behind the Core exit; honor the desktop's
        # own close dialog, while refusing to discard any newly entered draft.
        try:
            items=[c for c,d in automation.WalkControl(window,maxDepth=30)]
        except Exception:
            # Native accessibility disappears a moment before the host process.
            if not any(c.ProcessId==gui.pid for c in automation.GetRootControl().GetChildren()):
                time.sleep(.2)
                continue
            raise
        if any("未保存的编辑" in c.Name for c in items):
            raise RuntimeError("A new draft appeared during activation; desktop kept open")
        confirmations=[c for c in items if c.ControlTypeName=="ButtonControl" and c.Name in ("停止并关闭","关闭窗口")]
        if confirmations:
            confirmations[0].GetInvokePattern().Invoke()
        time.sleep(.2)
    result={"ok":True,"previous_runtime":old,"gui_pid":gui.pid,"core_pids":[p.pid for p in cores],
            "normal_stop":True,"forced_stop":False,"files":snapshots}
    save("stop-backup.json",result)
    print(json.dumps(result,ensure_ascii=False),flush=True)


def recover_stop():
    """Finish the record if the native window vanished during its exit audit."""
    before=json.loads((REPORT/"desktop-before-stop.json").read_text(encoding="utf-8"))
    assert not psutil.pid_exists(before["pid"])
    backup=REPORT/"activation-backup"
    previous=(backup/"runtime/active-runtime.txt").read_text(encoding="utf-8-sig").strip()
    old=(ROOT/"runtime"/previous).resolve()
    assert not any(p.info["exe"] and Path(p.info["exe"]).resolve() in (old/"QQBot.exe",old/"QQBotStudio.exe")
                   for p in psutil.process_iter(["exe"]))
    files=[]
    for source in backup.rglob("*"):
        if source.is_file() and source.suffix!=".sqlite3":
            relative=source.relative_to(backup)
            assert source.read_bytes()==(ROOT/relative).read_bytes(),relative
            files.append({"file":relative.as_posix(),"sha256":hashlib.sha256(source.read_bytes()).hexdigest()})
    result={"ok":True,"previous_runtime":previous,"gui_pid":before["pid"],"core_pids":[],"normal_stop":True,
            "forced_stop":False,"exit_audit":"Native window closed before accessibility walk; files and exited processes rechecked",
            "files":files}
    save("stop-backup.json",result)
    print(json.dumps({"ok":True,"normal_stop":True,"backup_verified":True},ensure_ascii=False),flush=True)


def activate():
    from config import AppConfig
    from maintenance.package_runtime import runtime_files
    runtime_files(RELEASE)
    stopped=json.loads((REPORT/"stop-backup.json").read_text(encoding="utf-8"))
    assert stopped["ok"] and not psutil.pid_exists(stopped["gui_pid"])
    assert not any(psutil.pid_exists(pid) for pid in stopped["core_pids"])
    pointer=ROOT/"runtime/active-runtime.txt"
    temporary=pointer.with_suffix(".tmp")
    temporary.write_text(str(RELEASE.relative_to(ROOT/"runtime"))+"\n",encoding="utf-8")
    temporary.replace(pointer)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0));port=listener.getsockname()[1]
    before=(ROOT/"config.yaml").read_bytes()
    with (REPORT/"active-gui.log").open("ab") as log:
        proc=subprocess.Popen([str(RELEASE/"QQBotStudio.exe"),"--home",str(ROOT)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,env={**os.environ,"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":f"--remote-debugging-port={port}"},
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    assert before==(ROOT/"config.yaml").read_bytes()
    config=AppConfig.load(str(ROOT/"config.yaml"))
    result={"ok":True,"gui_pid":proc.pid,"debug_port":port,"runtime":str(RELEASE),"character":config.voice.character,
            "accompaniment_gain_db":config.voice.singing.accompaniment_gain_db,"no_qq_send":True}
    save("activation.json",result)
    print(json.dumps(result,ensure_ascii=False),flush=True)


def verify():
    from playwright.sync_api import sync_playwright
    value=json.loads((REPORT/"activation.json").read_text(encoding="utf-8"))
    with sync_playwright() as p:
        browser=None
        for _ in range(100):
            try:
                browser=p.chromium.connect_over_cdp(f"http://127.0.0.1:{value['debug_port']}",timeout=700);break
            except Exception:
                time.sleep(.2)
        assert browser
        page=next(page for context in browser.contexts for page in context.pages if "index.html" in page.url)
        page.wait_for_function("window.studio?.ready",timeout=30000)
        assert page.evaluate("window.studio.version")=="20261116-voice-completion"
        errors=[]
        page.on("pageerror",lambda error:errors.append(str(error)))
        state=page.evaluate("()=>rpc('control','start')")
        assert state["state"]=="running",state
        page.wait_for_function("S.events.some(e=>e.kind==='log' && /成功连接并登录本地 QQNT/.test(e.data.text))",timeout=60000)
        page.wait_for_function("S.status.voice?.state==='applied'",timeout=20000)
        assert page.evaluate("S.savedConfig.voice.character")==value["character"]
        page.locator('[data-page="voice-training"]').click()
        page.wait_for_selector('#training-dataset option[value="dataset-ea33a1df09de"]',state="attached")
        page.locator('#training-dataset').select_option("dataset-ea33a1df09de")
        page.wait_for_selector('[data-voice-annotation="emotion"]')
        assert page.locator('[data-voice-annotation="emotion"]').first.locator("option").count()>=43
        assert page.locator('#training-reference-character').input_value()=="chieri-experimental"
        page.locator('.voice-training-table').evaluate("el=>el.scrollIntoView({block:'start',behavior:'instant'})")
        page.screenshot(path=str(REPORT/"active-reference-ui.png"),full_page=True)
        # Synthesize only on this machine, using an override, without changing the saved role.
        task=page.evaluate("()=>rpc('voice','preview',{config:{...S.config.voice,character:'chieri-experimental'},text:'一緒に本を読んで、ゆっくりお話ししましょう。最後まで聞いてくれてありがとう。',emotion:'neutral'})")
        page.wait_for_function("id=>S.status.task?.id===id && S.status.task.state!=='running'",arg=task["id"],timeout=180000)
        preview=page.evaluate("()=>{const t=S.status.task,r=t.result||{};return {state:t.state,error:t.error,character:r.character,duration:r.duration,effective_provider:r.effective_provider,completion_policy:r.completion_policy,manifest_path:r.manifest_path,chunks:(r.chunks||[]).map(c=>({text:c.text,duration_checks:c.duration_checks,stream_blocks:c.stream?.blocks}))}}")
        assert preview["state"]=="completed",preview
        assert preview["effective_provider"]=="voicestudio" and preview["completion_policy"]["duration_guard"]
        assert preview["chunks"][-1]["text"]=="さいごまできいてくれてありがとう。"
        sessions=[]
        for before in sorted((REPORT/"activation-backup/data").glob("*sessions*.json")):
            old=json.loads(before.read_text(encoding="utf-8"));after=ROOT/"data"/before.name
            current=json.loads(after.read_text(encoding="utf-8"))
            assert set(old).issubset(set(current)),before.name
            def cid(value):
                if isinstance(value,dict):return {k:v for k,v in value.items() if k in ("cid","rid","context_id","conversation_id")}
                return value
            assert all(cid(old[k])==cid(current[k]) for k in old),before.name
            sessions.append({"file":before.name,"entries":len(old),"context_ids_preserved":True})
        assert not errors,errors
        result={"ok":True,"state":page.evaluate("({state:S.status.state,pid:S.status.pid,voice:S.status.voice,character:S.savedConfig.voice.character})"),
                "preview":preview,"sessions":sessions,"javascript_errors":errors,"no_qq_send":True}
        save("active-verification.json",result)
        browser.close()
    print(json.dumps({"ok":True,"preview":preview,"sessions_preserved":True},ensure_ascii=False),flush=True)
    import asyncio
    import aiohttp
    from config import AppConfig
    from voice_engine import backend_config,backend_endpoint
    from voice_model_cache import select_models
    from voice_service import engine_lock,load_catalog
    async def restore():
        config=AppConfig.load(str(ROOT/"config.yaml")).voice
        native=backend_config(config)
        async with engine_lock(backend_endpoint(config)),aiohttp.ClientSession() as client:
            async def select(route,params):
                async with client.get(native.endpoint+route,params=params) as response:
                    return await response.read()
            await select_models(load_catalog(config)["characters"][config.character],native.endpoint,select,reuse=False)
    asyncio.run(restore())


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=("stop","recover-stop","activate","verify"))
    action=parser.parse_args().action
    {"stop":stop,"recover-stop":recover_stop,"activate":activate,"verify":verify}[action]()
