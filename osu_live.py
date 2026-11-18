"""Explicit osu live telemetry host; all conversation work stays in the main Bot."""
from __future__ import annotations
import argparse,hashlib,json,os,shutil,subprocess,tempfile,time,uuid
from pathlib import Path
import psutil
from config import AppConfig
from console_tools.broker import ConsoleBroker
from osu_spectator import foreground,start_tosu,stop_tosu,submit,worker_command
from osu_telemetry_worker import bound_process_matches,publish_latest
from runtime_paths import application_home
from vm_protocol import file_lock
from live_events import storage_root

def read(path):
    try:return json.loads(Path(path).read_text(encoding='utf8'))
    except (OSError,ValueError):return {}

def main_bot(home, namespace, database):
    path=storage_root(home,AppConfig.load(str(home/'config.yaml')))/(hashlib.sha256(namespace.encode()).hexdigest()[:16]+'.json')
    item=read(path)
    try:
        p=psutil.Process(item['pid'])
        return item.get('namespace')==namespace and Path(item['database']).resolve()==database.resolve() and abs(p.create_time()-item['created'])<.01 and 0<=time.time()-item['heartbeat']<12
    except (KeyError,TypeError,ValueError,psutil.Error):return False

def run(home):
    home=Path(home).resolve();config=AppConfig.load(str(home/'config.yaml'));root=storage_root(home,config);root.mkdir(parents=True,exist_ok=True)
    with file_lock(root/'instance.lock',timeout=0):
        target=None
        namespace='onebot:'+str(config.onebot.active_account)
        db_path=Path(config.bot.runtime_database_path)
        database=((db_path if db_path.is_absolute() else home/db_path).resolve().parent/'main_chat/console.sqlite3')
        run_id=uuid.uuid4().hex;out=root/run_id;out.mkdir();temp=Path(tempfile.mkdtemp(prefix='qqbot-osu-live-'))
        control=root/'control.json';publish_latest(control,{'enabled':True,'stop':False,'run':run_id})
        status={'run':run_id,'host_pid':os.getpid(),'host_created':psutil.Process().create_time(),'state':'waiting_game','heartbeat':time.time()}
        def save():publish_latest(root/'status.json',status)
        save();deadline=time.monotonic()+60
        while time.monotonic()<deadline and read(control).get('enabled') is True:
            target=foreground()
            if target:break
            status['heartbeat']=time.time();save();time.sleep(.5)
        if not target:
            stopped=read(control).get('stop') is True
            publish_latest(control,{'enabled':False,'stop':True,'run':run_id});status['state']='stopped' if stopped else 'game_timeout';save();shutil.rmtree(temp,ignore_errors=True);return
        exe=psutil.Process(target[0]).exe()
        status['state']='waiting_bot'
        save();owned=None;proc=None;mail=None;token=None;task=None;log=None
        try:
            while not (out/'stop.request').exists() and read(control).get('enabled') is True:
                if not bound_process_matches(*target,exe):status['state']='game_closed';break
                status['heartbeat']=time.time()
                if not proc and main_bot(home,namespace,database):
                    owned=start_tosu(home);broker=ConsoleBroker(temp/'console.sqlite3');mail,token,task=submit(broker)
                    command=worker_command(home,broker.path,task['id'],'ws://127.0.0.1:24050/websocket/v2',*target,75,out/'stop.request',True,exe,True)
                    command+=['--live-db',str(database),'--live-namespace',namespace,'--live-run',run_id]
                    log=(out/'worker-error.log').open('ab');proc=subprocess.Popen(command,cwd=temp,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    status['worker_pid']=proc.pid
                status['state']='watching' if proc and main_bot(home,namespace,database) else 'waiting_bot'
                if proc and proc.poll() is not None:status.update(state='worker_stopped',worker_exit=proc.returncode);break
                save();time.sleep(.5)
        finally:
            publish_latest(control,{'enabled':False,'stop':True,'run':run_id});(out/'stop.request').touch()
            if mail and task:
                try:mail.cancel_task(token,task['task_id'])
                except Exception:pass
            if proc:
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)
            if log:log.close()
            stop_tosu(owned);shutil.rmtree(temp,ignore_errors=True)
            if status['state'] not in ('game_closed','worker_stopped'):status['state']='stopped'
            status['heartbeat']=time.time();save()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=('start','stop','status'));args=parser.parse_args()
    home=application_home();config=AppConfig.load(str(home/'config.yaml'));root=storage_root(home,config);root.mkdir(parents=True,exist_ok=True)
    if args.operation=='start':run(home);return
    status=read(root/'status.json')
    if args.operation=='stop':
        publish_latest(root/'control.json',{'enabled':False,'stop':True,'run':status.get('run','')})
        if status.get('run'):
            target=(root/status['run']).resolve()
            if target.parent == root.resolve() and target.is_dir(): (target/'stop.request').touch()
        print('已请求停止；不会启动或关闭直播。');return
    status=status or {'state':'stopped'}
    room=read(root/'room-status.json');fresh=0 <= time.time()-room.get('checked_at',0) < 30
    status['room_state']=room.get('state','unknown') if fresh else 'unknown'
    status['enabled']=read(root/'control.json').get('enabled') is True
    print(json.dumps(status or {'state':'stopped'},ensure_ascii=False))
if __name__=='__main__':main()
