"""SQLite-backed ``OutboundQueue`` implementation.

Stores rows in the ``outbound_queue`` table defined in ``_schema.sql``.
Shares the same database file as ``SqliteCaseRepository`` so a future
"enqueue + record case event in one transaction" optimisation has a path
forward (today they use separate transactions for simplicity).

Atomic claim
============

SQLite has no ``SELECT ... FOR UPDATE`` but it does support
``UPDATE ... RETURNING`` with a subquery in the ``WHERE`` clause. We use
that to claim the oldest ready item in a single statement, which is
race-free across multiple workers/connections::

    UPDATE outbound_queue
       SET state = 'in_flight', claimed_at = ?
     WHERE item_id = (
         SELECT item_id FROM outbound_queue
          WHERE state = 'pending' AND hold_until <= ?
          ORDER BY enqueued_at
          LIMIT 1
     )
    RETURNING ...;

Per-case FIFO ordering falls out of plain ``ORDER BY enqueued_at`` with
a single worker — new items always land at the tail and the drain
always reads from the head.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import final

from guidepoint.case._models import CaseId
from guidepoint.clock import UtcDatetime
from guidepoint.persistence._outbound import (
    OutboundItem,
    OutboundKind,
    OutboundQueue,
    OutboundState,
)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_schema.sql"


def _generate_item_id() -> str:
    """Queue-assigned item identifier. Short, unique, prefix-tagged."""
    return f"out_{secrets.token_hex(8)}"


def _iso(value: datetime) -> str:
    """Serialise a tz-aware datetime to ISO 8601 UTC."""
    return value.astimezone(UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp back to a tz-aware UTC datetime."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _row_to_item(row: sqlite3.Row) -> OutboundItem:
    """Construct an ``OutboundItem`` from a SQLite row."""
    return OutboundItem(
        item_id=row["item_id"],
        case_id=CaseId(row["case_id"]),
        kind=row["kind"],
        to_phone=row["to_phone"],
        body=row["body"],
        state=OutboundState(row["state"]),
        enqueued_at=_parse_iso(row["enqueued_at"]),
        hold_until=_parse_iso(row["hold_until"]),
        claimed_at=_parse_iso(row["claimed_at"]),
        sent_at=_parse_iso(row["sent_at"]),
        twilio_sid=row["twilio_sid"] or "",
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        last_error=row["last_error"] or "",
    )


@final
class SqliteOutboundQueue:
    """Persistent FIFO queue of outbound customer messages."""

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = db_path.resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._init_db()

    # ------------------------------------------------------------------
    # OutboundQueue protocol
    # ------------------------------------------------------------------

    def enqueue(
        self,
        *,
        case_id: CaseId,
        to_phone: str,
        body: str,
        enqueued_at: UtcDatetime,
        hold_until: UtcDatetime | None = None,
        max_attempts: int = 3,
        kind: OutboundKind = "sms_text",
    ) -> OutboundItem:
        item_id = _generate_item_id()
        effective_hold = hold_until or enqueued_at
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO outbound_queue (
                    item_id, case_id, kind, to_phone, body, state,
                    enqueued_at, hold_until, attempts, max_attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    item_id,
                    str(case_id),
                    kind,
                    to_phone,
                    body,
                    OutboundState.PENDING.value,
                    _iso(enqueued_at),
                    _iso(effective_hold),
                    max_attempts,
                ),
            )
            conn.commit()
        item = self.get(item_id)
        # ``get`` is None-safe; we just inserted so it must exist. The
        # explicit check keeps mypy happy and surfaces any future
        # delete-after-enqueue race as a loud error rather than a None
        # leak into callers.
        if item is None:
            raise RuntimeError(
                f"enqueue inserted {item_id!r} but get() returned None"
            )
        return item

    def claim_next_ready(self, *, now: UtcDatetime) -> OutboundItem | None:
        now_iso = _iso(now)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                UPDATE outbound_queue
                   SET state = 'in_flight', claimed_at = ?, attempts = attempts + 1
                 WHERE item_id = (
                     SELECT item_id FROM outbound_queue
                      WHERE state = 'pending' AND hold_until <= ?
                      ORDER BY enqueued_at
                      LIMIT 1
                 )
                RETURNING item_id, case_id, kind, to_phone, body, state,
                          enqueued_at, hold_until, claimed_at, sent_at,
                          twilio_sid, attempts, max_attempts, last_error
                """,
                (now_iso, now_iso),
            ).fetchone()
            conn.commit()
        return _row_to_item(row) if row is not None else None

    def mark_sent(
        self,
        *,
        item_id: str,
        twilio_sid: str,
        sent_at: UtcDatetime,
    ) -> OutboundItem:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE outbound_queue
                   SET state = ?, twilio_sid = ?, sent_at = ?, last_error = ''
                 WHERE item_id = ?
                """,
                (OutboundState.SENT.value, twilio_sid, _iso(sent_at), item_id),
            )
            conn.commit()
        return self._require(item_id)

    def mark_blocked(self, *, item_id: str, reason: str) -> OutboundItem:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE outbound_queue
                   SET state = ?, last_error = ?
                 WHERE item_id = ?
                """,
                (OutboundState.BLOCKED.value, reason, item_id),
            )
            conn.commit()
        return self._require(item_id)

    def mark_retry(
        self,
        *,
        item_id: str,
        retry_at: UtcDatetime,
        last_error: str,
    ) -> OutboundItem:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE outbound_queue
                   SET state = ?, hold_until = ?, last_error = ?
                 WHERE item_id = ?
                """,
                (
                    OutboundState.PENDING.value,
                    _iso(retry_at),
                    last_error,
                    item_id,
                ),
            )
            conn.commit()
        return self._require(item_id)

    def mark_failed(self, *, item_id: str, last_error: str) -> OutboundItem:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE outbound_queue
                   SET state = ?, last_error = ?
                 WHERE item_id = ?
                """,
                (OutboundState.FAILED.value, last_error, item_id),
            )
            conn.commit()
        return self._require(item_id)

    def reclaim_stale_in_flight(self, *, older_than: UtcDatetime) -> int:
        cutoff = _iso(older_than)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE outbound_queue
                   SET state = ?, claimed_at = NULL
                 WHERE state = ? AND claimed_at IS NOT NULL AND claimed_at < ?
                """,
                (OutboundState.PENDING.value, OutboundState.IN_FLIGHT.value, cutoff),
            )
            conn.commit()
            return cur.rowcount

    def get(self, item_id: str) -> OutboundItem | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM outbound_queue WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        return _row_to_item(row) if row is not None else None

    def list_for_case(self, case_id: CaseId) -> tuple[OutboundItem, ...]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM outbound_queue
                 WHERE case_id = ?
                 ORDER BY enqueued_at, item_id
                """,
                (str(case_id),),
            ).fetchall()
        return tuple(_row_to_item(r) for r in rows)

    def pending_depth(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM outbound_queue WHERE state = ?",
                (OutboundState.PENDING.value,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require(self, item_id: str) -> OutboundItem:
        item = self.get(item_id)
        if item is None:
            raise KeyError(f"outbound_queue: no item with id {item_id!r}")
        return item

    def _init_db(self) -> None:
        schema = _SCHEMA_PATH.read_text(encoding="utf-8")
        with self._lock, self._connect() as conn:
            conn.executescript(schema)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


def build_sqlite_outbound_queue(*, db_path: Path) -> OutboundQueue:
    """Construct the SQLite-backed ``OutboundQueue``."""
    return SqliteOutboundQueue(db_path=db_path)


__all__ = [
    "SqliteOutboundQueue",
    "build_sqlite_outbound_queue",
]
