"""Private osu telemetry spectator host, CLI start/status/stop."""
from __future__ import annotations
import argparse, asyncio, ctypes, hashlib, json, os, shutil, socket, subprocess, sys, tempfile, time, uuid, wave, winsound
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlsplit
import psutil
from agent_mailbox import AgentMailbox
from console_tools.broker import ConsoleBroker
from runtime_paths import application_home, module_command

TOSU_SHA256='bdca224c71af7949fd5a4aa4780973a0c5540f2eab5b1ccbd922840328d8587b'

def start_tosu(home):
    path=home/'third_party/tosu-4.26.3/tosu.exe'
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=TOSU_SHA256:raise ValueError('tosu 未安装或文件校验失败。')
    with socket.socket() as sock:
        sock.settimeout(.5)
        if sock.connect_ex(('127.0.0.1',24050))==0:raise ValueError('本地 24050 端口已被占用；请指定已有只读数据源 --url。')
    proc=subprocess.Popen([str(path)],cwd=path.parent,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));created=psutil.Process(proc.pid).create_time();deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if proc.poll() is not None:raise RuntimeError('tosu 启动失败。')
        conns=[c for c in psutil.net_connections(kind='tcp') if c.status=='LISTEN' and c.laddr.port==24050]
        if conns:
            if any(c.pid!=proc.pid or c.laddr.ip!='127.0.0.1' for c in conns):
                proc.terminate();proc.wait(timeout=5);raise RuntimeError('tosu 监听地址或进程不符。')
            return proc,created
        time.sleep(.2)
    proc.terminate();proc.wait(timeout=5);raise RuntimeError('tosu 未能在本机启动。')

def stop_tosu(owned):
    if not owned:return
    proc,created=owned
    try:
        p=psutil.Process(proc.pid)
        if abs(p.create_time()-created)<.01:p.terminate();p.wait(timeout=5)
    except psutil.Error:pass

class Journal:
    def __init__(self,path):self.path=Path(path)
    def write(self,data):
        from osu_telemetry_worker import publish_latest
        self.path.parent.mkdir(parents=True,exist_ok=True);publish_latest(self.path,data)
    def read(self):return json.loads(self.path.read_text(encoding='utf-8'))

def foreground():
    u=ctypes.windll.user32;h=u.GetForegroundWindow();pid=wintypes.DWORD();u.GetWindowThreadProcessId(h,ctypes.byref(pid))
    try:
        p=psutil.Process(pid.value)
        if p.name().lower() in ('osu!.exe','osu.exe') and u.IsWindowVisible(h) and not u.IsIconic(h):return pid.value,p.create_time()
    except psutil.Error:pass
    return None

def worker_command(home,db,agent_id,url,pid,created,duration,stop_file,follow_bound=False,executable="",resident=False):
    if getattr(sys,'frozen',False):cmd=[sys.executable,'--home',str(home),'--module','osu-telemetry-worker']
    else:cmd=[sys.executable,'-X','utf8',str(Path(__file__).with_name('osu_telemetry_worker.py'))]
    if follow_bound:cmd+=['--follow-bound-game','--executable',executable]
    if resident:cmd+=['--resident']
    return cmd+['--worker','--db',str(db),'--agent-id',agent_id,'--url',url,'--pid',str(pid),'--created',str(created),'--duration',str(duration),'--stop-file',str(stop_file),'--latest-file',str(Path(stop_file).with_name('latest.json'))]

def classify_notice(row,agent_id,task_id):
    if row.get('agent_id')!=agent_id or row.get('direction')!='to_main' or row.get('state')!='ready':return 'invalid',None
    try:
        payload=json.loads(row.get('payload') or '{}')
        sample=json.loads(row.get('text') or '')
        if payload.get('kind')=='progress' and isinstance(sample,dict) and sample.get('version')==1 and sample.get('source')=='tosu_v2' and sample.get('agent_id')==agent_id and sample.get('task_id')==task_id and isinstance(sample.get('sequence'),int) and isinstance(sample.get('sampled_at'),(float,int)):
            return 'telemetry',sample
    except (TypeError,ValueError):pass
    try:payload=json.loads(row.get('payload') or '{}')
    except (TypeError,ValueError):return 'invalid',None
    if payload.get('kind') in ('result','telemetry_done'):
        return 'completed' if payload.get('success') is True and payload.get('reports',0)>0 else 'source_failed',None
    return 'invalid',None

