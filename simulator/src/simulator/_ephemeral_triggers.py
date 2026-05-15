"""In-memory ``TriggerSource`` for the simulator.

Per the operator's mental model: a trigger is the act of saying "go" —
it has no durable existence before the operator hits Fire. So the
simulator doesn't need a JSON-fixture trigger source at all.

This implementation:

- Holds whatever trigger the fire route just synthesized so
  ``CaseManager`` can call ``mark_fired`` / ``mark_failed`` against it
  without crashing.
- ``pending()`` always yields nothing (no monitor task in the
  simulator — the operator IS the monitor).
- ``save()`` records the trigger so ``CaseManager`` can find it during
  fire().

There is no on-disk audit of fired triggers. The audit lives on the
``Case`` (via its ``events`` log) — that's the durable record.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import final

from guidepoint.case import (
    CaseId,
    Trigger,
    TriggerId,
    TriggerNotFoundError,
)


@final
class EphemeralTriggerSource:
    """Hold one trigger at a time in memory; everything is a no-op afterwards."""

    def __init__(self) -> None:
        self._current: dict[TriggerId, Trigger] = {}

    def pending(self) -> tuple[Trigger, ...]:
        return ()

    def get(self, trigger_id: TriggerId) -> Trigger:
        if trigger_id not in self._current:
            raise TriggerNotFoundError(trigger_id)
        return self._current[trigger_id]

    def save(self, trigger: Trigger) -> None:
        self._current[trigger.id] = trigger

    def mark_fired(self, trigger_id: TriggerId, *, case_id: CaseId) -> None:
        if trigger_id in self._current:
            self._current[trigger_id] = self._current[trigger_id].model_copy(
                update={
                    "status": "fired",
                    "fired_at": datetime.now(UTC),
                    "error_detail": f"case_id={case_id}",
                }
            )

    def mark_failed(self, trigger_id: TriggerId, *, error_detail: str) -> None:
        if trigger_id in self._current:
            self._current[trigger_id] = self._current[trigger_id].model_copy(
                update={"status": "failed", "error_detail": error_detail}
            )


__all__ = ["EphemeralTriggerSource"]
