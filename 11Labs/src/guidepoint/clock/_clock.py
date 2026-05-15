"""Injectable clock — top-level utility shared by every domain module.

Per ARCHITECTURE rule #8 time is a dependency. Nothing in the codebase
calls ``datetime.now`` directly; every consumer takes a ``Clock`` and
asks it. Tests pass a fake clock (see test helpers) to make timestamps
deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, final


class Clock(Protocol):
    """Returns the current UTC instant."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        ...


def build_system_clock() -> Clock:
    """Real wall-clock implementation."""
    return _SystemClock()


@final
class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


__all__ = ["Clock", "build_system_clock"]