def submit(broker):
    mailbox=AgentMailbox(broker);ctx={'namespace':'local:osu','scope':'local:osu','session_key':'__work__:osu_telemetry','generation':0,'main_agent':True,'is_master':True,'sender_id':'local-owner','work_permissions':{'access':'all'}}
    context=broker.issue_context(ctx,broker.path.parent/'contexts');token=json.loads(Path(context).read_text(encoding='utf-8'))['token']
    result=mailbox.operate(token,'create',name='osu只读遥测',instructions='只读取授权的本机osu遥测并节流返回事实。',task_type='process',text='采集本地osu只读遥测并报告。',request_key='local-telemetry')
    return mailbox,token,result

async def main_turn(config,broker,task,source,out,focus,life,unmute_own=False,follow_bound=False,executable="",shared_root=None):
    from model_backend import create_ai_client
    from main_agent_policy import main_cascade_config
    from voice_engine import ready,stop_engine
    import socket
    voice=config.broadcast_voice;run_id=out.name;cfgpath=Path.cwd()/'config.yaml';config.save(str(cfgpath))
    ai=create_ai_client(config,sessions_file=str(Path.cwd()/'sessions.json'),account_key='osu_telemetry');original=ai._run_agentapi_cmd
    prior=ai._sessions.get('__work__:osu_telemetry_main')
    if prior and prior.conversation_id:life['cid']=prior.conversation_id;Journal(out/'private-lifecycle.json').write(life)
    async def track(args,timeout=None,*,cascade_config=None,on_created=None):
        def remember(cid):
            life['cid']=cid;Journal(out/'private-lifecycle.json').write(life)
            if on_created:on_created(cid)
        return await original(args,timeout=timeout,cascade_config=cascade_config,on_created=remember if args and args[0]=='new-conversation' else on_created)
    ai._run_agentapi_cmd=track
    if getattr(sys,'frozen',False):cmd=sys.executable;args=['--home',str(source),'--module','osu-telemetry-mcp']
    else:cmd=sys.executable;args=['-X','utf8',str(Path(__file__).with_name('osu_telemetry_mcp.py'))]
    if unmute_own:args+=['--unmute-own']
    if shared_root:args+=['--shared-root',str(shared_root)]
    if follow_bound:args+=['--follow-bound-game','--executable',executable]
    args+=['--config',str(cfgpath),'--db',str(broker.path),'--namespace','local:osu','--parent','__work__:osu_telemetry','--agent-id',task['id'],'--task-id',task['task_id'],'--run-id',run_id,'--output',str(out),'--pid',str(focus[0]),'--created',str(focus[1])]
    cascade=main_cascade_config();planner=cascade['plannerConfig'];server={'serverName':'osu-telemetry-'+run_id,'command':cmd,'args':args,'enabledTools':['telemetry_report','local_speak','finish'],'forceAllToolsEager':True,'skipToolNamePrefix':True};planner['customizationDiscoveryConfig']['mcp']['servers']=[server];planner['customizationConfig']['mcpServers']=[server]
    def stopped():
        try:return 'finish' if (out/'terminal.flag').read_text(encoding='utf-8')==run_id else False
        except OSError:return 'finish' if ((shared_root or out)/'stop.request').exists() else False
    started=time.monotonic();trace=[]
    try:
        try:
            await ai.chat('__work__:osu_telemetry_main','请读取 telemetry_report 中新鲜的子代理报告，选歌或菜单只评论状态、不评论得分；依据实际有效字段说一句以“刚才”开头的过去时短句，不使用“当前、现在、正在”等实时措辞，不超过20字，用 local_speak 播放；即使语音失败也请 finish。',system_prompt='你是中性的本地 osu! 观众；只依据报告，不猜测。',timeout=65,raise_on_error=True,allow_empty_response=True,bind_response=True,stop_requested=stopped,cascade_config=cascade,response_trace=trace)
        finally:pass
    finally:
        life['model_ms']=round((time.monotonic()-started)*1000,1);life['trace_tools']=[x.get('name') for r in trace for x in r.get('tool_calls',[]) if isinstance(x,dict)]
        session=ai._sessions.get('__work__:osu_telemetry_main')
        if session and session.conversation_id:life['cid']=session.conversation_id
        cid=life.get('cid')
        if cid:
            life['cancel_confirmed']=bool(await ai._cancel_native_conversation(cid));life['idle_confirmed']=bool(await ai._confirm_native_idle(cid,timeout=5))
        Journal(out/'private-lifecycle.json').write(life)
        if not life.get('idle_confirmed'):
            (out/'stop.request').write_text('stop',encoding='ascii')
        for process in psutil.process_iter(['pid','cmdline']):
            try:
                args=process.info['cmdline'] or []
                if run_id in args and ('osu-telemetry-mcp' in args or any(Path(str(x)).name=='osu_telemetry_mcp.py' for x in args)):
                    process.terminate();process.wait(timeout=5)
            except (psutil.Error,OSError):pass
    return life

