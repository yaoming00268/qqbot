from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

from osu_map_end import MapEndDetector

def sample(state='play', map_id=7, mode='mania', **values):
    return {'state':state, 'map_id':map_id, 'mode':mode, 'values':values}

def test_result_only_once_and_last_valid_values():
    d=MapEndDetector()
    assert d.observe(sample(map_time_ms=1000, score=30, accuracy=98)) is None
    assert d.observe(sample(map_time_ms=2500, score=70)) is None
    event=d.observe(sample('resultScreen'))
    assert event['values'] == {'map_time_ms':2500, 'score':70, 'accuracy':98}
    assert d.observe(sample('resultScreen')) is None

def test_retry_disconnect_menu_and_changed_map_do_not_complete():
    d=MapEndDetector(); d.observe(sample(map_time_ms=10000));d.observe(sample(map_time_ms=100))
    assert d.observe(sample('resultScreen',map_id=8)) is None
    d.observe(sample(map_time_ms=100));d.reset();assert d.observe(sample('resultScreen')) is None
    d.observe(sample());d.observe(sample('menu'));assert d.observe(sample('resultScreen')) is None

def test_missing_identity_and_invalid_values_are_not_fabricated():
    d=MapEndDetector();d.observe(sample(map_id=None,score=0));assert d.observe(sample('resultScreen')) is None
    d.observe(sample(score=float('nan'),accuracy=-1));assert d.observe(sample('resultScreen'))['values'] == {}


def test_recorded_upstream_sample_field_mapping():
    # Keys follow recorded tosu 4.26.3 v2 shape; values are synthetic.
    from osu_map_end import from_tosu_v2
    data={'state':{'name':'play'},'beatmap':{'checksum':'fixture-hash','id':123,'time':{'live':1200}},
          'play':{'mode':{'name':'mania'},'failed':False,'score':22,'accuracy':95.0,
                  'combo':{'max':12},'hits':{'0':1,'300':4}}}
    sample=from_tosu_v2(data)
    assert sample['map_id']=='fixture-hash'
    assert sample['values']['map_time_ms']==1200
    assert sample['hits']=={'0':1,'300':4}


def test_select_play_reports_abort_or_failure_without_clear():
    d=MapEndDetector();data=sample(map_time_ms=500);data['failed']=False;d.observe(data)
    assert d.observe(sample('selectPlay'))['status']=='aborted'
    data['failed']=True;d.observe(data)
    assert d.observe(sample('selectPlay'))['status']=='failed'
    assert d.observe(sample('selectPlay')) is None
