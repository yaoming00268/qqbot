"""Durable, room-bound live events and explicit speech receipts.

No network, model or audio calls happen in this module.
"""
from __future__ import annotations
import hashlib
import json
import time
import uuid

ROOT = 'onebot_group_1129846525'
ROOM = '1977219646'
MAX_PENDING = 128

def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS live_events(
      id TEXT PRIMARY KEY, namespace TEXT NOT NULL, root TEXT NOT NULL, room TEXT NOT NULL,
      kind TEXT NOT NULL, summary TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
      generation INTEGER, run TEXT NOT NULL, worker TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS live_events_pending ON live_events(namespace,state,created);
    CREATE TABLE IF NOT EXISTS live_state(namespace TEXT PRIMARY KEY, wait_count INTEGER NOT NULL DEFAULT 0, last_activity REAL NOT NULL DEFAULT 0, last_idle REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS live_outgoing(
      id TEXT PRIMARY KEY, event_id TEXT NOT NULL, token TEXT NOT NULL, request_key TEXT NOT NULL,
      text TEXT NOT NULL, emotion TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
      UNIQUE(token,request_key));
    ''')

def enqueue(broker, *, namespace, run, worker, attempt, summary, created=None):
    if not all(isinstance(x,str) and 0 < len(x) <= 200 for x in (namespace,run,worker,attempt)):
        raise ValueError('invalid live event binding')
    if not isinstance(summary,dict) or summary.get('type') != 'map_finished':
        raise ValueError('invalid map summary')
    payload=json.dumps(summary,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    if len(payload)>4096: raise ValueError('summary too large')
    event_id=hashlib.sha256(('\n'.join((namespace,run,worker,attempt))).encode()).hexdigest()
    with broker.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute('SELECT summary FROM live_events WHERE id=?',(event_id,)).fetchone()
        if existing:
            if existing[0] != payload: raise ValueError('attempt already has different summary')
            return event_id
        n=db.execute("SELECT count(*) FROM live_events WHERE namespace=? AND state IN ('queued','running','unknown')",(namespace,)).fetchone()[0]
        if n>=MAX_PENDING: raise OverflowError('live event queue full')
        db.execute('INSERT INTO live_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (event_id,namespace,ROOT,ROOM,'analysis',payload,'queued',time.time() if created is None else created,None,run,worker))
        db.execute("INSERT INTO live_state(namespace,wait_count,last_activity,last_idle) VALUES(?,0,?,0) ON CONFLICT(namespace) DO UPDATE SET wait_count=0,last_activity=excluded.last_activity",(namespace,time.time()))
        db.execute("UPDATE live_events SET state='superseded' WHERE namespace=? AND kind='idle' AND state='queued'",(namespace,))
        db.execute("UPDATE live_outgoing SET state='superseded' WHERE state IN ('queued','rendering','speaking') AND event_id IN (SELECT id FROM live_events WHERE namespace=? AND kind='idle')",(namespace,))
    return event_id

def claim(broker, namespace, generation):
    with broker.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM live_events WHERE namespace=? AND state IN ('running','unknown')",(namespace,)).fetchone(): return None
        row=db.execute("SELECT * FROM live_events WHERE namespace=? AND state='queued' ORDER BY CASE kind WHEN 'analysis' THEN 0 ELSE 1 END,created,rowid LIMIT 1",(namespace,)).fetchone()
        if not row: return None
        db.execute("UPDATE live_events SET state='running',generation=? WHERE id=? AND state='queued'",(generation,row['id']))
        result=dict(row);result.update(state='running',generation=generation);return result

def say(broker, token, text, emotion, request_key):
    ctx=broker.context(token)
    if not ctx.get('live_event') or ctx.get('root') != ROOT or str(ctx.get('room')) != ROOM:
        raise PermissionError('not a bound live turn')
    text=str(text).strip();emotion=str(emotion).strip();request_key=str(request_key).strip()
    if not text or len(text)>500 or len(emotion)>60 or not request_key or len(request_key)>160: raise ValueError('invalid live speech')
    with broker.connect() as db:
        db.execute('BEGIN IMMEDIATE');broker._require_active_turn(db,token)
        row=db.execute("SELECT * FROM live_events WHERE id=? AND namespace=? AND root=? AND room=? AND state='running' AND generation=?",(ctx['event_id'],ctx['namespace'],ROOT,ROOM,ctx['generation'])).fetchone()
        if not row: raise PermissionError('live event no longer active')
        if row['kind']=='idle' and db.execute("SELECT 1 FROM live_events WHERE namespace=? AND kind='analysis' AND state IN ('queued','running') LIMIT 1",(ctx['namespace'],)).fetchone():
            raise PermissionError('analysis takes priority over idle speech')
        existing=db.execute('SELECT id,text,emotion,state FROM live_outgoing WHERE token=? AND request_key=?',(token,request_key)).fetchone()
        if existing:
            if (existing['text'],existing['emotion']) != (text,emotion): raise ValueError('request key reused')
            return {'id':existing['id'],'state':existing['state']}
        identity=uuid.uuid4().hex
        db.execute('INSERT INTO live_outgoing VALUES(?,?,?,?,?,?,?,?)',(identity,ctx['event_id'],token,request_key,text,emotion,'queued',time.time()))
        return {'id':identity,'state':'queued'}

def recover(broker, namespace):
    with broker.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE live_events SET state='unknown' WHERE namespace=? AND state='running'",(namespace,))
        db.execute("UPDATE live_outgoing SET state='unknown' WHERE state IN ('rendering','speaking','playing') AND event_id IN (SELECT id FROM live_events WHERE namespace=?)",(namespace,))


def reset_activity(broker, namespace):
    with broker.connect() as db:
        db.execute("INSERT INTO live_state(namespace,wait_count,last_activity,last_idle) VALUES(?,0,?,0) ON CONFLICT(namespace) DO UPDATE SET wait_count=0,last_activity=excluded.last_activity",(namespace,time.time()))

def enqueue_idle(broker, namespace, now=None, interval=300):
    now=time.time() if now is None else now
    with broker.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT OR IGNORE INTO live_state(namespace,wait_count,last_activity,last_idle) VALUES(?,0,?,?)",(namespace,now,now))
        state=db.execute('SELECT * FROM live_state WHERE namespace=?',(namespace,)).fetchone()
        if state['wait_count']>=3 or now-max(state['last_activity'],state['last_idle'])<interval:return None
        if db.execute("SELECT 1 FROM live_events WHERE namespace=? AND state IN ('queued','running','unknown')",(namespace,)).fetchone():return None
        if db.execute("SELECT 1 FROM live_outgoing o JOIN live_events e ON e.id=o.event_id WHERE e.namespace=? AND o.state IN ('queued','rendering','speaking','playing','unknown')",(namespace,)).fetchone():return None
        event_id=uuid.uuid4().hex
        db.execute('INSERT INTO live_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',(event_id,namespace,ROOT,ROOM,'idle','{}','queued',now,None,'idle','main'))
        db.execute('UPDATE live_state SET last_idle=? WHERE namespace=?',(now,namespace))
        return event_id

def record_idle_result(broker, namespace, event_id):
    with broker.connect() as db:
        row=db.execute("SELECT r.mode FROM contexts c JOIN reply_controls r ON r.token=c.token WHERE c.namespace=? AND json_extract(c.payload,'$.event_id')=? ORDER BY c.rowid DESC LIMIT 1",(namespace,event_id)).fetchone()
        waited=bool(row and row[0]=='wait')
        db.execute('UPDATE live_state SET wait_count=CASE WHEN last_activity > (SELECT created FROM live_events WHERE id=?) THEN 0 WHEN ? THEN min(3,wait_count+1) ELSE 0 END WHERE namespace=?',(event_id,waited,namespace))


def cancel_pending(broker,namespace):
    with broker.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE live_outgoing SET state='cancelled' WHERE state IN ('queued','rendering','speaking') AND event_id IN (SELECT id FROM live_events WHERE namespace=?)",(namespace,))
        db.execute("UPDATE live_events SET state='cancelled' WHERE namespace=? AND state='queued'",(namespace,))


def storage_root(home, config):
    from pathlib import Path
    path=Path(config.bot.runtime_database_path)
    return (path if path.is_absolute() else Path(home)/path).resolve().parent/'osu-live'
