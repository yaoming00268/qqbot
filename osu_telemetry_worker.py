"""Read-only, bounded osu telemetry worker feeding the project's agent mailbox."""
from __future__ import annotations
import argparse, asyncio, ctypes, json, math, os, sys, time
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlsplit
import aiohttp, psutil
MAX_SAMPLE_AGE_SECONDS=3
from agent_mailbox import AgentMailbox
from console_tools.broker import ConsoleBroker

ALLOWED_PROCESSES={'osu!.exe','osu.exe'}

def foreground_matches(pid, created):
    if os.name!='nt':return False
    u=ctypes.windll.user32;h=u.GetForegroundWindow();p=wintypes.DWORD();u.GetWindowThreadProcessId(h,ctypes.byref(p))
    if p.value!=pid or not u.IsWindowVisible(h) or u.IsIconic(h):return False
    try:
        proc=psutil.Process(pid)
        return proc.name().lower() in ALLOWED_PROCESSES and abs(proc.create_time()-created)<0.01
    except psutil.Error:return False

def bound_process_matches(pid,created,executable):
    try:
        p=psutil.Process(pid)
        return p.is_running() and p.name().lower() in ALLOWED_PROCESSES and abs(p.create_time()-created)<0.01 and str(Path(p.exe()).resolve()).casefold()==str(Path(executable).resolve()).casefold()
    except (psutil.Error,OSError):return False

def normalize(data,seq,now,follow_bound=False):
    game=data.get('game') or {};play=data.get('play') or {};combo=play.get('combo') or {};beat=data.get('beatmap') or {};tm=beat.get('time') or {};state=data.get('state') or {};mode=play.get('mode') or {}
    if not isinstance(game,dict) or (not follow_bound and game.get('focused') is not True) or str(data.get('client','')).lower() not in ('stable','lazer'):return None
    def number(value,lo,hi):
        return value if isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value) and lo<=value<=hi else None
    raw={'score':number(play.get('score'),0,1e12),'accuracy':number(play.get('accuracy'),0,100),'combo':number(combo.get('current'),0,1e7),'map_time_ms':number(tm.get('live'),0,1e9)}
    if state.get('name')!='play':raw={key:None for key in raw}
    result={'version':1,'sequence':seq,'sampled_at':now,'source':'tosu_v2','client':str(data.get('client',''))[:30], 'state':str(state.get('name',''))[:30], 'mode':str(mode.get('name',''))[:30], 'paused':game.get('paused') is True,'focused':game.get('focused') is True,'values':{k:v for k,v in raw.items() if v is not None},'validity':{k:v is not None for k,v in raw.items()}}
    return result

