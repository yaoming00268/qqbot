from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import pytest
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from live_events import enqueue,claim,recover,ROOT,ROOM

@pytest.fixture
def live(tmp_path):
    broker=ConsoleBroker(tmp_path/'console.sqlite3')
    def put(attempt,score):
        return enqueue(broker,namespace='onebot:test',run='run',worker='worker',attempt=attempt,
            summary={'type':'map_finished','score':score})
    return broker,put,tmp_path

def test_two_maps_and_duplicate(live):
    broker,put,_=live;a=put('attempt1',1);assert put('attempt1',1)==a;b=put('attempt2',2)
    with pytest.raises(ValueError):put('attempt1',3)
    assert claim(broker,'onebot:test',7)['id']==a
    assert claim(broker,'onebot:test',7) is None
    with broker.connect() as db:db.execute("UPDATE live_events SET state='done' WHERE id=?",(a,))
    assert claim(broker,'onebot:test',7)['id']==b

def test_live_tool_bound_and_qq_rejected(live):
    broker,put,tmp=live;event=put('a',1);claim(broker,'onebot:test',3)
    path=broker.issue_context({'namespace':'onebot:test','scope':'live:'+ROOM,'session_key':ROOT,
        'idle_event':True,'live_event':True,'event_id':event,'root':ROOT,'room':ROOM,'generation':3},tmp/'ctx')
    receipt=execute('live-say',path,text='测试台词',request_key='one')
    assert receipt['state']=='queued'
    assert execute('live-say',path,text='测试台词',request_key='one')['id']==receipt['id']
    for action in ('reply','message','commit','deliver','send','broadcast'):
        with pytest.raises((PermissionError,ValueError)):execute(action,path,text='泄漏')
    execute('finish',path)
    with pytest.raises(Exception):execute('live-say',path,text='过期',request_key='two')
    with broker.connect() as db:
        assert db.execute('SELECT count(*) FROM outgoing').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM live_outgoing').fetchone()[0]==1

def test_unknown_is_never_reclaimed(live):
    broker,put,_=live;put('a',1);claim(broker,'onebot:test',0);recover(broker,'onebot:test')
    assert claim(broker,'onebot:test',0) is None
    with broker.connect() as db:assert db.execute('SELECT state FROM live_events').fetchone()[0]=='unknown'
import asyncio
from live_playback import drain_one

def test_closed_cancels_without_render_or_play(live):
    broker,put,tmp=live;event=put('a',1);claim(broker,'onebot:test',3)
    path=broker.issue_context({'namespace':'onebot:test','scope':'live:'+ROOM,'session_key':ROOT,
        'idle_event':True,'live_event':True,'event_id':event,'root':ROOT,'room':ROOM,'generation':3},tmp/'ctx')
    execute('live-say',path,text='一句',request_key='one')
    calls=[]
    async def offline():return False
    async def render(row):calls.append('render')
    async def play(row,path):calls.append('play')
    assert asyncio.run(drain_one(broker,'onebot:test',offline,lambda row:True,render,play))
    assert calls==[]
    with broker.connect() as db:assert db.execute('SELECT state FROM live_outgoing').fetchone()[0]=='cancelled'

def test_close_during_synthesis_never_plays(live):
    broker,put,tmp=live;event=put('a',1);claim(broker,'onebot:test',3)
    path=broker.issue_context({'namespace':'onebot:test','scope':'live:'+ROOM,'session_key':ROOT,
        'idle_event':True,'live_event':True,'event_id':event,'root':ROOT,'room':ROOM,'generation':3},tmp/'ctx')
    execute('live-say',path,text='一句',request_key='one');calls=[]
    checks=iter([True,False])
    async def available():return next(checks)
    async def render(row):calls.append('render');return 'wav'
    async def play(row,path):calls.append('play')
    asyncio.run(drain_one(broker,'onebot:test',available,lambda row:True,render,play))
    assert calls==['render']
    with broker.connect() as db:assert db.execute('SELECT state FROM live_outgoing').fetchone()[0]=='cancelled'

def test_wait_three_and_new_activity(live):
    from live_events import enqueue_idle,record_idle_result,reset_activity
    broker,put,tmp=live;namespace='onebot:test'
    assert enqueue_idle(broker,namespace,now=1000,interval=300) is None
    for i in range(3):
        event=enqueue_idle(broker,namespace,now=1300+i*301,interval=300)
        assert event
        claim(broker,namespace,3)
        path=broker.issue_context({'namespace':namespace,'scope':'live:'+ROOM,'session_key':ROOT,
            'idle_event':True,'live_event':True,'event_id':event,'root':ROOT,'room':ROOM,'generation':3},tmp/'ctx')
        execute('wait',path)
        record_idle_result(broker,namespace,event)
        with broker.connect() as db:db.execute("UPDATE live_events SET state='done' WHERE id=?",(event,))
    assert enqueue_idle(broker,namespace,now=3000,interval=300) is None
    reset_activity(broker,namespace)
    with broker.connect() as db:assert db.execute('SELECT wait_count FROM live_state').fetchone()[0]==0

def test_analysis_supersedes_unplayed_idle(live):
    from live_events import enqueue_idle
    broker,put,tmp=live;namespace='onebot:test'
    enqueue_idle(broker,namespace,now=1000);event=enqueue_idle(broker,namespace,now=1301)
    claim(broker,namespace,3)
    path=broker.issue_context({'namespace':namespace,'scope':'live:'+ROOM,'session_key':ROOT,
        'idle_event':True,'live_event':True,'event_id':event,'root':ROOT,'room':ROOM,'generation':3},tmp/'ctx')
    receipt=execute('live-say',path,text='闲聊',request_key='one')
    with broker.connect() as db:db.execute("UPDATE live_outgoing SET state='rendering' WHERE id=?",(receipt['id'],))
    put('new-map',10)
    with broker.connect() as db:assert db.execute('SELECT state FROM live_outgoing').fetchone()[0]=='superseded'


