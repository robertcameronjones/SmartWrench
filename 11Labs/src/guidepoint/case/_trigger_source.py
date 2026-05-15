"""Where Triggers come from.

Production: a monitor task polls the cloud DB for ``status='pending'``
rows and yields them as Trigger objects. After firing, calls
``mark_fired`` so the same row isn't picked up twice.

Simulator: reads ``fixtures/triggers/*.json``, yields the pending ones,
rewrites the file with ``status='fired'`` (and the resulting case_id)
when ``mark_fired`` is called.

Same Protocol either way. Nothing in ``CaseManager`` changes when the
source swaps.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, final

from guidepoint.case._models import (
    CaseId,
    Trigger,
    TriggerId,
    TriggerNotFoundError,
)


class TriggerSource(Protocol):
    """A poller of pending triggers + an acknowledger."""

    def pending(self) -> Iterable[Trigger]:
        """Yield triggers that have not yet been fired (status='pending').

        Caller is responsible for calling ``mark_fired`` (or
        ``mark_failed``) after handling each one.
        """
        ...

    def get(self, trigger_id: TriggerId) -> Trigger:
        """Return one trigger by id; raise ``TriggerNotFoundError`` if missing."""
        ...

    def mark_fired(self, trigger_id: TriggerId, *, case_id: CaseId) -> None:
        """Flip a trigger to ``status='fired'`` and stamp ``case_id``."""
        ...

    def mark_failed(self, trigger_id: TriggerId, *, error_detail: str) -> None:
        """Flip a trigger to ``status='failed'`` with a reason."""
        ...

    def save(self, trigger: Trigger) -> None:
        """Persist (overwrite) one trigger.

        Used by the simulator's UI when the operator edits a trigger.
        Production sources may treat this as a no-op or raise — they
        don't usually own writes.
        """
        ...


@final
@dataclass(frozen=True, slots=True)
class JsonTriggerPaths:
    """On-disk location for the JSON-fixture trigger source."""

    triggers_dir: Path

    @staticmethod
    def for_root(project_root: Path) -> JsonTriggerPaths:
        return JsonTriggerPaths(
            triggers_dir=(project_root / "fixtures" / "triggers").resolve(),
        )


def build_json_trigger_source(*, paths: JsonTriggerPaths) -> TriggerSource:
    """Construct the JSON-file ``TriggerSource`` used by the simulator."""
    return _JsonFileTriggerSource(paths=paths)


@final
class _JsonFileTriggerSource:
    """Reads/writes one JSON file per trigger under ``triggers_dir``."""

    def __init__(self, *, paths: JsonTriggerPaths) -> None:
        self._paths = paths

    def pending(self) -> Iterable[Trigger]:
        for trigger in self._iter_all():
            if trigger.status == "pending":
                yield trigger

    def get(self, trigger_id: TriggerId) -> Trigger:
        path = self._path_for(trigger_id)
        if not path.exists():
            raise TriggerNotFoundError(trigger_id)
        return Trigger.model_validate_json(path.read_text(encoding="utf-8"))

    def mark_fired(self, trigger_id: TriggerId, *, case_id: CaseId) -> None:
        existing = self.get(trigger_id)
        updated = existing.model_copy(
            update={
                "status": "fired",
                "fired_at": datetime.now(UTC),
                "error_detail": f"case_id={case_id}",
            }
        )
        self._write(updated)

    def mark_failed(self, trigger_id: TriggerId, *, error_detail: str) -> None:
        existing = self.get(trigger_id)
        updated = existing.model_copy(update={"status": "failed", "error_detail": error_detail})
        self._write(updated)

    def save(self, trigger: Trigger) -> None:
        self._write(trigger)

    def _iter_all(self) -> Iterable[Trigger]:
        if not self._paths.triggers_dir.exists():
            return
        for path in sorted(self._paths.triggers_dir.glob("*.json")):
            yield Trigger.model_validate_json(path.read_text(encoding="utf-8"))

    def _path_for(self, trigger_id: TriggerId) -> Path:
        return self._paths.triggers_dir / f"{trigger_id}.json"

    def _write(self, trigger: Trigger) -> None:
        self._paths.triggers_dir.mkdir(parents=True, exist_ok=True)
        payload = trigger.model_dump(mode="json")
        text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
        self._path_for(trigger.id).write_text(text + "\n", encoding="utf-8")


__all__ = [
    "JsonTriggerPaths",
    "TriggerSource",
    "build_json_trigger_source",
]
