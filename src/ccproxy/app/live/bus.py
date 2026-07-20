"""In-process pub/sub for live-update notifications.

A simple ``set[asyncio.Queue]`` of subscribers, with ``publish`` that fans
out events and ``subscribe`` that yields events as an async iterator. The
``EventBus`` is process-local — for multi-process deployments a real
broker (Redis pub/sub, Postgres LISTEN/NOTIFY, etc.) would be required.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger("claude_code_proxy.live.bus")


class EventBus:
    """In-process fan-out of dict events to async subscribers."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def publish(self, event: dict[str, Any]) -> None:
        """Synchronously fan-out an event to every subscriber.

        Each subscriber gets its own copy via ``queue.put_nowait``. If a
        subscriber's queue is full (capacity 1024), the event is dropped for
        that subscriber — they're too slow and we don't want to block.
        """
        # Copy first to avoid mutation during iteration.
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("dropped event for slow subscriber")

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Yield events as they arrive.

        Each subscriber gets its own queue; queues are removed when the
        subscriber's iteration ends (e.g. SSE client disconnects).
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        self._subscribers.add(queue)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
