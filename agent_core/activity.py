"""Bridge evidence-backed software/game notes into the persistent actor."""
from __future__ import annotations

import logging
from agent_core.store import ActorStore, digest


def record_note(context, target, note_id, text, kind, evidence):
    if not context.get("actor_id") or not context.get("actor_database"):
        return
    try:
        store = ActorStore(context["actor_database"])
        fictional = target["kind"] != "software"
        source = "fictional_game" if fictional else "software_observation"
        memory_kind = source if kind in ("plot", "observation") else source + "_" + kind
        store.remember(context["actor_id"], target["title"] + "\n" + text,
            kind=memory_kind, account_id=context.get("actor_account_id", ""), visibility="actor",
            source_id="activity:" + target["id"] + ":" + note_id, evidence=evidence)
        store.ingest(context["actor_id"], context.get("actor_account_id", ""), "activity.note",
            {"target_id": target["id"], "title": target["title"], "text": text, "kind": kind,
             "source": source, "evidence": evidence}, source_id=digest([target["id"], kind, text]),
            visibility="actor", processed=True)
    except Exception:
        # The original note and input receipts remain authoritative on archive failure.
        logging.getLogger("ActorRuntime").exception("活动笔记已保存，暂未同步到角色记忆")
