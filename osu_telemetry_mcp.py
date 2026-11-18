"""Private telemetry main-agent MCP: fresh mailbox receipt, local speech, finish."""
from __future__ import annotations
import argparse, asyncio, base64, json, os, sys, tempfile, time, wave, winsound
from pathlib import Path
from console_tools.broker import ConsoleBroker
from agent_mailbox import AgentMailbox
from config import AppConfig
from osu_telemetry_worker import foreground_matches, bound_process_matches, MAX_SAMPLE_AGE_SECONDS, publish_latest
from broadcast_tts import BroadcastVoiceService

def validate_latest(item,notice,agent_id,now,foreground):
 if not foreground or item.get('agent_id')!=agent_id or item.get('task_id')!=notice.get('task_id') or item.get('sequence',0)<notice.get('sequence',0):return False
 age=now-item.get('sampled_at',0)
 return 0<=age<=MAX_SAMPLE_AGE_SECONDS

def playback_reason(context,latest,notice,agent_id,now,foreground,stopped):
 if stopped:return 'stop_requested'
 if not foreground:return 'focus_changed'
 age=now-context.get('sampled_at',0)
 if not 0<=age<=60:return 'stale_context'
 if not isinstance(latest,dict) or not validate_latest(latest,notice,agent_id,now,True) or latest.get('sequence',0)<context.get('sequence',0):return 'stale_or_invalid_source'
 return ''

def past_comment_valid(text):
 return bool(text and len(text)<=40 and text.count('。')<=1 and '\n' not in text and text.startswith(('刚才','上一刻','此前')) and not any(x in text for x in ('当前','现在','实时','此刻','正在')))

def initialize_owned_audio_session(sessions,pid,process_created,identity,*,unmute=False):
    matches=[s for s in sessions if s.ProcessId==pid]
    if len(matches)!=1 or identity(pid)!=process_created:raise RuntimeError('无法唯一核验本进程音频会话。')
    volume=matches[0].SimpleAudioVolume;before=bool(volume.GetMute())
    if before and unmute:volume.SetMute(0,None)
    after=bool(volume.GetMute())
    if after:raise PermissionError('本进程音频会话已静音。')
    return {'mute_before':before,'mute_after':after,'volume':float(volume.GetMasterVolume())}

