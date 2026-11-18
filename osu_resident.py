"""Bounded-rate resident osu spectator; explicit stop and owned-process cleanup."""
from __future__ import annotations
import argparse,asyncio,json,os,shutil,socket,subprocess,sys,tempfile,time,uuid
from pathlib import Path
import psutil
from config import AppConfig
from console_tools.broker import ConsoleBroker
from osu_spectator import Journal,foreground,start_tosu,stop_tosu,submit,worker_command,classify_notice,main_turn
from osu_telemetry_worker import bound_process_matches
from runtime_paths import application_home
from vm_protocol import file_lock
from voice_engine import ready,stop_engine

class EventGate:
 def __init__(self,cooldown=40,limit=30):self.cooldown=cooldown;self.limit=limit;self.last=0;self.calls=[]
 def allow(self,now):
  self.calls=[x for x in self.calls if now-x<3600]
  return (not self.last or now-self.last>=self.cooldown) and len(self.calls)<self.limit
 def mark(self,now):self.last=now;self.calls.append(now)

def run(home,pid=0):
 home=Path(home).resolve();root=home/'data/osu-resident';root.mkdir(parents=True,exist_ok=True)
 with file_lock(root/'instance.lock',timeout=0):
  previous=root/'current.json'
  if previous.exists():
   old=Journal(previous).read()
   if old.get('state') in ('stop_unknown','worker_stopped') and old.get('unconfirmed_turns'):raise RuntimeError('上次主代理停止未确认，禁止新会话。')
  target=foreground()
  if pid:
   p=psutil.Process(pid)
   if p.name().lower() not in ('osu!.exe','osu.exe'):raise ValueError('指定进程不是 osu!')
   target=(pid,p.create_time())
  if not target:raise ValueError('请先将 osu! 置于前台。')
  executable=psutil.Process(target[0]).exe();out=root/uuid.uuid4().hex;out.mkdir();temp=Path(tempfile.mkdtemp(prefix='qqbot-osu-resident-')).resolve();oldcwd=Path.cwd();os.chdir(temp)
  state={'run':str(out),'host_pid':os.getpid(),'host_created':psutil.Process().create_time(),'state':'starting','comments':0,'turns':0,'heartbeat':time.time()};journal=Journal(out/'status.json');journal.write(state);Journal(previous).write(state)
  config=AppConfig.load(str(home/'config.yaml'));voice=config.broadcast_voice;voice.enabled=True;voice.auto_start=True;voice.default_language='zh';voice.delivery_mode='validated';voice.cache_dir=str(temp/'voice-cache')
  for key in ('catalog_path','engine_python','engine_directory','engine_config'):
   p=Path(getattr(voice,key));setattr(voice,key,str(p if p.is_absolute() else (home/p).resolve()))
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));voice.endpoint='http://127.0.0.1:'+str(sock.getsockname()[1])
  owned=None;proc=None;audio_proc=None;life={};mail=None;token=None;task=None;gate=EventGate()
  try:
   owned=start_tosu(home);engine=asyncio.run(ready(voice))
   if not engine.get('owned') or engine.get('reused'):raise RuntimeError('语音引擎归属不明')
   if getattr(sys,'frozen',False):audio_cmd=[sys.executable,'--home',str(home),'--module','osu-resident-audio']
   else:audio_cmd=[sys.executable,'-X','utf8',str(Path(__file__).with_name('osu_resident_audio.py'))]
   audio_proc=subprocess.Popen(audio_cmd+[str(out),str(target[0]),str(target[1]),executable],cwd=temp,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
   deadline=time.monotonic()+10
   while not (out/'audio-ready.json').exists() and time.monotonic()<deadline and audio_proc.poll() is None:time.sleep(.1)
   if not (out/'audio-ready.json').exists():raise RuntimeError('音频进程未就绪')
   broker=ConsoleBroker(temp/'console.sqlite3');mail,token,task=submit(broker);cmd=worker_command(home,broker.path,task['id'],'ws://127.0.0.1:24050/websocket/v2',*target,75,out/'stop.request',True,executable,True)
   worker_log=(out/'worker-error.log').open('ab');proc=subprocess.Popen(cmd,cwd=temp,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=worker_log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));state.update(worker_pid=proc.pid,worker_created=psutil.Process(proc.pid).create_time(),agent_id=task['id'],task_id=task['task_id'],state='watching');journal.write(state);Journal(previous).write(state)
   while not (out/'stop.request').exists():
    state['heartbeat']=time.time()
    if not bound_process_matches(*target,executable):state['state']='game_closed';break
    if proc.poll() is not None:state.update(state='worker_stopped',worker_exit_code=proc.returncode);break
    rows=mail.pending('local:osu','__work__:osu_telemetry',0);valid=[]
    for row in rows:
     kind,item=classify_notice(row,task['id'],task['task_id'])
     if kind=='telemetry' and 0<=time.time()-item['sampled_at']<=3:valid.append(row)
     else:mail.acknowledge([row])
    if len(valid)>1:mail.acknowledge(valid[:-1]);valid=valid[-1:]
    if valid and gate.allow(time.monotonic()):
     mail.acknowledge(valid[:-1]);turn=out/('turn-'+uuid.uuid4().hex);turn.mkdir();state.update(state='main',turns=state['turns']+1,last_event=time.time());journal.write(state);Journal(previous).write(state);life={'idle_confirmed':False,'cid':''};gate.mark(time.monotonic())
     try:asyncio.run(main_turn(config,broker,task,home,turn,target,life,state['turns']==1,True,executable,out))
     except Exception as e:state['last_error']=type(e).__name__
     events=[json.loads(x) for x in (turn/'main-audit.jsonl').read_text(encoding='utf8').splitlines()] if (turn/'main-audit.jsonl').exists() else []
     if not (turn/'terminal.flag').exists() and valid:mail.acknowledge(valid[-1:])
     if any(x.get('event')=='speak' for x in events):state['comments']+=1;state['last_speech']=next(x['text'] for x in events if x.get('event')=='speak')
     if not life.get('idle_confirmed'):state.update(state='stop_unknown',unconfirmed_turns=[turn.name]);break
     state['state']='watching'
     if events and any(x.get('event')=='speak' for x in events):gate.last=time.monotonic()
    elif valid and not gate.allow(time.monotonic()):
     mail.acknowledge(valid[:-1])
    journal.write(state);Journal(previous).write(state);time.sleep(.5)
  except BaseException as e:state.update(state='failed',last_error=type(e).__name__);raise
  finally:
   if mail and task:
    try:mail.cancel_task(token,task['task_id'])
    except Exception:pass
   (out/'stop.request').touch()
   if audio_proc:
    try:audio_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:audio_proc.terminate();audio_proc.wait(timeout=5)
   if proc:
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)
   if 'worker_log' in locals():worker_log.close()
   if proc:state['worker_exit_code']=proc.returncode
   try:state['engine_stopped']=stop_engine(voice).get('owned',False)
   except Exception:state['engine_stopped']=False
   stop_tosu(owned);state['worker_stopped']=not proc or proc.poll() is not None;state['heartbeat']=time.time()
   if state['state'] not in ('stop_unknown','failed','game_closed','worker_stopped'):state['state']='stopped'
   journal.write(state);Journal(previous).write(state);os.chdir(oldcwd)
   if state['state']!='stop_unknown':shutil.rmtree(temp,ignore_errors=True)
  return state

def main():
 p=argparse.ArgumentParser();p.add_argument('operation',choices=('start','status','stop'));p.add_argument('--pid',type=int,default=0);a=p.parse_args();home=application_home();root=home/'data/osu-resident';current=root/'current.json'
 if a.operation=='start':
  if current.exists():
   old=Journal(current).read()
   try:
    process=psutil.Process(old['host_pid'])
    if process.is_running() and abs(process.create_time()-old['host_created'])<.01:print(json.dumps({'already_running':True,**old},ensure_ascii=False));return
   except (psutil.Error,KeyError):pass
  print(json.dumps(run(home,a.pid),ensure_ascii=False));return
 state=Journal(current).read()
 if a.operation=='stop':(Path(state['run'])/'stop.request').write_text('stop',encoding='ascii');print('stop requested');return
 print(json.dumps(state,ensure_ascii=False))
if __name__=='__main__':main()
