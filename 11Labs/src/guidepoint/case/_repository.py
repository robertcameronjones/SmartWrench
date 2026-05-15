"""Persistence Protocol for Cases.

Same shape as ``MasterDataRepository``: Protocol + JSON-file impl now,
MySQL impl later behind the same Protocol. The case is stored as one
JSON file per case under ``fixtures/cases/<case_id>.json`` in simulator
mode.

State transitions and event/attempt appends are atomic from the
caller's perspective (file gets rewritten as a whole). For the
simulator's single-process workload that's fine; the production MySQL
implementation will use real transactions.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, final

from guidepoint.case._models import (
    CallAttempt,
    Case,
    CaseEvent,
    CaseId,
    CaseNotFoundError,
    CaseState,
    SlotId,
)


class CaseRepository(Protocol):
    """Persist and retrieve ``Case`` records."""

    def save(self, case: Case) -> None:
        """Initial insert or full overwrite of one case."""
        ...

    def get(self, case_id: CaseId) -> Case:
        """Return one case; raise ``CaseNotFoundError`` if missing."""
        ...

    def list_by_state(self, state: CaseState) -> Iterable[Case]:
        """Return every case currently in the given state."""
        ...

    def list_recent(self, *, limit: int = 50) -> Iterable[Case]:
        """Return the most recently created cases, newest first."""
        ...

    def update_state(self, case_id: CaseId, *, new_state: CaseState) -> Case:
        """Atomic state transition. Returns the updated Case."""
        ...

    def update_outcome(
        self,
        case_id: CaseId,
        *,
        new_state: CaseState,
        outcome_detail: str,
        booked_slot_id: SlotId | None,
        closed_at: datetime,
    ) -> Case:
        """Set terminal outcome fields together with the terminal state."""
        ...

    def append_event(self, case_id: CaseId, event: CaseEvent) -> Case:
        """Append one event to the case's audit log."""
        ...

    def append_call_attempt(self, case_id: CaseId, attempt: CallAttempt) -> Case:
        """Record a completed call attempt on the case."""
        ...


@final
@dataclass(frozen=True, slots=True)
class JsonCasePaths:
    """On-disk location for the JSON-file case repository."""

    cases_dir: Path

    @staticmethod
    def for_root(project_root: Path) -> JsonCasePaths:
        return JsonCasePaths(
            cases_dir=(project_root / "fixtures" / "cases").resolve(),
        )


def build_json_case_repository(*, paths: JsonCasePaths) -> CaseRepository:
    """Construct the JSON-file ``CaseRepository`` used by the simulator."""
    return _JsonFileCaseRepository(paths=paths)


@final
class _JsonFileCaseRepository:
    """One JSON file per case under ``cases_dir``. No indexes, full reads."""

    def __init__(self, *, paths: JsonCasePaths) -> None:
        self._paths = paths

    def save(self, case: Case) -> None:
        self._write(case)

    def get(self, case_id: CaseId) -> Case:
        path = self._path_for(case_id)
        if not path.exists():
            raise CaseNotFoundError(case_id)
        return Case.model_validate_json(path.read_text(encoding="utf-8"))

    def list_by_state(self, state: CaseState) -> Iterable[Case]:
        return tuple(c for c in self._iter_all() if c.state == state)

    def list_recent(self, *, limit: int = 50) -> Iterable[Case]:
        cases = sorted(self._iter_all(), key=lambda c: c.created_at, reverse=True)
        return tuple(cases[:limit])

    def update_state(self, case_id: CaseId, *, new_state: CaseState) -> Case:
        case = self.get(case_id)
        updated = case.model_copy(update={"state": new_state})
        self._write(updated)
        return updated

    def update_outcome(
        self,
        case_id: CaseId,
        *,
        new_state: CaseState,
        outcome_detail: str,
        booked_slot_id: SlotId | None,
        closed_at: datetime,
    ) -> Case:
        case = self.get(case_id)
        updated = case.model_copy(
            update={
                "state": new_state,
                "outcome_detail": outcome_detail,
                "booked_slot_id": booked_slot_id,
                "closed_at": closed_at,
            }
        )
        self._write(updated)
        return updated

    def append_event(self, case_id: CaseId, event: CaseEvent) -> Case:
        case = self.get(case_id)
        updated = case.model_copy(update={"events": (*case.events, event)})
        self._write(updated)
        return updated

    def append_call_attempt(self, case_id: CaseId, attempt: CallAttempt) -> Case:
        case = self.get(case_id)
        updated = case.model_copy(
            update={
                "call_attempts": (*case.call_attempts, attempt),
                "attempt_count": case.attempt_count + 1,
            }
        )
        self._write(updated)
        return updated

    def _iter_all(self) -> Iterable[Case]:
        if not self._paths.cases_dir.exists():
            return ()
        return tuple(
            Case.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(self._paths.cases_dir.glob("*.json"))
        )

    def _path_for(self, case_id: CaseId) -> Path:
        return self._paths.cases_dir / f"{case_id}.json"

    def _write(self, case: Case) -> None:
        self._paths.cases_dir.mkdir(parents=True, exist_ok=True)
        payload = case.model_dump(mode="json")
        text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
        self._path_for(case.case_id).write_text(text + "\n", encoding="utf-8")


__all__ = [
    "CaseRepository",
    "JsonCasePaths",
    "build_json_case_repository",
]
