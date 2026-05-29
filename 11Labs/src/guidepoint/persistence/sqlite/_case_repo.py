"""SQLite-backed ``CaseRepository`` implementation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import final

from guidepoint.case._models import (
    CallAttempt,
    Case,
    CaseEvent,
    CaseId,
    CaseNotFoundError,
    CaseState,
    SlotId,
)
from guidepoint.case._repository import CaseRepository
from guidepoint.master_data import VehicleVin

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_schema.sql"


@final
class SqliteCaseRepository:
    """Persist cases in SQLite with indexed lookup columns."""

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = db_path.resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._init_db()

    def save(self, case: Case) -> None:
        self._write(case)

    def get(self, case_id: CaseId) -> Case:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM cases WHERE case_id = ?",
                (str(case_id),),
            ).fetchone()
        if row is None:
            raise CaseNotFoundError(case_id)
        return Case.model_validate_json(row[0])

    def list_by_state(self, state: CaseState) -> Iterable[Case]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM cases WHERE state = ? ORDER BY case_id",
                (state.value,),
            ).fetchall()
        return tuple(Case.model_validate_json(row[0]) for row in rows)

    def list_recent(self, *, limit: int = 50) -> Iterable[Case]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM cases ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(Case.model_validate_json(row[0]) for row in rows)

    def list_active(self) -> Iterable[Case]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM cases WHERE is_terminal = 0 ORDER BY case_id",
            ).fetchall()
        return tuple(Case.model_validate_json(row[0]) for row in rows)

    def list_by_vehicle_vin(self, vin: VehicleVin) -> Iterable[Case]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM cases WHERE vehicle_vin = ? AND is_terminal = 0 "
                "ORDER BY case_id",
                (str(vin),),
            ).fetchall()
        return tuple(Case.model_validate_json(row[0]) for row in rows)

    def list_by_customer_phone(self, phone: str) -> Iterable[Case]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM cases WHERE customer_phone = ? AND is_terminal = 0 "
                "ORDER BY case_id",
                (phone,),
            ).fetchall()
        return tuple(Case.model_validate_json(row[0]) for row in rows)

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
        booked_slot_display: str = "",
        closed_at: datetime,
    ) -> Case:
        case = self.get(case_id)
        updated = case.model_copy(
            update={
                "state": new_state,
                "outcome_detail": outcome_detail,
                "booked_slot_id": booked_slot_id,
                "booked_slot_display": booked_slot_display,
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

    def _init_db(self) -> None:
        schema = _SCHEMA_PATH.read_text(encoding="utf-8")
        with self._lock, self._connect() as conn:
            conn.executescript(schema)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _write(self, case: Case) -> None:
        payload = case.model_dump_json()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cases (
                    case_id, data, state, customer_phone, vehicle_vin,
                    created_at, is_terminal
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    data = excluded.data,
                    state = excluded.state,
                    customer_phone = excluded.customer_phone,
                    vehicle_vin = excluded.vehicle_vin,
                    created_at = excluded.created_at,
                    is_terminal = excluded.is_terminal
                """,
                (
                    str(case.case_id),
                    payload,
                    case.state.value,
                    case.customer.phone,
                    str(case.vehicle.vin),
                    case.created_at.isoformat(),
                    int(case.state.is_terminal),
                ),
            )
            conn.commit()


def build_sqlite_case_repository(*, db_path: Path) -> CaseRepository:
    """Construct the SQLite ``CaseRepository``."""
    return SqliteCaseRepository(db_path=db_path)


__all__ = [
    "SqliteCaseRepository",
    "build_sqlite_case_repository",
]
