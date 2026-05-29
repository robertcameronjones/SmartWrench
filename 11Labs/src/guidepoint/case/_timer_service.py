"""In-process timer wheel for the case driver.

The ``CaseDriver`` arms per-case wall-clock timers via the
``TimerService`` Protocol (``ScheduleTimer`` / ``CancelTimer``
actions from the reducer). This module provides the default
simulator / single-process binding: one-shot ``asyncio`` tasks keyed
by ``(case_id, name)``.

Production can swap in a durable scheduler (cron, Redis, etc.) behind
the same Protocol without touching the reducer or driver.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, final

import structlog

from guidepoint.case._models import CaseId
from guidepoint.clock import Clock, UtcDatetime

_log = structlog.get_logger(__name__)

TimerFireCallback = Callable[[CaseId, str], Awaitable[None] | Coroutine[Any, Any, None]]


@final
class InMemoryTimerService:
    """Asyncio-backed one-shot timers keyed by ``(case_id, name)``.

    Scheduling replaces any prior timer with the same key. When
    ``fire_at`` is already in the past (or equal to ``clock.now()``),
    the callback fires on the next event-loop tick without sleeping.

    The ``fire`` callback is injected at construction — typically a
    closure that maps ``(case_id, name)`` to a ``CaseSignal`` and
    enqueues it on the ``CaseDriver`` (see ``world_bridge`` helpers).
    """

    def __init__(
        self,
        *,
        clock: Clock,
        fire: TimerFireCallback,
    ) -> None:
        self._clock = clock
        self._fire = fire
        self._tasks: dict[tuple[CaseId, str], asyncio.Task[None]] = {}

    def schedule(
        self,
        *,
        case_id: CaseId,
        name: str,
        fire_at: UtcDatetime,
    ) -> None:
        """Arm a one-shot timer, replacing any prior timer with the same name."""

        self.cancel(case_id=case_id, name=name)
        delay = (fire_at - self._clock.now()).total_seconds()
        _log.info(
            "timer.scheduled",
            case_id=case_id,
            name=name,
            fire_at=fire_at.isoformat(),
            delay_seconds=max(delay, 0.0),
        )

        async def _run() -> None:
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await self._fire(case_id, name)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("timer.fire.failed", case_id=case_id, name=name)
            finally:
                self._tasks.pop((case_id, name), None)

        task = asyncio.create_task(_run(), name=f"timer:{case_id}:{name}")
        self._tasks[(case_id, name)] = task

    def cancel(self, *, case_id: CaseId, name: str) -> None:
        """Cancel a previously-armed timer. No-op if none exists."""

        task = self._tasks.pop((case_id, name), None)
        if task is not None and not task.done():
            task.cancel()
            _log.debug("timer.cancelled", case_id=case_id, name=name)

    def pending(self) -> tuple[tuple[CaseId, str], ...]:
        """Snapshot of armed ``(case_id, name)`` pairs (for health checks)."""

        return tuple(self._tasks.keys())


__all__ = [
    "InMemoryTimerService",
    "TimerFireCallback",
]
