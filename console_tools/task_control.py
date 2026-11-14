"""One task view for ordinary requests and individual persistent-agent mail."""
from agent_mailbox import AgentMailbox

ACTIVE_STATES = ("submitted", "queued", "running", "ready")


def task_status(broker, token, identity):
    return AgentMailbox(broker).task_status(token, identity) or broker.status(token, identity)


def list_tasks(broker, token, states=(), limit=20, *, exclude=""):
    limit = min(100, max(1, int(limit)))
    rows = [broker.status(token, identity) for identity in broker.list_requests(token, states, limit + bool(exclude))
            if identity != exclude]
    rows.extend(AgentMailbox(broker).list_tasks(token, states, limit))
    return sorted(rows, key=lambda row: (row.get("created", 0), row["id"]), reverse=True)[:limit]


def cancel_task(broker, token, identity):
    row = AgentMailbox(broker).cancel_task(token, identity)
    if row is not None:
        return row
    row = broker.status(token, identity)
    if row["state"] in ACTIVE_STATES:
        broker.update(identity, "cancel_requested")
    return broker.status(token, identity)
