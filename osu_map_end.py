"""Conservative osu map-end detection; no I/O or delivery side effects."""
from __future__ import annotations
import math

class MapEndDetector:
    """Only a play-to-results or play-to-song-select transition produces an event.

    Disconnects, retries, unrelated menus and map changes discard the active attempt.
    Call reset() on connection loss; missing values are never made up.
    """
    def __init__(self):
        self.active = None
        self.sequence = 0

    def reset(self):
        self.active = None

    def observe(self, sample):
        state = str(sample.get('state', '')).lower()
        values = sample.get('values') or {}
        map_id = sample.get('map_id')
        mode = str(sample.get('mode') or '')
        if state == 'play':
            if not map_id or not mode:
                self.reset()
                return None
            if self.active and (self.active['map_id'] != map_id or self.active['mode'] != mode):
                self.reset()
            current_time = values.get('map_time_ms')
            if self.active and isinstance(current_time, (int, float)):
                old = self.active['values'].get('map_time_ms')
                if isinstance(old, (int, float)) and current_time < old - 2000:
                    self.reset()  # restart or seek, never treat as completion
            if self.active is None:
                self.active = {'map_id': map_id, 'mode': mode, 'values': {}, 'failed': None}
            for name in ('score', 'accuracy', 'combo', 'map_time_ms'):
                value = values.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                    self.active['values'][name] = value
            self.active['metric_source'] = 'last_play'
            if isinstance(sample.get('sampled_at'), (int, float)):
                self.active['sampled_at'] = sample['sampled_at']
            if type(sample.get('failed')) is bool:
                self.active['failed'] = sample['failed']
            if isinstance(sample.get('hits'), dict):
                self.active['hits'] = dict(sample['hits'])
            return None
        if state in ('resultscreen', 'selectplay') and self.active:
            active, self.active = self.active, None
            if map_id and map_id != active['map_id']:
                return None
            self.sequence += 1
            status = ('failed' if active['failed'] is True else 'aborted') if state == 'selectplay' else ('failed' if active['failed'] is True else 'completed' if active['failed'] is False else 'unknown')
            return {'type': 'map_finished', 'status': status, 'sequence': self.sequence, **active}
        self.reset()
        return None


def from_tosu_v2(data):
    """Map fields recorded from tosu 4.26.3 /websocket/v2; missing stays absent."""
    if not isinstance(data, dict): return None
    state=data.get('state') or {}; beat=data.get('beatmap') or {}; play=data.get('play') or {}
    if not all(isinstance(x,dict) for x in (state,beat,play)): return None
    mode=play.get('mode') or {}; tm=beat.get('time') or {}; combo=play.get('combo') or {}
    if not all(isinstance(x,dict) for x in (mode,tm,combo)): return None
    checksum=beat.get('checksum'); identity=checksum if isinstance(checksum,str) and checksum.strip() else None
    if not identity and isinstance(beat.get('id'),int) and beat['id'] > 0: identity=str(beat['id'])
    values={'score':play.get('score'),'accuracy':play.get('accuracy'), 'combo':combo.get('max'), 'map_time_ms':tm.get('live')}
    hits=play.get('hits'); hits={k:v for k,v in hits.items() if k in ('0','50','100','300','geki','katu','sliderBreaks') and type(v) is int and v>=0} if isinstance(hits,dict) else {}
    return {'state':state.get('name',''), 'map_id':identity, 'mode':mode.get('name',''),
            'values':values, 'failed':play.get('failed'), 'hits':hits}
