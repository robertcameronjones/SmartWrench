"""In-process pub/sub for any frozen Pydantic event type.

The bus is generic over the payload type so the ``events`` module
stays a pure utility — it does not depend on any domain module. Each
domain (``case`` today, others tomorrow) parameterizes the bus with
its own event class.

Single uvicorn worker today, so a process-local fan-out is sufficient.
The Protocol is the seam to slot in Redis pub/sub or NATS later
without changing producers or consumers.

Slow consumers get dropped, not the whole bus: each subscriber owns a
bounded ``asyncio.Queue`` and ``put_nowait`` is the only put we use.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol, final

import structlog

# Per-subscriber bounded queue. The bus prefers dropping events on
# slow subscribers over blocking publishers (or worse, blocking the
# whole bus). When a queue fills, ``publish`` emits a structured
# ``queue.overflow.event_bus`` warning so log scrapes can catch
# slow-consumer regressions consistently with other queue overflows
# in the system.
_QUEUE_MAX = 256
_log = structlog.get_logger(__name__)


class EventBus[T](Protocol):
    """Process-local pub/sub. ``T`` is the payload class."""

    async def publish(self, event: T) -> None:
        """Fan ``event`` out to every active subscriber. Never raises."""
        ...

    def subscribe(self) -> AsyncIterator[T]:
        """Async-iterate events until the consumer stops awaiting."""
        ...

    def subscriber_depths(self) -> tuple[tuple[int, int], ...]:
        """Snapshot of every active subscriber's queue depth.

        Returns a tuple of ``(current_depth, max_depth)`` pairs, one
        per live subscriber. Intended for a health / debug endpoint so
        operators can spot a slow consumer before it starts dropping
        events. Synchronous and non-blocking.
        """
        ...


def build_event_bus[T](*, payload_type: type[T]) -> EventBus[T]:
    """Construct the default in-process bus for the given payload type.

    The ``payload_type`` argument is purely a phantom — it lets the
    type checker bind ``T`` from the call site (since Python generics
    cannot infer ``T`` from a no-arg call). It is not stored or used
    at runtime.
    """
    del payload_type
    return _InProcessBus[T]()


@final
class _InProcessBus[T]:
    """asyncio.Queue per subscriber. No cross-process semantics."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[T]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, event: T) -> None:
        async with self._lock:
            targets = list(self._subscribers)
        for queue in targets:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                _log.warning(
                    "queue.overflow.event_bus",
                    payload_type=type(event).__name__,
                    current_depth=queue.qsize(),
                    max_depth=_QUEUE_MAX,
                )

    def subscribe(self) -> AsyncIterator[T]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[T]:
        queue: asyncio.Queue[T] = asyncio.Queue(maxsize=_QUEUE_MAX)
        async with self._registered(queue):
            while True:
                yield await queue.get()

    @asynccontextmanager
    async def _registered(
        self,
        queue: asyncio.Queue[T],
    ) -> AsyncGenerator[None]:
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    def subscriber_depths(self) -> tuple[tuple[int, int], ...]:
        # Snapshot is read without the lock: ``set`` iteration in
        # CPython is safe for read-only access against put/remove from
        # other tasks (we accept a possibly-stale snapshot — the cost
        # of taking the lock here is not worth the freshness).
        return tuple((q.qsize(), _QUEUE_MAX) for q in self._subscribers)


__all__ = ["EventBus", "build_event_bus"]