def own_audio_session(*,unmute=False):
    import psutil
    from pycaw.pycaw import AudioUtilities
    pid=os.getpid();created=psutil.Process(pid).create_time()
    path=None
    try:
        with tempfile.NamedTemporaryFile(suffix='.wav',delete=False) as f:path=f.name
        with wave.open(path,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(bytes(24000*2*3))
        winsound.PlaySound(path,winsound.SND_FILENAME|winsound.SND_ASYNC)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            sessions=[s for s in AudioUtilities.GetAllSessions() if s.ProcessId==pid]
            if sessions:return initialize_owned_audio_session(sessions,pid,created,lambda x:psutil.Process(x).create_time(),unmute=unmute)
            time.sleep(.1)
        raise RuntimeError('本进程音频会话未出现。')
    finally:
        winsound.PlaySound(None,winsound.SND_PURGE)
        if path:Path(path).unlink(missing_ok=True)

def main():
 p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--db',required=True);p.add_argument('--namespace',required=True);p.add_argument('--parent',required=True);p.add_argument('--agent-id',required=True);p.add_argument('--task-id',required=True);p.add_argument('--run-id',required=True);p.add_argument('--output',required=True);p.add_argument('--pid',type=int,required=True);p.add_argument('--created',type=float,required=True);p.add_argument('--unmute-own',action='store_true');p.add_argument('--follow-bound-game',action='store_true');p.add_argument('--executable',default='');p.add_argument('--shared-root',default='');a=p.parse_args()
 for stream in (sys.stdin,sys.stdout,sys.stderr):
  if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace' if stream is sys.stderr else 'strict')
 out=Path(a.output);shared=Path(a.shared_root) if a.shared_root else out;audio_initialized=False;broker=ConsoleBroker(a.db,initialize=False);mail=AgentMailbox(broker);cfg=AppConfig.load(a.config);terminal=False;read=False;spoken=False;sampled_at=0;accepted=None;context=None;start=time.monotonic()
 def audit(data):
  with (out/'main-audit.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps({**data,'run_id':a.run_id,'elapsed':round(time.monotonic()-start,3)},ensure_ascii=False)+'\n')
 def matches():return bound_process_matches(a.pid,a.created,a.executable) if a.follow_bound_game else foreground_matches(a.pid,a.created)
 def active():return not terminal and not (shared/'stop.request').exists() and matches()
 def observe():
  nonlocal read,sampled_at,accepted,context
  if not active():return {'isError':True,'content':[{'type':'text','text':'游戏不在前台或本轮已停止。'}]}
  if read:return {'content':[{'type':'text','text':'本轮已读取报告。'}]}
  rows=mail.pending(a.namespace,a.parent,0)
  for row in rows:
   from osu_spectator import classify_notice
   try:
    notice=json.loads(row['text']);kind,notice=classify_notice(row,a.agent_id,a.task_id)
    if kind!='telemetry':continue
    item=json.loads((shared/'latest.json').read_text(encoding='utf-8'))
    if not validate_latest(item,notice,a.agent_id,time.time(),active()):continue
   except (ValueError,TypeError,OSError):continue
   age=time.time()-item.get('sampled_at',0)
   if age<0 or age>MAX_SAMPLE_AGE_SECONDS:mail.acknowledge([row]);continue
   read=True;sampled_at=item['sampled_at'];accepted=notice;context=dict(item);mail.acknowledge([row]);item['age_seconds']=round(age,2);audit({'event':'mailbox_consumed','seq':row['seq'],'sample_seq':item['sequence'],'sample_age_ms':round(age*1000,1),'client':item.get('client'),'state':item.get('state'),'mode':item.get('mode')})
   return {'content':[{'type':'text','text':json.dumps(item,ensure_ascii=False)}]}
  return {'isError':True,'content':[{'type':'text','text':'没有新鲜的子代理报告。'}]}
 async def synth(text):
  chunks=[];fmt={}
  async def sink(e):
   if not fmt:fmt.update(rate=e['sample_rate'],channels=e['channels'])
   if fmt['rate']!=e['sample_rate'] or fmt['channels']!=e['channels']:raise ValueError('音频格式变化')
   chunks.append(base64.b64decode(e['pcm_base64'],validate=True))
  t=time.monotonic();r=await BroadcastVoiceService(cfg.broadcast_voice).synthesize(text,'neutral',audio_sink=sink)
  if r.get('state')!='ready' or not chunks:raise ValueError('语音未生成')
  path=out/'comment.wav'
  with wave.open(str(path),'wb') as w:w.setnchannels(fmt['channels']);w.setsampwidth(2);w.setframerate(fmt['rate']);w.writeframes(b''.join(chunks))
  return path,round((time.monotonic()-t)*1000,1)
 def guard(stage):
  now=time.time();focused=matches();stopped=terminal or (shared/'stop.request').exists()
  try:latest=json.loads((shared/'latest.json').read_text(encoding='utf-8'))
  except (OSError,ValueError,TypeError):latest=None
  reason=playback_reason(context or {},latest,accepted or {},a.agent_id,now,focused,stopped)
  audit({'event':'playback_guard','stage':stage,'reason':reason,'context_age_ms':round((now-(context or {}).get('sampled_at',0))*1000,1),'latest_age_ms':round((now-latest.get('sampled_at',0))*1000,1) if isinstance(latest,dict) else None,'context_seq':(context or {}).get('sequence'),'latest_seq':latest.get('sequence') if isinstance(latest,dict) else None,'foreground_matches':focused,'stop_requested':stopped})
  return reason
 def speak(text):
  nonlocal spoken,audio_initialized
  text=str(text).strip()
  if not read or spoken:return {'isError':True,'content':[{'type':'text','text':'本轮不能播放。'}]}
  if not past_comment_valid(text):
   audit({'event':'speech_rejected','reason':'past_tense_required'});return {'isError':True,'content':[{'type':'text','text':'请以刚才开头，用过去时描述所观察的事实，不说当前或正在。'}]}
  reason=guard('before_synthesis')
  if reason:return {'isError':True,'content':[{'type':'text','text':'播放前检查未通过：'+reason}]}
  spoken=True
  try:
   audit({'event':'synthesis_start','sampled_at':sampled_at,'sample_age_ms':round((time.time()-sampled_at)*1000,1)})
   path,ms=asyncio.run(synth(text));audit({'event':'synthesis_end','tts_ms':ms})
   reason=guard('before_playback')
   if reason:audit({'event':'speech_discarded','reason':reason});return {'isError':True,'content':[{'type':'text','text':'已停止本地播放：'+reason}]}
   if a.shared_root:
    import uuid
    identity=uuid.uuid4().hex;request={'id':identity,'path':str(path),'context':context,'notice':accepted,'agent_id':a.agent_id};publish_latest(shared/'audio-request.json',request,stopped=lambda:(shared/'stop.request').exists());deadline=time.monotonic()+30;response=None
    while time.monotonic()<deadline and not (shared/'stop.request').exists():
     try:
      x=json.loads((shared/'audio-response.json').read_text(encoding='utf8'))
      if x.get('id')==identity:response=x;break
     except (OSError,ValueError):pass
     time.sleep(.1)
    if not response or not response.get('ok'):raise RuntimeError('本地音频进程未确认播放。')
    audit({'event':'speak','text':text,'tts_ms':ms,'audio':str(path),'playback':'resident_audio_ack','played_at':response['played_at']})
   else:
    if not audio_initialized:
     audio_state=own_audio_session(unmute=a.unmute_own);audio_initialized=True;audit({'event':'audio_session_initialized',**audio_state})
    audio_state=own_audio_session(unmute=False);audit({'event':'audio_session_before_play',**audio_state})
    winsound.PlaySound(str(path),winsound.SND_FILENAME);audit({'event':'speak','text':text,'tts_ms':ms,'audio':str(path),'playback':'winsound_returned'})
   return {'content':[{'type':'text','text':'本地语音已播放。'}]}
  except Exception as e:
   import traceback
   from voice_text import VoiceError
   frames=[{'file':Path(x.filename).name,'line':x.lineno,'function':x.name} for x in traceback.extract_tb(e.__traceback__)]
   code='voice_input_or_engine' if isinstance(e,VoiceError) else 'voice_unexpected'
   detail=str(e)[:300] if isinstance(e,VoiceError) else '本地语音引擎执行失败。'
   cause=e.__cause__;cause_info={'type':type(cause).__name__,'detail':str(cause)[:200]} if cause else None
   audit({'event':'speech_error','type':type(e).__name__,'code':code,'detail':detail,'traceback':frames,'cause':cause_info})
   return {'isError':True,'content':[{'type':'text','text':'语音未完成：'+detail}]}
 def finish():
  nonlocal terminal
  terminal=True;audit({'event':'finish'});return {'content':[{'type':'text','text':'本地看客已结束。'}]}
 tools=[{'name':'telemetry_report','description':'读取本轮真实子代理已交付且未过期的游戏摘要。','inputSchema':{'type':'object','properties':{},'additionalProperties':False}},{'name':'local_speak','description':'本地播放一条基于摘要的简短评论。','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text'],'additionalProperties':False}},{'name':'finish','description':'结束本轮。','inputSchema':{'type':'object','properties':{},'additionalProperties':False}}]
 audit({'event':'server_start','pid':os.getpid()})
 for line in sys.stdin:
  try:
   req=json.loads(line);ident=req.get('id');method=req.get('method');params=req.get('params') or {}
   if ident is None:continue
   if method=='initialize':result={'protocolVersion':params.get('protocolVersion','2024-11-05'),'capabilities':{'tools':{}},'serverInfo':{'name':'osu-telemetry-local','version':'1'}}
   elif method=='tools/list':result={'tools':tools}
   elif method=='tools/call':
    name=params.get('name');args=params.get('arguments') or {};result=({'isError':True,'content':[{'type':'text','text':'本轮已结束。'}]} if terminal else observe() if name=='telemetry_report' else speak(args.get('text','')) if name=='local_speak' else finish() if name=='finish' else {'isError':True,'content':[{'type':'text','text':'未知工具'}]})
   else:result={}
   print(json.dumps({'jsonrpc':'2.0','id':ident,'result':result},ensure_ascii=True),flush=True)
   if method=='tools/call' and params.get('name')=='finish' and terminal:(out/'terminal.flag').write_text(a.run_id,encoding='utf-8')
  except Exception as e:audit({'event':'server_error','type':type(e).__name__});print(json.dumps({'jsonrpc':'2.0','id':req.get('id'),'error':{'code':-32603,'message':'本地工具失败'}}),flush=True)
if __name__=='__main__':main()
