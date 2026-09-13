"""Bounded, serial AI work that never holds Telegram's update handler open."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from time import monotonic

logger = logging.getLogger(__name__)
Operation = Callable[[], Awaitable[None]]


class OwnerAiWorkQueue:
    """Keep dialogue order and one provider call chain in flight per process.

    Runtime objects stay outside persisted bot_data. Application.create_task
    tracks the worker so a normal shutdown waits for accepted work.
    """

    def __init__(self, *, capacity=8, per_conversation=3, max_wait_seconds=90):
        self.capacity = capacity
        self.per_conversation = per_conversation
        self.max_wait_seconds = max_wait_seconds
        self._pending = deque()
        self._counts = Counter()
        self._worker = None

    def submit(self, key, operation: Operation, on_expired: Operation, *, create_task):
        if sum(self._counts.values()) >= self.capacity:
            return False
        if self._counts[key] >= self.per_conversation:
            return False
        self._counts[key] += 1
        self._pending.append((key, monotonic(), operation, on_expired))
        if self._worker is None or self._worker.done():
            self._worker = create_task(self._drain())
        return True

    async def _drain(self):
        while self._pending:
            key, accepted_at, operation, on_expired = self._pending.popleft()
            try:
                if monotonic() - accepted_at > self.max_wait_seconds:
                    await on_expired()
                else:
                    await operation()
            except asyncio.CancelledError:
                self._pending.clear()
                self._counts.clear()
                raise
            except Exception as exc:
                # Never log message text or provider credentials.
                logger.error("owner_ai_worker_failed error_type=%s", type(exc).__name__)
            finally:
                if key in self._counts:
                    self._counts[key] -= 1
                    if not self._counts[key]:
                        del self._counts[key]