def test_custom_runtime_root_is_shared_by_control_and_sink(tmp_path):
    from config import AppConfig
    from live_events import storage_root
    from live_playback import OwnedAudio
    cfg=AppConfig();cfg.bot.runtime_database_path=str(tmp_path/'custom'/'runtime.sqlite3')
    assert storage_root(tmp_path,cfg)==tmp_path/'custom'/'osu-live'
    audio=OwnedAudio(tmp_path,cfg.broadcast_voice,tmp_path/'custom'/'main_chat'/'console.sqlite3')
    assert audio.root.parent==storage_root(tmp_path,cfg)


def test_cancel_waits_for_start_thread_and_reclaims_owned_process(tmp_path):
    import threading
    from unittest.mock import patch
    import aiohttp
    from config import AppConfig
    from live_playback import OwnedAudio
    cfg=AppConfig();audio=OwnedAudio(tmp_path,cfg.broadcast_voice,tmp_path/'data/main_chat/console.sqlite3')
    entered=threading.Event();release=threading.Event();owned=object();stopped=[]
    class Session:
        def __init__(self,*args,**kwargs):pass
        async def __aenter__(self):raise aiohttp.ClientConnectionError()
        async def __aexit__(self,*args):pass
    def start(config,*,on_started):
        entered.set();release.wait(5)
        data={'pid':123,'create_time':456,'endpoint':config.endpoint,'owned':True,'running':True}
        on_started(owned,data);return data
    async def exercise():
        with patch('voice_engine.engine_status',return_value={'running':False}),patch('voice_engine.start_engine',side_effect=start),patch('voice_engine.aiohttp.ClientSession',Session),patch('voice_engine._stop_owned_process',side_effect=lambda p:stopped.append(p)):
            task=asyncio.create_task(audio.render({'text':'测试','emotion':'neutral'}))
            assert await asyncio.to_thread(entered.wait,3)
            task.cancel();await asyncio.sleep(.05);assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):await task
            await audio.close()
    asyncio.run(exercise());assert stopped and all(p is owned for p in stopped)


def test_registration_failure_stops_direct_owned_process(tmp_path):
    from unittest.mock import patch,MagicMock
    from config import AppConfig
    import voice_engine
    cfg=AppConfig().broadcast_voice
    python=tmp_path/'python.exe';python.touch();directory=tmp_path/'engine';directory.mkdir()
    (directory/'api_v2.py').touch();settings=tmp_path/'config.yaml';settings.touch()
    cfg.engine_python=str(python);cfg.engine_directory=str(directory);cfg.engine_config=str(settings);cfg.endpoint='http://127.0.0.1:45678'
    process=MagicMock();process.pid=123;owned=MagicMock();owned.create_time.return_value=456
    registry=tmp_path/'state.json';old_write=type(registry).write_text
    def failing(path,*args,**kwargs):
        if path==registry.with_suffix('.tmp'):raise OSError('fixture registry failure')
        return old_write(path,*args,**kwargs)
    with patch('voice_engine.engine_status',return_value={'running':False}),patch('voice_engine.state_path',return_value=registry),patch('voice_engine.subprocess.Popen',return_value=process),patch('psutil.Process',return_value=owned),patch.object(type(registry),'write_text',failing),patch('voice_engine._stop_owned_process') as stop:
        with pytest.raises(OSError):voice_engine.start_engine(cfg)
        stop.assert_called_once_with(owned)


def test_stop_uses_verified_object_without_pid_lookup():
    from unittest.mock import patch
    import voice_engine
    from unittest.mock import MagicMock
    owned=MagicMock();owned.children.return_value=[];replacement=MagicMock()
    status={'running':True,'owned':True,'pid':12,'create_time':23,'endpoint':'fixture'}
    with patch('voice_engine._engine_status_with_process',return_value=(status,owned)),patch('psutil.Process',return_value=replacement) as lookup,patch('psutil.wait_procs',return_value=([],[])):
        result=voice_engine.stop_engine(expected_identity=(12,23,'fixture'))
        lookup.assert_not_called();owned.children.assert_called_once();owned.terminate.assert_called_once()
        replacement.children.assert_not_called();replacement.terminate.assert_not_called();replacement.kill.assert_not_called();assert result['stopped_pid']==12


def test_close_uses_held_object_after_registry_replaced(tmp_path):
    from unittest.mock import patch
    from config import AppConfig
    from live_playback import OwnedAudio
    cfg=AppConfig();audio=OwnedAudio(tmp_path,cfg.broadcast_voice,tmp_path/'data/main_chat/console.sqlite3')
    owned=object();audio.engine_owned=True;audio.engine_process=owned;audio.engine_identity=(11,22,'original')
    with patch('voice_engine._stop_owned_process') as stop,patch('voice_engine.engine_status',side_effect=AssertionError('registry reread')):
        asyncio.run(audio.close());stop.assert_called_once_with(owned)


def test_reused_engine_is_rejected_without_cleanup(tmp_path):
    from unittest.mock import patch,AsyncMock
    from config import AppConfig
    from live_playback import OwnedAudio
    audio=OwnedAudio(tmp_path,AppConfig().broadcast_voice,tmp_path/'data/main_chat/console.sqlite3')
    with patch('voice_engine.engine_status',return_value={'running':False}),patch('voice_engine.ready',new=AsyncMock(return_value={'owned':True,'reused':True})),patch('voice_engine._stop_owned_process') as stop:
        with pytest.raises(RuntimeError):asyncio.run(audio.render({'text':'测试','emotion':'neutral'}))
        stop.assert_not_called()
