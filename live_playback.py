"""One-at-a-time live speech delivery with fail-closed, non-replayed receipts."""
from __future__ import annotations
import asyncio

async def drain_one(broker, namespace, live_available, generation_current, render, play):
    with broker.connect() as db:
        row=db.execute("""SELECT o.*,e.generation,e.root,e.room,e.run FROM live_outgoing o
            JOIN live_events e ON e.id=o.event_id WHERE e.namespace=? AND o.state='queued'
            AND e.state IN ('running','done') ORDER BY o.created,o.rowid LIMIT 1""",(namespace,)).fetchone()
    if not row:return False
    row=dict(row)
    if not await live_available() or not generation_current(row):
        with broker.connect() as db:
            db.execute("UPDATE live_outgoing SET state='cancelled' WHERE state='queued' AND event_id IN (SELECT id FROM live_events WHERE namespace=?)",(namespace,))
        return True
    with broker.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        changed=db.execute("UPDATE live_outgoing SET state='rendering' WHERE id=? AND state='queued'",(row['id'],)).rowcount
    if not changed:return True
    status='unknown'
    try:
        path=await render(row)
        if not await live_available() or not generation_current(row):
            status='cancelled'
        else:
            with broker.connect() as db:
                changed=db.execute("UPDATE live_outgoing SET state='speaking' WHERE id=? AND state='rendering'",(row['id'],)).rowcount
            if not changed: return True
            status=await play(row,path)
            if status not in ('played','cancelled','unknown'):status='unknown'
    except asyncio.CancelledError:
        raise
    except Exception:
        status='unknown'
    finally:
        with broker.connect() as db:
            db.execute("UPDATE live_outgoing SET state=? WHERE id=? AND state IN ('rendering','speaking','playing')",(status,row['id']))
    return True


class OwnedAudio:
    """Separate synthesis engine and persistent sink owned by this bot process."""
    def __init__(self, home, config, database):
        import socket,uuid
        from pathlib import Path
        self.home=Path(home).resolve();self.database=Path(database).resolve();self.root=self.database.parent.parent/'osu-live'/('audio-'+uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.config=config.model_copy(deep=True, update={'enabled':True,'auto_start':True,'delivery_mode':'validated','process_namespace':'osu-live','default_language':'zh'})
        if self.config.provider != 'gpt_sovits_v2': raise ValueError('仅允许隔离本机语音引擎')
        for name in ('catalog_path','engine_python','engine_directory','engine_config'):
            path=Path(getattr(self.config,name));setattr(self.config,name,str(path if path.is_absolute() else (self.home/path).resolve()))
        self.config.cache_dir=str(self.root/'voice-cache')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));self.config.endpoint='http://127.0.0.1:'+str(sock.getsockname()[1])
        self.process=None;self.engine_owned=False;self.engine_identity=None;self.engine_process=None;self.engine_failed=False

    async def render(self,row):
        import base64,wave
        from broadcast_tts import BroadcastVoiceService
        from voice_engine import ready, engine_status, _stop_owned_process
        if self.engine_failed: raise RuntimeError("直播语音引擎启动未完成，等待停止清理。")
        if not self.engine_owned:
            if engine_status(self.config).get('running'):
                raise RuntimeError('隔离语音进程已被其他实例占用')
            def started(process, data):
                self.engine_process=process
                self.engine_identity=(data['pid'],data['create_time'],data['endpoint'])
                self.engine_owned=True
            try:
                state=await ready(self.config,on_started=started)
                if not self.engine_process or not state.get('owned') or state.get('reused') or self.engine_identity != (state.get('pid'),state.get('create_time'),state.get('endpoint')):
                    raise RuntimeError('语音进程归属未确认')
            except BaseException:
                self.engine_failed=True
                if self.engine_process is not None:
                    try: await asyncio.to_thread(_stop_owned_process,self.engine_process)
                    except Exception: pass
                raise
        samples=[];fmt={}
        async def sink(event):
            if not fmt:fmt.update(rate=event['sample_rate'],channels=event['channels'])
            if fmt['rate']!=event['sample_rate'] or fmt['channels']!=event['channels']:raise ValueError('音频格式变化')
            samples.append(base64.b64decode(event['pcm_base64'],validate=True))
        result=await BroadcastVoiceService(self.config.model_copy(update={'auto_start':False})).synthesize(row['text'],row['emotion'] or 'neutral',audio_sink=sink)
        if result.get('state')!='ready' or not samples:raise ValueError('语音未生成')
        path=self.root/(row['id']+'.wav')
        with wave.open(str(path),'wb') as wav:
            wav.setnchannels(fmt['channels']);wav.setsampwidth(2);wav.setframerate(fmt['rate']);wav.writeframes(b''.join(samples))
        return path

    async def play(self,row,path):
        import json,os,subprocess,time
        import psutil
        from osu_telemetry_worker import publish_latest
        from runtime_paths import module_command
        if self.process is None:
            command=module_command('live-audio-sink')+[str(self.home),str(self.root),str(os.getpid()),str(psutil.Process().create_time()),str(self.database)]
            self.process=subprocess.Popen(command,cwd=self.home,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if self.process.poll() is not None:return 'unknown'
        response=self.root/'audio-response.json';response.unlink(missing_ok=True)
        publish_latest(self.root/'audio-request.json',{'id':row['id'],'path':str(path)})
        deadline=time.monotonic()+max(30,self.config.max_duration_seconds+20)
        while time.monotonic()<deadline and self.process.poll() is None:
            try:
                data=json.loads(response.read_text(encoding='utf8'))
                if data.get('id')==row['id']:return data.get('state','unknown')
            except (OSError,ValueError):pass
            await asyncio.sleep(.1)
        return 'unknown'

    async def close(self):
        from voice_engine import _stop_owned_process
        (self.root/'stop.request').touch()
        if self.process:
            try:await asyncio.to_thread(self.process.wait,5)
            except Exception:
                self.process.terminate()
                try:await asyncio.to_thread(self.process.wait,5)
                except Exception:pass
        if self.engine_process is not None:
            await asyncio.to_thread(_stop_owned_process,self.engine_process)
