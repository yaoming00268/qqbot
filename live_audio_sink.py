"""Owned long-lived local WAV sink for the selected live room; never starts a stream."""
from __future__ import annotations
import asyncio,json,os,sys,time,wave,winsound,sqlite3
from pathlib import Path
import psutil
from bilibili_adapter import BilibiliAdapter
from config import AppConfig
from osu_telemetry_mcp import own_audio_session
from osu_telemetry_worker import publish_latest
from live_events import ROOM, storage_root

def local_enabled(home):
    try:
        config=AppConfig.load(str(home/"config.yaml"))
        x=json.loads((storage_root(home,config)/"control.json").read_text(encoding="utf8"))
        return x.get("enabled") is True and x.get("stop") is not True
    except (OSError, ValueError):return False

def parent_alive(pid,created):
    try: return abs(psutil.Process(pid).create_time()-created)<0.01
    except psutil.Error: return False

async def available(home):
    try:
        config=AppConfig.load(str(home/'config.yaml'))
        control=json.loads((storage_root(home,config)/'control.json').read_text(encoding='utf8'))
        if control.get('enabled') is not True or control.get('stop') is True or not config.bilibili.enabled:
            return False
        data=await asyncio.wait_for(BilibiliAdapter(config.bilibili.credentials_file).room_info(int(ROOM)),timeout=5)
        return data.get('live_status')==1
    except Exception:return False

def main():
    home=Path(sys.argv[1]).resolve();root=Path(sys.argv[2]).resolve();pid=int(sys.argv[3]);created=float(sys.argv[4]);database=Path(sys.argv[5]).resolve()
    config=AppConfig.load(str(home/'config.yaml')); data_root=storage_root(home,config)
    if root.parent != data_root or database != data_root.parent/'main_chat/console.sqlite3':raise ValueError('invalid sink directory')
    initialized=False
    while parent_alive(pid,created) and not (root/'stop.request').exists():
        request=root/'audio-request.json'
        if not request.exists():time.sleep(.1);continue
        data={};started=False
        try:
            data=json.loads(request.read_text(encoding='utf8'));request.unlink()
            path=Path(data['path']).resolve()
            if not path.is_relative_to(root) or path.suffix.lower()!='.wav' or not path.is_file():raise ValueError('invalid wav')
            if not asyncio.run(available(home)) or not parent_alive(pid,created) or (root/'stop.request').exists():raise PermissionError('live closed')
            if not initialized:own_audio_session(unmute=True);initialized=True
            # Fresh room check after initial audio setup, immediately before playback.
            if not asyncio.run(available(home)) or not parent_alive(pid,created) or (root/'stop.request').exists():raise PermissionError('live closed')
            with wave.open(str(path),'rb') as f:seconds=f.getnframes()/f.getframerate()
            with sqlite3.connect(database,timeout=5) as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute("SELECT o.state,e.kind,e.namespace FROM live_outgoing o JOIN live_events e ON e.id=o.event_id WHERE o.id=?",(data['id'],)).fetchone()
                if not row or row[0]!='speaking': raise PermissionError('speech superseded')
                if row[1]=='idle' and db.execute("SELECT 1 FROM live_events WHERE namespace=? AND kind='analysis' AND state IN ('queued','running') LIMIT 1",(row[2],)).fetchone():
                    db.execute("UPDATE live_outgoing SET state='superseded' WHERE id=?",(data['id'],)); raise PermissionError('analysis pending')
                db.execute("UPDATE live_outgoing SET state='playing' WHERE id=? AND state='speaking'",(data['id'],))
            winsound.PlaySound(str(path),winsound.SND_FILENAME|winsound.SND_ASYNC);started=True;begin=time.monotonic()
            while time.monotonic()-begin<seconds:
                if (root/'stop.request').exists() or not parent_alive(pid,created) or not local_enabled(home):
                    winsound.PlaySound(None,winsound.SND_PURGE);raise InterruptedError('stopped')
                time.sleep(.1)
            result={'id':data['id'],'state':'played'}
        except Exception as error:
            result={'id':data.get('id',''),'state':'unknown' if started else 'cancelled','error':type(error).__name__}
        publish_latest(root/'audio-response.json',result)
    winsound.PlaySound(None,winsound.SND_PURGE)
if __name__=='__main__':main()
