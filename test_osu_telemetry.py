"""Bounded private telemetry worker and mailbox safety."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__,__file__)
import asyncio,json,time
from unittest.mock import patch
import pytest
from aiohttp import web
from agent_mailbox import AgentMailbox
from console_tools.broker import ConsoleBroker
from osu_telemetry_worker import Gate,normalize,collect
from osu_spectator import submit


def sample(focused=True,score=123):
 return {'client':'lazer','game':{'focused':focused,'paused':False},'state':{'name':'play'},'play':{'mode':{'name':'mania'},'score':score,'accuracy':97.5,'combo':{'current':12}},'beatmap':{'time':{'live':2000}}}

def test_telemetry_validity_and_dedup():
 assert normalize(sample(False),1,time.time()) is None
 x=normalize(sample(),1,100);assert x['values']['combo']==12 and x['validity']['accuracy'];g=Gate(15);assert g.accept(x,100) and not g.accept(x,120);changed=sample(score=124);changed['play']['combo']['current']=112;y=normalize(changed,2,121);assert g.accept(y,121)

def test_real_mailbox_worker_report_and_completion(tmp_path):
 broker=ConsoleBroker(tmp_path/'console.sqlite3');mail,token,task=submit(broker);agent=mail.agent(task['id']);message=mail.claim(task['id']);workspace=mail.workspace(agent);workspace.mkdir(parents=True)
 ctx={'namespace':agent['namespace'],'scope':agent['scope'],'session_key':agent['parent_key'],'generation':0,'agent_worker_id':agent['id'],'agent_input_id':message['seq'],'main_agent':False}
 path=broker.issue_context(ctx,workspace/'contexts');worker_token=json.loads(open(path,encoding='utf8').read())['token']
 async def run():
  async def ws(req):
   socket=web.WebSocketResponse();await socket.prepare(req);await socket.send_json(sample());await asyncio.sleep(.2);return socket
  app=web.Application();app.router.add_get('/websocket/v2',ws);runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start();port=site._server.sockets[0].getsockname()[1]
  try:
   with patch('osu_telemetry_worker.foreground_matches',return_value=True):result=await collect(f'ws://127.0.0.1:{port}/websocket/v2',1,0,mail,agent,message,worker_token,3)
   assert result['reports']==1
  finally:await runner.cleanup()
 asyncio.run(run());rows=mail.pending('local:osu','__work__:osu_telemetry',0);data=[json.loads(r['text']) for r in rows if r['text'].startswith('{')];assert len(data)==1 and data[0]['values']['score']==123
 assert mail.task_status(token,task['task_id'])['state']=='completed'

def test_worker_rejects_remote_endpoint(tmp_path):
 broker=ConsoleBroker(tmp_path/'console.sqlite3');mail,token,task=submit(broker);agent=mail.agent(task['id']);message=mail.claim(task['id'])
 with pytest.raises(ValueError):asyncio.run(collect('ws://example.com/websocket/v2',1,0,mail,agent,message,'unused',1))


def test_receipt_binding_freshness_and_foreground():
 from osu_telemetry_mcp import validate_latest,playback_reason,past_comment_valid
 item={'agent_id':'agent','task_id':'task','sequence':5,'sampled_at':100};notice={'task_id':'task','sequence':4}
 assert validate_latest(item,notice,'agent',102,True)
 assert not validate_latest(item,notice,'wrong',102,True)
 assert not validate_latest(item,{**notice,'task_id':'other'},'agent',102,True)
 assert not validate_latest(item,{**notice,'sequence':6},'agent',102,True)
 assert not validate_latest(item,notice,'agent',104,True)
 assert not validate_latest(item,notice,'agent',102,False)
 context={'sequence':4,'sampled_at':75};latest={'agent_id':'agent','task_id':'task','sequence':5,'sampled_at':99};notice={'task_id':'task','sequence':4}
 assert playback_reason(context,latest,notice,'agent',100,True,False)==''
 assert playback_reason(context,{**latest,'sampled_at':96},notice,'agent',100,True,False)=='stale_or_invalid_source'
 assert playback_reason(context,{**latest,'task_id':'other'},notice,'agent',100,True,False)=='stale_or_invalid_source'
 assert playback_reason(context,{**latest,'sampled_at':101},notice,'agent',100,True,False)=='stale_or_invalid_source'
 assert playback_reason({**context,'sampled_at':39},latest,notice,'agent',100,True,False)=='stale_context'
 assert playback_reason(context,latest,notice,'agent',100,False,False)=='focus_changed'
 assert playback_reason(context,latest,notice,'agent',100,True,True)=='stop_requested'
 assert past_comment_valid('刚才连击达到一百。') and not past_comment_valid('当前连击一百。')



def test_worker_stop_before_frame_cannot_report(tmp_path):
 broker=ConsoleBroker(tmp_path/'console.sqlite3');mail,token,task=submit(broker);agent=mail.agent(task['id']);message=mail.claim(task['id']);workspace=mail.workspace(agent);workspace.mkdir(parents=True)
 ctx={'namespace':agent['namespace'],'scope':agent['scope'],'session_key':agent['parent_key'],'generation':0,'agent_worker_id':agent['id'],'agent_input_id':message['seq'],'main_agent':False};path=broker.issue_context(ctx,workspace/'contexts');wt=json.loads(open(path,encoding='utf8').read())['token'];stop=tmp_path/'stop';stop.touch()
 async def run():
  async def ws(req):
   socket=web.WebSocketResponse();await socket.prepare(req);await socket.send_json(sample());await asyncio.sleep(.1);return socket
  app=web.Application();app.router.add_get('/websocket/v2',ws);runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start();port=site._server.sockets[0].getsockname()[1]
  try:
   with patch('osu_telemetry_worker.foreground_matches',return_value=True):result=await collect(f'ws://127.0.0.1:{port}/websocket/v2',1,0,mail,agent,message,wt,2,str(stop))
   assert result['reports']==0
  finally:await runner.cleanup()
 asyncio.run(run());rows=mail.pending('local:osu','__work__:osu_telemetry',0);assert not any(r['text'].startswith('{') for r in rows)


def test_new_entry_module_commands_are_frozen_core_dispatch():
 import runtime_paths
 from unittest.mock import patch
 from pathlib import Path
 with patch.object(runtime_paths.sys,'frozen',True,create=True), patch.object(runtime_paths.sys,'executable',r'C:\fixture\QQBot.exe'), patch.object(runtime_paths,'application_home',return_value=Path(r'C:\fixture')):
  with patch.object(Path,'is_file',return_value=True):
   for module in ('core','gui','cli','mcp','actor','voice','media','bilibili-browser','osu-spectator','osu-telemetry-worker','osu-telemetry-mcp'):
    command=runtime_paths.module_command(module)
    assert command[0].lower().endswith('.exe') and all(not x.lower().endswith('.py') for x in command)
    if module.startswith('osu-'):assert command[-2:]==['--module',module]


def test_notice_type_task_and_payload_validation():
 from osu_spectator import classify_notice
 sample={'version':1,'source':'tosu_v2','agent_id':'a','task_id':'t','sequence':1,'sampled_at':100}
 row={'agent_id':'a','direction':'to_main','state':'ready','payload':json.dumps({'kind':'progress'}),'text':json.dumps(sample)}
 assert classify_notice(row,'a','t')[0]=='telemetry'
 assert classify_notice(row,'a','other')[0]=='invalid'
 assert classify_notice({**row,'text':'{bad'},'a','t')[0]=='invalid'
 assert classify_notice({**row,'payload':json.dumps({'kind':'result','success':False}),'text':'failed'},'a','t')[0]=='source_failed'
 assert classify_notice({**row,'agent_id':'other'},'a','t')[0]=='invalid'




def test_audio_unmute_only_exact_owned_session():
 from types import SimpleNamespace
 from osu_telemetry_mcp import initialize_owned_audio_session
 class Volume:
  def __init__(self):self.muted=True;self.calls=0
  def GetMute(self):return self.muted
  def SetMute(self,value,event):self.calls+=1;self.muted=bool(value)
  def GetMasterVolume(self):return 0.8
 owned=Volume();other=Volume();sessions=[SimpleNamespace(ProcessId=42,SimpleAudioVolume=owned),SimpleNamespace(ProcessId=99,SimpleAudioVolume=other)]
 with pytest.raises(PermissionError):initialize_owned_audio_session(sessions,42,123,lambda p:123,unmute=False)
 assert owned.calls==0 and other.calls==0
 result=initialize_owned_audio_session(sessions,42,123,lambda p:123,unmute=True)
 assert result['mute_before'] and not result['mute_after'] and owned.calls==1 and other.calls==0
 with pytest.raises(RuntimeError):initialize_owned_audio_session(sessions,42,123,lambda p:999,unmute=True)
 assert owned.calls==1 and other.calls==0


def test_follow_bound_game_keeps_background_and_rejects_process_change():
 from osu_telemetry_worker import normalize,bound_process_matches
 from types import SimpleNamespace
 background=sample(False);assert normalize(background,1,100) is None
 item=normalize(background,1,100,True);assert item and item['focused'] is False
 proc=SimpleNamespace(is_running=lambda:True,name=lambda:'osu!.exe',create_time=lambda:123,exe=lambda:r'C:\osu\osu!.exe')
 with patch('osu_telemetry_worker.psutil.Process',return_value=proc):
  assert bound_process_matches(10,123,r'C:\osu\osu!.exe')
  assert not bound_process_matches(10,124,r'C:\osu\osu!.exe')
  assert not bound_process_matches(10,123,r'C:\other\osu!.exe')
 proc.is_running=lambda:False
 with patch('osu_telemetry_worker.psutil.Process',return_value=proc):assert not bound_process_matches(10,123,r'C:\osu\osu!.exe')


def test_menu_does_not_publish_stale_play_scores():
 from osu_telemetry_worker import normalize
 data=sample();data['state']={'name':'selectPlay'};x=normalize(data,1,100)
 assert x['state']=='selectPlay' and x['values']=={} and not any(x['validity'].values())