def start(home,url,duration=75,unmute_own=False,follow_bound=False,pid_override=0):
    from config import AppConfig
    target=None;executable=''
    if pid_override:
        if not follow_bound:raise ValueError('--pid 需与 --follow-bound-game 一起使用。')
        p=psutil.Process(pid_override)
        if p.name().lower() not in ('osu!.exe','osu.exe'):raise ValueError('指定进程不是 osu!。')
        target=(p.pid,p.create_time());executable=p.exe()
    config=AppConfig.load(str(home/'config.yaml'));out=home/'data/osu-spectator'/uuid.uuid4().hex;out.mkdir(parents=True);temp=Path(tempfile.mkdtemp(prefix='qqbot-osu-telemetry-')).resolve();old=Path.cwd();os.chdir(temp)
    from voice_engine import ready,stop_engine
    import socket
    voice=config.broadcast_voice;voice.enabled=True;voice.auto_start=True;voice.default_language='zh';voice.delivery_mode='validated';voice.cache_dir=str(temp/'voice-cache')
    for key in ('catalog_path','engine_python','engine_directory','engine_config'):
        path=Path(getattr(voice,key));setattr(voice,key,str(path if path.is_absolute() else (home/path).resolve()))
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));voice_port=sock.getsockname()[1]
    voice.endpoint='http://127.0.0.1:'+str(voice_port);warm_started=time.monotonic()
    try:engine=asyncio.run(ready(voice))
    except BaseException:
        stop_engine(voice);os.chdir(old);shutil.rmtree(temp,ignore_errors=True);raise
    warm_ms=round((time.monotonic()-warm_started)*1000,1)
    if not engine.get('owned') or engine.get('reused'):
        os.chdir(old);raise RuntimeError('语音引擎归属不明；未停止已有服务。')
    print('语音已就绪，等待 osu! 处于前台（最多120秒）。',flush=True)
    deadline=time.monotonic()+120
    try:
        while time.monotonic()<deadline:
            target=target or foreground()
            if target:
                executable=psutil.Process(target[0]).exe();break
            time.sleep(.5)
        if not target:raise ValueError('120秒内没有前台 osu!；未启动遥测或主代理。')
    except BaseException:
        stop_engine(voice);os.chdir(old);shutil.rmtree(temp,ignore_errors=True);raise
    broker=ConsoleBroker(temp/'console.sqlite3');mailbox,token,task=submit(broker);proc=subprocess.Popen(worker_command(home,broker.path,task['id'],url,*target,duration,out/'stop.request',follow_bound,executable),cwd=temp,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));state={'voice_warm_ms':warm_ms,'agent_id':task['id'],'task_id':task['task_id'],'worker_pid':proc.pid,'worker_started':psutil.Process(proc.pid).create_time(),'state':'running','output':str(out),'samples':0};journal=Journal(out/'status.json');journal.write(state);life={'idle_confirmed':False,'cid':''};started=time.monotonic()
    try:
        while time.monotonic()-started<duration:
            if (out/'stop.request').exists():state['state']='stopped';break
            from osu_telemetry_worker import bound_process_matches
            if not (bound_process_matches(*target,executable) if follow_bound else foreground()==target):state['state']='paused';break
            pending=mailbox.pending('local:osu','__work__:osu_telemetry',0)
            if pending:
                row=pending[0];kind,sample=classify_notice(row,task['id'],task['task_id'])
                if kind=='source_failed':state['state']='source_failed';state['error']='遥测子代理未返回有效报告。';break
                if kind!='telemetry':state['state']='invalid_report';state['error']='子代理报告格式或归属无效。';break
                age=time.time()-sample['sampled_at']
                if 0<=age<=3 and row['agent_id']==task['id']:
                    state.update(state='main',mail_seq=row['seq'],summary=sample,delivery_ms=round(age*1000,1));journal.write(state);print(json.dumps({'state':'main','run':str(out),'task_id':task['task_id']},ensure_ascii=False),flush=True)
                    asyncio.run(main_turn(config,broker,task,home,out,target,life,unmute_own,follow_bound,executable));state['main']={k:v for k,v in life.items() if k!='cid'};events=[json.loads(x) for x in (out/'main-audit.jsonl').read_text(encoding='utf-8').splitlines()] if (out/'main-audit.jsonl').exists() else [];state['state']='completed' if life.get('idle_confirmed') and any(x.get('event')=='speak' for x in events) and any(x.get('event')=='finish' for x in events) else 'incomplete';journal.write(state);break
                mailbox.acknowledge([row])
            if proc.poll() is not None:state['state']='no_fresh_report';break
            time.sleep(.15)
    except BaseException:
        state['state']='interrupted';raise
    finally:
        life['engine_stopped']=stop_engine(voice).get('owned',False)
        mailbox.cancel_task(token,task['task_id'])
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Stop only the verified child process created by this invocation.
            if psutil.pid_exists(proc.pid) and abs(psutil.Process(proc.pid).create_time()-state['worker_started'])<.01:proc.terminate();proc.wait(timeout=5)
        state.update(worker_stopped=proc.poll() is not None,idle_confirmed=life.get('idle_confirmed',False) if life.get('cid') else 'not_applicable',engine_stopped=life.get('engine_stopped',False),elapsed=round(time.monotonic()-started,2));journal.write(state)
        os.chdir(old)
        if life.get('idle_confirmed') or not life.get('cid'):shutil.rmtree(temp,ignore_errors=True)
    return state

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('operation',choices=('start','status','stop'));p.add_argument('--url',default='');p.add_argument('--duration',type=int,default=75);p.add_argument('--run',default='');p.add_argument('--unmute-own',action='store_true',help='仅本轮播放进程初始化时解除其自身音频会话静音');p.add_argument('--follow-bound-game',action='store_true',help='跟随绑定的 osu 进程，不因失焦停止');p.add_argument('--pid',type=int,default=0);a=p.parse_args();home=application_home()
    if a.operation=='start':
        owned=None
        deadline=time.monotonic()+120
        while not a.pid and foreground() is None and time.monotonic()<deadline:time.sleep(1)
        if not a.pid and foreground() is None:raise ValueError('120 秒内没有前台 osu!，未启动看客。')
        try:
            if not a.url:owned=start_tosu(home)
            print(json.dumps(start(home,a.url or 'ws://127.0.0.1:24050/websocket/v2',min(90,max(10,a.duration)),a.unmute_own,a.follow_bound_game,a.pid),ensure_ascii=False))
        finally:stop_tosu(owned)
        return
    path=Path(a.run).resolve();root=(home/'data/osu-spectator').resolve()
    if not path.is_relative_to(root):raise ValueError('运行目录不属于本机看客')
    state=Journal(path/'status.json').read()
    if a.operation=='stop':
        (path/'stop.request').write_text('stop',encoding='ascii');print(json.dumps({'stop_requested':True}));return
    print(json.dumps(state,ensure_ascii=False))
if __name__=='__main__':main()
