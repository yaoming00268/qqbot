"""Fresh read-only room status, fail closed without changing stream state."""
from __future__ import annotations
import asyncio,json
from pathlib import Path
from bilibili_adapter import BilibiliAdapter
from config import AppConfig
from live_events import ROOM

async def live_now(home):
    home=Path(home).resolve()
    try:
        config=AppConfig.load(str(home/'config.yaml'))
        from live_events import storage_root
        control=json.loads((storage_root(home,config)/'control.json').read_text(encoding='utf8'))
        if control.get('enabled') is not True or control.get('stop') is True or not config.bilibili.enabled:return False
        result=await asyncio.wait_for(BilibiliAdapter(home/config.bilibili.credentials_file).room_info(int(ROOM)),timeout=5)
        return result.get('live_status') == 1
    except Exception:return False