class Gate:
    def __init__(self,cooldown=35):self.cooldown=cooldown;self.last=None;self.at=0
    def accept(self,item,now):
        if not item:return False
        signature=(item['state'],item['mode'],item['paused'],int(item['values'].get('combo',0)//100),int(item['values'].get('accuracy',0)//5))
        if self.last==signature or now-self.at<self.cooldown:return False
        self.last,self.at=signature,now;return True

def publish_latest(target,item,*,sleep=time.sleep,stopped=lambda:False):
    import uuid
    target=Path(target);tmp=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        tmp.write_text(json.dumps(item,ensure_ascii=False),encoding='utf8')
        for attempt in range(10):
            if stopped():raise InterruptedError('停止请求')
            try:os.replace(tmp,target);return
            except OSError as exc:
                if getattr(exc,'winerror',None) not in (5,32) and not isinstance(exc,PermissionError):raise
                if attempt==9:raise
                sleep(.02)
    finally:tmp.unlink(missing_ok=True)

async def collect(url,pid,created,mailbox,agent,message,worker_token,duration=75,stop_file="",latest_file="",follow_bound=False,executable="",resident=False,live_broker=None,live_namespace="",live_run=""):
    parsed=urlsplit(url)
    if parsed.scheme!='ws' or parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.path!='/websocket/v2' or parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError('仅允许本机 tosu v2 只读数据端点')
    deadline=float("inf") if resident else time.monotonic()+min(90,max(1,duration));gate=Gate();seq=0;reports=0;first=None;started=time.monotonic()
    from osu_map_end import MapEndDetector, from_tosu_v2
    map_detector=MapEndDetector()
    try:
        async with aiohttp.ClientSession(trust_env=False,timeout=aiohttp.ClientTimeout(total=None,sock_connect=3)) as client:
            async with client.ws_connect(url,heartbeat=10,max_msg_size=2*1024*1024) as ws:
                while time.monotonic()<deadline:
                    if stop_file and Path(stop_file).exists():break
                    if not (bound_process_matches(pid,created,executable) if follow_bound else foreground_matches(pid,created)):break
                    with mailbox.broker.connect() as db:
                        active=db.execute("SELECT state FROM agent_mail WHERE seq=?",(message["seq"],)).fetchone()
                    if not active or active[0]!="running":break
                    
                    try:msg=await ws.receive(timeout=min(2,max(.01,deadline-time.monotonic())))
                    except asyncio.TimeoutError:continue
                    if msg.type!=aiohttp.WSMsgType.TEXT:break
                    sampled=time.time();seq+=1
                    try:
                        raw=json.loads(msg.data)
                        item=normalize(raw,seq,sampled,follow_bound)
                    except (ValueError,TypeError,AttributeError):continue
                    if not (bound_process_matches(pid,created,executable) if follow_bound else foreground_matches(pid,created)):break
                    if live_broker is not None:
                        snapshot=from_tosu_v2(raw)
                        if snapshot: snapshot["sampled_at"] = sampled
                        event=map_detector.observe(snapshot) if snapshot else None
                        if event:
                            from runtime_paths import application_home
                            from live_room import live_now
                            if not await live_now(application_home()): continue
                            from live_events import enqueue
                            enqueue(live_broker, namespace=live_namespace, run=live_run, worker=agent["id"],
                                    attempt=str(event["sequence"]), summary=event)
                    if item and latest_file:
                        item.update(agent_id=agent['id'],task_id=message['task_id'],worker_pid=os.getpid())
                        try:publish_latest(latest_file,item,stopped=lambda:bool(stop_file and Path(stop_file).exists()))
                        except InterruptedError:
                            if stop_file and Path(stop_file).exists():break
                            raise
                    if gate.accept(item,time.monotonic()):
                        if not resident and reports>=2:continue
                        published=time.time();item['worker_pid']=os.getpid();item['delivery_ms']=round((published-sampled)*1000,2)
                        mailbox.operate(worker_token,'report',text=json.dumps(item,ensure_ascii=False),request_key=f'telemetry:{message["seq"]}:{seq}',finish=False)
                        reports+=1;first=first or item
    except BaseException:
        mailbox.complete(agent,message,'failed','本地遥测读取中断。',payload={'code':'TELEMETRY_FAILED','samples':seq,'reports':reports})
        raise
    else:
        mailbox.complete(agent,message,'completed','本地遥测观察已结束。',payload={'kind':'telemetry_done','samples':seq,'reports':reports})
    return {'samples':seq,'reports':reports,'first':first}

def main():
    p=argparse.ArgumentParser();p.add_argument('--worker',action='store_true');p.add_argument('--db');p.add_argument('--agent-id');p.add_argument('--url',default='ws://127.0.0.1:24050/websocket/v2');p.add_argument('--pid',type=int);p.add_argument('--created',type=float);p.add_argument('--duration',type=int,default=75);p.add_argument('--stop-file',default='');p.add_argument('--latest-file',default='');p.add_argument('--follow-bound-game',action='store_true');p.add_argument('--executable',default='');p.add_argument('--resident',action='store_true');p.add_argument('--live-db',default='');p.add_argument('--live-namespace',default='');p.add_argument('--live-run',default='');a=p.parse_args()
    if not a.worker:raise SystemExit('此模块由本地看客入口启动。')
    broker=ConsoleBroker(a.db,initialize=False);mailbox=AgentMailbox(broker);agent=mailbox.agent(a.agent_id);message=mailbox.claim(a.agent_id)
    if not message:raise SystemExit('没有可领取的子代理任务。')
    workspace=mailbox.workspace(agent);workspace.mkdir(parents=True,exist_ok=True)
    meta={'namespace':agent['namespace'],'scope':agent['scope'],'session_key':agent['parent_key'],'generation':agent['generation'],
          'agent_worker_id':agent['id'],'agent_input_id':message['seq'],'main_agent':False,'is_master':False,'is_admin':False,
          'sender_id':'','source_message_id':'','output_dir':str(workspace),'artifact_roots':[str(workspace)],'work_permissions':{'access':'disabled'}}
    context=broker.issue_context(meta,workspace/'.contexts');worker_token=json.loads(Path(context).read_text(encoding='utf-8'))['token']
    live_broker=None
    if a.live_db:
        if not a.live_namespace.startswith("onebot:") or not a.live_run: raise ValueError("直播队列绑定不完整")
        live_broker=ConsoleBroker(a.live_db, initialize=False)
    result=asyncio.run(collect(a.url,a.pid,a.created,mailbox,agent,message,worker_token,a.duration,a.stop_file,a.latest_file,a.follow_bound_game,a.executable,a.resident,live_broker,a.live_namespace,a.live_run));print(json.dumps(result,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
