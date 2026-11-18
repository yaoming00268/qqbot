"""Owned resident audio sink; file IPC restricted to a single run directory."""
from __future__ import annotations
import json,os,sys,time,winsound
from pathlib import Path
from osu_telemetry_mcp import own_audio_session,playback_reason,initialize_owned_audio_session
from osu_telemetry_worker import bound_process_matches,publish_latest

def main():
 root=Path(sys.argv[1]).resolve();pid=int(sys.argv[2]);created=float(sys.argv[3]);exe=sys.argv[4]
 audio=own_audio_session(unmute=True);(root/'audio-ready.json').write_text(json.dumps({'pid':os.getpid(),**audio}),encoding='utf8')
 from pycaw.pycaw import AudioUtilities
 import psutil
 own_created=psutil.Process().create_time()
 while not (root/'stop.request').exists():
  if not bound_process_matches(pid,created,exe):break
  req=root/'audio-request.json'
  if not req.exists():time.sleep(.1);continue
  data={}
  try:
   data=json.loads(req.read_text(encoding='utf8'));req.unlink();path=Path(data['path']).resolve()
   if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower()!='.wav':raise ValueError('无效音频路径')
   latest=json.loads((root/'latest.json').read_text(encoding='utf8'));reason=playback_reason(data['context'],latest,data['notice'],data['agent_id'],time.time(),bound_process_matches(pid,created,exe),(root/'stop.request').exists())
   if reason:raise ValueError(reason)
   status=initialize_owned_audio_session(AudioUtilities.GetAllSessions(),os.getpid(),own_created,lambda p:psutil.Process(p).create_time(),unmute=False)
   winsound.PlaySound(str(path),winsound.SND_FILENAME|winsound.SND_ASYNC);started=time.time()
   import wave
   with wave.open(str(path),'rb') as w:seconds=w.getnframes()/w.getframerate()
   while time.time()-started<seconds:
    if (root/'stop.request').exists() or not bound_process_matches(pid,created,exe):winsound.PlaySound(None,winsound.SND_PURGE);raise ValueError('stopped')
    time.sleep(.1)
   result={'id':data['id'],'ok':True,'played_at':started,'session':status}
  except Exception as e:result={'id':data.get('id','') if isinstance(data,dict) else '','ok':False,'error':type(e).__name__}
  publish_latest(root/'audio-response.json',result,stopped=lambda:(root/'stop.request').exists())
if __name__=='__main__':main()
