"""Independent chat/work lanes and a durable, receipt-aware text outbox."""
from __future__ import annotations

import asyncio
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import inspect
import logging
from pathlib import Path
import sqlite3
import time
import uuid

from messaging import SendError, SendUnknown, SendUnavailable

logger = logging.getLogger("ConversationRuntime")
current_event = ContextVar("qqbot_event", default=None)


@dataclass
class EventContext:
    scope: str
    message_id: str = ""
    received_at: float = 0
    lane: str = "chat"
    owner: str = ""
    result: object = None
    text: str = ""
    merged: list = field(default_factory=list)
    directed: bool = False
    persona: object = None
    batch: list = field(default_factory=list)
    batchable: bool = False
    reply_generating: bool = False
    rebatch_requested: bool = False
    interrupted_replies: list = field(default_factory=list)
    sender_name: str = ""


@dataclass
class ChatItem:
    event: EventContext
    callback: object
    directed: bool
    record_only: object
    coalesce: bool = False
    first_at: float = 0
    last_at: float = 0
    batchable: bool = False


class RuntimeStore:
    def __init__(self, path, namespace):
        self.namespace = namespace
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS work (
              id TEXT PRIMARY KEY, namespace TEXT, scope TEXT, owner TEXT,
              message_id TEXT, label TEXT, state TEXT, detail TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS outbox (
              id TEXT PRIMARY KEY, namespace TEXT, scope TEXT, action TEXT,
              payload TEXT, state TEXT, receipt TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS work_results(job_id TEXT PRIMARY KEY, data TEXT);
        """)
        # A send that crossed the transport boundary may already exist in QQ.
        self.db.execute("UPDATE outbox SET state='unknown' WHERE namespace=? AND state='sending'", (namespace,))
        self.db.execute("UPDATE work SET state='interrupted', detail='进程已重启；请检查任务进度或重新提交。' "
                        "WHERE namespace=? AND state IN ('queued','running')", (namespace,))
        self.db.commit()

    def work_state(self, job_id, state, detail=""):
        if detail is None:
            detail_val = ""
        elif isinstance(detail, (str, int, float, bytes)):
            detail_val = str(detail)
        elif hasattr(detail, "_mock_name"):
            detail_val = ""
        else:
            detail_val = str(detail)
        self.db.execute("UPDATE work SET state=?,detail=? WHERE id=? AND namespace=?",
                        (state, detail_val, job_id, self.namespace))
        self.db.commit()

    def jobs(self, owner):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM work WHERE namespace=? AND owner=? ORDER BY created DESC LIMIT 10",
            (self.namespace, owner))]

    def result(self, job_id):
        row = self.db.execute("SELECT data FROM work_results WHERE job_id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else {}


class ConversationRuntime:
    """Ordered main chats and independently running temporary work chats.

    Consecutive group speech can share one model turn. Every original message
    remains in the transcript; work and priority controls bypass the wait.
    """
    def __init__(self, store, *, passive_backlog=2, on_work_done=None,
                 max_pending_jobs=32, per_conversation_queue_limit=3,
                 settle_seconds=1.5, max_wait_seconds=6, buffer_seconds=0,
                 backlog_interrupt_messages=3):
        self.store = store
        self.passive_backlog = passive_backlog
        self.inboxes = {}
        self.chat_workers = {}
        self.work_worker = None
        self.active_work = {}
        self.closed = False
        self.on_work_done = on_work_done
        self.settle_seconds, self.max_wait_seconds = settle_seconds, max_wait_seconds
        self.buffer_seconds = buffer_seconds
        self.backlog_interrupt_messages = backlog_interrupt_messages
        self.inbox_changed = {}
        # Accept old settings for configuration compatibility. Temporary model
        # conversations have no queue length or concurrency ceiling.
        self.max_pending_jobs = max_pending_jobs
        self.per_conversation_queue_limit = per_conversation_queue_limit

    def submit_chat(self, event, callback, *, directed=True, record_only=None, coalesce=False, batchable=None):
        if self.closed:
            return
        inbox = self.inboxes.setdefault(event.scope, deque())
        now = asyncio.get_running_loop().time()
        batchable = coalesce if batchable is None else batchable
        item = ChatItem(event, callback, directed, record_only, coalesce, now, now, batchable)
        event.batchable = batchable
        event.directed = directed
        if coalesce and self.buffer_seconds <= 0:
            # Replace a queued opportunity for this speaker, retaining every
            # fragment and keeping the newest source as the reply target.
            for previous in reversed(inbox):
                if not previous.coalesce:
                    break
                if previous.event.persona != event.persona or previous.batchable != batchable:
                    break
                if previous.event.owner == event.owner:
                    # A slow active model must not merge independent questions
                    # merely because both happen to be waiting in the queue.
                    if now - previous.first_at > self.max_wait_seconds or now - previous.last_at > self.max_wait_seconds:
                        break
                    event.merged = [*previous.event.merged,
                        {"id": previous.event.message_id, "sender_id": previous.event.owner.rsplit(":", 1)[-1],
                         "text": previous.event.text, "received_at": previous.event.received_at,
                         "sender_name": previous.event.sender_name, "directed": previous.directed}]
                    item.first_at = previous.first_at
                    item.directed = event.directed = directed or previous.directed
                    if previous.record_only:
                        previous.record_only()
                    inbox.remove(previous)
                    break
        if not directed and not (coalesce and self.buffer_seconds > 0):
            passive = [entry for entry in inbox if not entry.directed and not entry.coalesce]
            while len(passive) >= self.passive_backlog:
                discarded = passive.pop(0)
                inbox.remove(discarded)
                if discarded.record_only:
                    discarded.record_only()
        inbox.append(item)
        self.inbox_changed.setdefault(event.scope, asyncio.Event()).set()
        if event.scope not in self.chat_workers:
            self.chat_workers[event.scope] = asyncio.create_task(self._chat_loop(event.scope))

    @staticmethod
    def source_rows(event):
        if event.batch:
            return [dict(row) for row in event.batch]
        return [*(dict(row) for row in event.merged),
                {"id": event.message_id, "sender_id": event.owner.rsplit(":", 1)[-1],
                 "sender_name": event.sender_name, "text": event.text,
                 "received_at": event.received_at, "directed": event.directed}]

    def request_rebatch(self, event):
        """Only natural speech at the head of this persona's queue can interrupt."""
        if event.rebatch_requested:
            return True
        if not (self.backlog_interrupt_messages and event.batchable and event.reply_generating):
            return False
        active_ids = {row["id"] for row in self.source_rows(event) if row.get("id")}
        pending = set()
        for item in self.inboxes.get(event.scope, ()):
            if not item.batchable or item.event.persona != event.persona:
                break
            pending.update(row["id"] for row in self.source_rows(item.event)
                           if row.get("id") and row["id"] not in active_ids)
        if len(pending) < self.backlog_interrupt_messages:
            return False
        event.rebatch_requested = True
        logger.info("[%s] 积压 %d 条新消息，中断当前回复并合并交给主代理", event.scope, len(pending))
        return True

    async def _combine_chat(self, items, carry=None):
        """Prepare queued materials once; the interrupted input was already prepared."""
        rows = self.source_rows(carry) if carry else []
        for entry in items[:-1]:
            token = current_event.set(entry.event)
            try:
                if entry.record_only:
                    value = entry.record_only()
                    if inspect.isawaitable(value):
                        await value
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("缓存消息资料准备失败: %s", entry.event.message_id)
            finally:
                current_event.reset(token)
            rows.extend(self.source_rows(entry.event))
        item = items[-1]
        rows.extend(self.source_rows(item.event))
        unique = {}
        for index, row in enumerate(rows):
            unique[row.get("id") or ("anonymous", index)] = row
        item.event.batch = sorted(unique.values(), key=lambda row: row.get("received_at") or 0)
        item.event.merged = [row for row in item.event.batch if row.get("id") != item.event.message_id]
        item.directed = item.event.directed = any(row.get("directed") for row in rows)
        if carry:
            item.event.interrupted_replies = list(carry.interrupted_replies)
        return item

    async def _chat_loop(self, scope):
        try:
            inbox = self.inboxes[scope]
            carry = None
            while inbox and not self.closed:
                item = inbox[0]
                fixed = item.coalesce and self.buffer_seconds > 0
                combined = False
                if carry and item.batchable and item.event.persona == carry.persona:
                    items = []
                    while inbox and inbox[0].batchable and inbox[0].event.persona == carry.persona:
                        items.append(inbox.popleft())
                    item = await self._combine_chat(items, carry)
                    carry = None
                    combined = True
                    fixed = False
                elif fixed:
                    # A fixed window never extends when more people speak.
                    remaining = item.first_at + self.buffer_seconds - asyncio.get_running_loop().time()
                    if remaining > 0:
                        changed = self.inbox_changed[scope]
                        changed.clear()
                        try:
                            await asyncio.wait_for(changed.wait(), remaining)
                        except TimeoutError:
                            pass
                        continue
                    items = []
                    deadline = item.first_at + self.buffer_seconds
                    while (inbox and inbox[0].coalesce and inbox[0].first_at <= deadline
                           and inbox[0].event.persona == item.event.persona):
                        items.append(inbox.popleft())
                    item = await self._combine_chat(items)
                elif item.coalesce and self.settle_seconds > 0 and self.max_wait_seconds > 0:
                    changed = self.inbox_changed[scope]
                    changed.clear()
                    remaining = min(item.last_at + self.settle_seconds, item.first_at + self.max_wait_seconds) - asyncio.get_running_loop().time()
                    if remaining > 0:
                        try:
                            await asyncio.wait_for(changed.wait(), remaining)
                        except TimeoutError:
                            pass
                        continue
                if not fixed and not combined:
                    item = inbox.popleft()
                event, callback = item.event, item.callback
                logger.info("[%s] 处理来源 %s，聊天队列等待 %.2f 秒，合并 %d 条补充", scope,
                            event.message_id, asyncio.get_running_loop().time() - item.first_at, len(event.merged))
                token = current_event.set(event)
                try:
                    await callback()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("聊天事件处理失败: %s", scope)
                finally:
                    current_event.reset(token)
                    if event.rebatch_requested:
                        carry = event
                    if fixed and inbox and carry is None:
                        # Messages arriving during a reply get a whole new
                        # collection window after that reply has completed.
                        finished = asyncio.get_running_loop().time()
                        for pending in inbox:
                            pending.first_at = max(pending.first_at, finished)
        finally:
            self.chat_workers.pop(scope, None)
            self.inboxes.pop(scope, None)
            self.inbox_changed.pop(scope, None)

    def submit_work(self, event, label, callback, guard=lambda: True):
        if self.closed:
            raise SendError("机器人正在停止，任务未加入队列。")
        job_id = uuid.uuid4().hex[:12]
        self.store.db.execute("INSERT INTO work VALUES(?,?,?,?,?,?,?,?,?)",
            (job_id, self.store.namespace, event.scope, event.owner, event.message_id,
             label[:200], "queued", "", time.time()))
        self.store.db.commit()
        task = asyncio.create_task(self._run_work(job_id, event, callback, guard))
        self.active_work[job_id] = (event, task)
        task.add_done_callback(lambda _: self.active_work.pop(job_id, None))
        if self.work_worker is None or self.work_worker.done():
            self.work_worker = asyncio.create_task(self._work_loop())
        return job_id

    async def _work_loop(self):
        # A completion handle for shutdown/draining; new work starts at submit,
        # so a slow snapshot never prevents later images/tasks from starting.
        while self.active_work:
            await asyncio.gather(*(task for _, task in list(self.active_work.values())), return_exceptions=True)
            # Python 3.12 can finish gather synchronously for completed tasks;
            # remove them here instead of starving their scheduled callbacks.
            for job_id, (_, task) in list(self.active_work.items()):
                if task.done():
                    self.active_work.pop(job_id, None)

    async def _run_work(self, job_id, event, callback, guard):
        event = EventContext(event.scope, event.message_id, event.received_at, "work", event.owner,
                             persona=event.persona)
        token = current_event.set(event)
        try:
            if self.closed or not guard():
                self.store.work_state(job_id, "cancelled")
                return
            self.store.work_state(job_id, "running")
            try:
                result = await callback()
                result = event.result if event.result is not None else result
                success = getattr(result, "success", True)
                data = getattr(result, "data", None)
                if isinstance(data, dict):
                    self.store.db.execute("INSERT OR REPLACE INTO work_results VALUES(?,?)",
                        (job_id, json.dumps(data, ensure_ascii=False, default=str)))
                    self.store.db.commit()
                state = data.get("state") if isinstance(data, dict) and data.get("state") in ("ready", "unknown") else "completed" if success else "failed"
                self.store.work_state(job_id, state, getattr(result, "message", ""))
                if self.on_work_done:
                    await self.on_work_done(job_id, event, result)
            except asyncio.CancelledError:
                self.store.work_state(job_id, "cancelled")
                raise
            except Exception as error:
                logger.exception("后台任务失败: %s", job_id)
                self.store.work_state(job_id, "failed", str(error)[:500])
        finally:
            current_event.reset(token)

    def cancel(self, *, scope=None, owner=None, job_id=None):
        def matches(j_id, event):
            if job_id is not None:
                return j_id == job_id and (owner is None or event.owner == owner) and (scope is None or event.scope == scope)
            return (scope is None or event.scope == scope) and (owner is None or event.owner == owner)
        for active_id, (event, task) in list(self.active_work.items()):
            if matches(active_id, event):
                self.store.work_state(active_id, "cancelled")
                task.cancel()
        query = "UPDATE work SET state='cancelled', detail='已取消任务。' WHERE namespace=? AND state IN ('queued', 'ready')"
        params = [self.store.namespace]
        if job_id is not None:
            query += " AND id=?"
            params.append(job_id)
        if owner is not None:
            query += " AND owner=?"
            params.append(owner)
        if scope is not None:
            query += " AND scope=?"
            params.append(scope)
        self.store.db.execute(query, tuple(params))
        self.store.db.commit()

    async def close(self):
        self.closed = True
        self.cancel()
        tasks = list(self.chat_workers.values())
        for task in tasks:
            task.cancel()
        tasks.extend(task for _, task in self.active_work.values())
        if self.work_worker:
            tasks.append(self.work_worker)
        await asyncio.gather(*tasks, return_exceptions=True)


class TextOutbox:
    """Persist before sending; replay pending only, never uncertain sends."""
    def __init__(self, store, connected, send, *, quote_resolver=None, on_reply_sent=None, guard_validator=None):
        self.store, self.connected, self.send = store, connected, send
        self.worker = None
        self.waiters = {}
        self.guards = {}
        self.closed = False
        self.quote_resolver = quote_resolver
        self.on_reply_sent = on_reply_sent
        self.guard_validator = guard_validator

    async def enqueue(self, scope, action, payload, *, guard=lambda: True, source_message_id="", received_at=0,
                      quote_after=8, quote_on_newer=False, reply_meta=None, explicit_quote=False, guard_meta=None):
        key = uuid.uuid4().hex
        outbox_meta = {
            "source_message_id": source_message_id,
            "received_at": received_at,
            "quote_after": quote_after,
            "quote_on_newer": quote_on_newer,
            "reply_meta": reply_meta,
            "explicit_quote": explicit_quote
        }
        if guard_meta:
            outbox_meta["guard_meta"] = guard_meta
        payload = {**payload, "_outbox_metadata": outbox_meta}
        self.store.db.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?)",
            (key, self.store.namespace, scope, action, json.dumps(payload, ensure_ascii=False),
             "pending", "", time.time()))
        self.store.db.commit()
        logger.info("[%s] 正文入发送队列，来源 %s，outbox=%s", scope, source_message_id, key)
        self.guards[key] = guard
        if not self.connected():
            return {"status": "queued", "data": {"outbox_id": key}}
        fut = asyncio.get_running_loop().create_future()
        self.waiters[key] = fut
        self.wake()
        try:
            return await asyncio.shield(fut)
        finally:
            self.waiters.pop(key, None)

    def wake(self):
        if not self.closed and (self.worker is None or self.worker.done()):
            self.worker = asyncio.create_task(self._flush())

    async def _flush(self):
        while not self.closed and self.connected():
            row = self.store.db.execute("SELECT * FROM outbox WHERE namespace=? AND state='pending' "
                # Wall-clock timestamps can tie or move backwards. SQLite's
                # insertion sequence keeps the durable queue in speech order.
                "ORDER BY rowid LIMIT 1", (self.store.namespace,)).fetchone()
            if row is None:
                return
            key = row["id"]
            payload = json.loads(row["payload"])
            metadata = payload.pop("_outbox_metadata", {})
            guard_meta = metadata.get("guard_meta")

            valid = True
            if key in self.guards:
                try:
                    valid = bool(self.guards[key]())
                except Exception as ge:
                    logger.warning("[%s] guard evaluation exception: %s", row["scope"], ge)
                    valid = False
            else:
                if self.guard_validator:
                    try:
                        valid = bool(self.guard_validator(guard_meta, row, metadata))
                    except Exception as ve:
                        logger.warning("[%s] guard_validator exception: %s", row["scope"], ve)
                        valid = False
                else:
                    valid = True

            if not valid:
                logger.info("[%s] 取消失效缓存回复，outbox=%s（会话重置、人设变更或缺少有效性元数据）", row["scope"], key)
                self._finish(key, "cancelled", SendError("会话已重置、换人设或已有同轮补充，缓存回复已取消。"))
                continue

            self.store.db.execute("UPDATE outbox SET state='sending' WHERE id=?", (key,))
            self.store.db.commit()
            try:
                messages = payload.get("message")
                # Quote only when explicitly requested. Older queued messages
                # must not acquire quotes because time passed or chat advanced.
                if isinstance(messages, list) and not metadata.get("explicit_quote"):
                    messages[:] = [seg for seg in messages if seg.get("type") != "reply"]
                auto_quote = bool(isinstance(messages, list) and any(seg.get("type") == "reply" for seg in messages))
                response = await self.send(row["action"], payload)
            except SendUnavailable:
                self.store.db.execute("UPDATE outbox SET state='pending' WHERE id=?", (key,))
                self.store.db.commit()
                fut = self.waiters.get(key)
                if fut and not fut.done():
                    fut.set_result({"status": "queued", "data": {"outbox_id": key}})
                break
            except SendUnknown as error:
                self._finish(key, "unknown", error)
            except asyncio.CancelledError:
                self._finish(key, "unknown", SendUnknown("发送中断，请核对聊天记录。"))
                raise
            except Exception as error:
                self._finish(key, "failed", error)
            else:
                if self.on_reply_sent and metadata.get("reply_meta"):
                    self.on_reply_sent(row["scope"], metadata.get("source_message_id", ""))
                if metadata.get("reply_meta"):
                    from chat_turns import emit_reply
                    emit_reply(**metadata["reply_meta"], quoted=auto_quote)
                if isinstance(response, dict) and metadata.get("quote_on_newer"):
                    response = {**response, "_reply_quoted": auto_quote}
                self._finish(key, "accepted", response)
                logger.info("[%s] QQ 已接受正文，来源 %s，队列至回执 %.2f 秒，outbox=%s", row["scope"],
                            metadata.get("source_message_id", ""), time.time() - row["created"], key)
        # A disconnect can happen while another message is in flight. Release
        # callers for still-pending rows; the durable queue retains the text.
        for key, fut in list(self.waiters.items()):
            row = self.store.db.execute("SELECT state FROM outbox WHERE id=?", (key,)).fetchone()
            if row and row[0] == "pending" and not fut.done():
                fut.set_result({"status": "queued", "data": {"outbox_id": key}})

    def _finish(self, key, state, result):
        self.store.db.execute("UPDATE outbox SET state=?,receipt=? WHERE id=?",
                              (state, json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result), key))
        self.store.db.commit()
        self.guards.pop(key, None)
        fut = self.waiters.get(key)
        if fut and not fut.done():
            if isinstance(result, Exception):
                fut.set_exception(result)
            else:
                fut.set_result(result)

    def cancel(self, scope=None):
        rows = list(self.store.db.execute("SELECT id FROM outbox WHERE namespace=? AND state='pending'" +
            (" AND scope=?" if scope else ""), (self.store.namespace, scope) if scope else (self.store.namespace,)))
        for row in rows:
            self._finish(row["id"], "cancelled", SendError("会话已重置，缓存回复已取消。"))

    async def close(self):
        self.closed = True
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
