"""FIFO reply turns, independent per conversation and reentrant per task."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging

logger = logging.getLogger("ReplyQueue")


@dataclass
class _Slot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: asyncio.Task | None = None
    users: int = 0


class ReplyQueue:
    """Keep a complete model turn and its transport receipts in one FIFO slot.

    Media preparation stays outside the slot. Reentrancy belongs to the actual
    task, so child tasks cannot bypass the queue through inherited context.
    """
    def __init__(self):
        self._slots = {}

    @asynccontextmanager
    async def turn(self, key):
        slot = self._slots.setdefault(key, _Slot())
        task = asyncio.current_task()
        if slot.owner is task:
            yield
            return
        slot.users += 1
        started = asyncio.get_running_loop().time()
        if slot.lock.locked():
            logger.info("[%s] 回复排队，前方 %d 轮", key, slot.users - 1)
        try:
            # asyncio.Lock admits waiters in arrival order, including when
            # earlier waiters are cancelled. Always remove idle slots below.
            await slot.lock.acquire()
            slot.owner = task
            try:
                logger.debug("[%s] 开始回复，排队 %.2f 秒", key,
                             asyncio.get_running_loop().time() - started)
                yield
            finally:
                slot.owner = None
                slot.lock.release()
        finally:
            slot.users -= 1
            if not slot.users:
                self._slots.pop(key, None)
