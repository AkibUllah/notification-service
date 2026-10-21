"""In-process fan-out of new inbox messages to SSE subscribers, keyed by user id."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import contextmanager
from typing import Any


class InboxBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)

    @contextmanager
    def subscribe(self, user_id: str):
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subscribers[user_id].append(queue)
        try:
            yield queue
        finally:
            self._subscribers[user_id].remove(queue)

    def publish(self, user_id: str, event: dict[str, Any]) -> int:
        delivered = 0
        for queue in list(self._subscribers.get(user_id, [])):
            try:
                queue.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                continue
        return delivered

    def subscriber_count(self) -> int:
        return sum(len(v) for v in self._subscribers.values())
