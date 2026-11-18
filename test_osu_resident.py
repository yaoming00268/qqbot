"""Resident spectator scheduling and safe quiet behavior."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__,__file__)
from osu_resident import EventGate
from osu_telemetry_worker import Gate,normalize

def test_resident_main_cooldown_and_hourly_limit():
 g=EventGate(cooldown=40,limit=2);assert g.allow(100);g.mark(100);assert not g.allow(120);assert g.allow(141);g.mark(141);assert not g.allow(200);assert g.allow(3800)

def test_telemetry_quantization_and_state_changes():
 def s(score,combo,state='play'):
  return normalize({'client':'lazer','game':{'focused':True},'state':{'name':state},'play':{'mode':{'name':'mania'},'score':score,'accuracy':97,'combo':{'current':combo}},'beatmap':{'time':{'live':100}}},1,100)
 g=Gate(35);assert g.accept(s(1,101),100);assert not g.accept(s(999,150),140);assert g.accept(s(1000,201),141);assert not g.accept(s(1001,201,'selectPlay'),150);assert g.accept(s(1001,201,'selectPlay'),180)


def test_unknown_turn_is_not_restartable(tmp_path):
 import json
 from osu_resident import run
 from unittest.mock import patch
 root=tmp_path/'data/osu-resident';root.mkdir(parents=True);(root/'current.json').write_text(json.dumps({'state':'stop_unknown','unconfirmed_turns':['turn-a']}),encoding='utf8')
 with patch('osu_resident.foreground',return_value=None):
  import pytest
  with pytest.raises(RuntimeError,match='未确认'):run(tmp_path)

def test_cooldown_starts_after_playback_completion():
 from osu_resident import EventGate
 g=EventGate(cooldown=40);g.mark(100);g.last=130
 assert not g.allow(169) and g.allow(170)


def test_latest_atomic_retry_and_failure(tmp_path):
 from osu_telemetry_worker import publish_latest
 from unittest.mock import patch
 target=tmp_path/'latest.json';real=__import__('os').replace;attempts=[]
 def retry(src,dst):
  attempts.append(1)
  if len(attempts)<3:raise PermissionError('locked')
  real(src,dst)
 with patch('osu_telemetry_worker.os.replace',side_effect=retry):publish_latest(target,{'sequence':1},sleep=lambda _:None)
 assert len(attempts)==3 and __import__('json').loads(target.read_text())['sequence']==1
 with patch('osu_telemetry_worker.os.replace',side_effect=PermissionError('locked')):
  import pytest
  with pytest.raises(PermissionError):publish_latest(target,{'sequence':2},sleep=lambda _:None)
 assert __import__('json').loads(target.read_text())['sequence']==1 and not list(tmp_path.glob('*.tmp'))


def test_atomic_latest_concurrent_readers_observe_valid_json(tmp_path):
 import json,threading,time
 from osu_telemetry_worker import publish_latest
 path=tmp_path/'latest.json';publish_latest(path,{'sequence':0,'sampled_at':0});errors=[];done=threading.Event()
 def reader():
  while not done.is_set():
   try:
    with path.open(encoding='utf8') as f:data=json.load(f)
    assert data['sequence']==data['sampled_at']
   except PermissionError:pass  # Windows may briefly block a reader during atomic rename.
   except (OSError,ValueError,AssertionError) as e:errors.append(type(e).__name__)
 t=threading.Thread(target=reader);t.start()
 try:
  for i in range(200):publish_latest(path,{'sequence':i,'sampled_at':i})
 finally:done.set();t.join()
 assert not errors and json.loads(path.read_text(encoding='utf8'))['sequence']==199

def test_atomic_latest_stop_preserves_old_value(tmp_path):
 import json,pytest
 from osu_telemetry_worker import publish_latest
 path=tmp_path/'latest.json';publish_latest(path,{'sequence':1,'sampled_at':1})
 with pytest.raises(InterruptedError):publish_latest(path,{'sequence':2,'sampled_at':2},stopped=lambda:True)
 assert json.loads(path.read_text(encoding='utf8'))=={'sequence':1,'sampled_at':1}
