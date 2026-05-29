"""Injectable clock + tz discipline — shared by every domain module.

Per ARCHITECTURE rule #8 time is a dependency. Nothing in the codebase
calls ``datetime.now`` directly; every consumer takes a ``Clock`` and
asks it. Tests pass a fake clock (see test helpers) to make timestamps
deterministic.

This module also publishes the project-wide tz invariants:

- **No naive datetimes anywhere.** Every datetime in the system carries
  a timezone designator. Naive inputs are rejected at the model
  boundary.
- **Storage is always UTC.** Any datetime that hits a persistence layer
  (JSON file, SQLite row, on-the-wire payload) is Zulu.
- **Working values may be local-tz-aware**, but must convert to UTC
  before anything persists them.
- **UI is the conversion boundary** — accepts user-local input, sends
  UTC to the backend; receives UTC, renders to customer-local on
  display.

Pydantic models on the storage boundary annotate datetime fields as
``UtcDatetime`` instead of bare ``datetime``. The custom validator
rejects naive inputs and normalizes any tz-aware input to UTC, so a
working-value with ``tzinfo=America/Detroit`` is converted to UTC
before persistence without the caller having to remember.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Protocol, final

from pydantic import AfterValidator


def _ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes; normalize tz-aware ones to UTC.

    Used by the ``UtcDatetime`` type alias on every persisted-model
    datetime field. The error message explicitly references the
    project's tz discipline so a regression is easy to fix at the call
    site.
    """
    if value.tzinfo is None:
        raise ValueError(
            "naive datetime not allowed (project discipline: storage is "
            "always UTC, working values may be local but must be tz-aware). "
            "Add tzinfo before persisting."
        )
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_ensure_utc)]
"""Pydantic type alias for "must be tz-aware; stored as UTC".

Use everywhere a persisted Pydantic model declares a datetime field.
At the model boundary this rejects naive datetimes (raising
``ValidationError``) and silently normalizes any tz-aware input to
UTC, so callers can hand in working-value local time and trust the
right thing happens at persistence."""


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


__all__ = ["Clock", "UtcDatetime", "build_system_clock"]
