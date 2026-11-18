"""Compare durable IDs while allowing normal activity timestamps to advance."""
from pathlib import Path
import json

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/"maintenance_reports/voice-completion-20261116-131025"
BACKUP=REPORT/"activation-backup"


def ids(value,path=()):
    found={}
    if isinstance(value,dict):
        for key,item in value.items():
            if key in ("cid","rid","context_id","conversation_id","session_id"):
                found[path+(key,)]=item
            elif isinstance(item,(dict,list)):
                found.update(ids(item,path+(key,)))
    elif isinstance(value,list):
        for index,item in enumerate(value):
            found.update(ids(item,path+(index,)))
    return found


result={key:(BACKUP/path).read_bytes()==(ROOT/path).read_bytes() for key,path in (
    ("config_unchanged","config.yaml"),("personas_unchanged","personas.yaml"),("catalog_unchanged","data/voice/catalog.json"))}
sessions=[]
for before in (BACKUP/"data").glob("*sessions*.json"):
    after=ROOT/"data"/before.name
    old=json.loads(before.read_text(encoding="utf-8"))
    new=json.loads(after.read_text(encoding="utf-8"))
    old_ids,new_ids=ids(old),ids(new)
    preserved=all(path in new_ids and value==new_ids[path] for path,value in old_ids.items())
    assert set(old).issubset(set(new)) and preserved,before.name
    sessions.append({"file":before.name,"entries_before":len(old),"entries_now":len(new),
        "durable_ids":len(old_ids),"all_durable_ids_preserved":preserved,"bytes_unchanged":before.read_bytes()==after.read_bytes()})
assert all(result.values()),result
result.update(ok=True,sessions=sessions,note="Runtime activity timestamps may update normally after start; context IDs are compared recursively.")
(REPORT/"preservation-audit.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(result,ensure_ascii=False))
