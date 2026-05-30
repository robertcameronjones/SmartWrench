"""Tests for the fire-and-forget ``build_queued_twilio_sender``.

The sender now does three things and only three things:

1. Enqueue the outbound item in the SQLite queue.
2. Optionally wake the worker via ``notify_worker``.
3. Return the queue ``item_id`` — used as the session ``Turn``'s audit
   handle until the worker reports the real Twilio MessageSid via the
   ``OutboundDispatched`` signal.

There is no polling, no waiting for the worker, no timeout. Failure
modes the older sender used to raise (``SmsConsentError``,
``QueueWaitTimeout``) are now worker-side concerns and surface
through structlog warns + queue state — not through the sender call
site.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from guidepoint.case._models import CaseId
from guidepoint.persistence import OutboundQueue, OutboundState, build_sqlite_outbound_queue

from sms_adapter import QueueEnqueueError, build_queued_twilio_sender

NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tiny fakes
# ---------------------------------------------------------------------------


class FrozenClock:
    """Returns a fixed datetime. The sender doesn't need a real clock."""

    def __init__(self, *, t: datetime = NOW) -> None:
        self._t = t

    def now(self) -> datetime:
        return self._t


# ---------------------------------------------------------------------------
# Core fire-and-forget contract
# ---------------------------------------------------------------------------


def test_sender_returns_item_id_immediately(tmp_path: Path) -> None:
    """The sender must not block — it returns the queue item id and is done."""
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "q.db")
    sender = build_queued_twilio_sender(queue=queue, clock=FrozenClock())

    started = time.monotonic()
    result = sender(case_id=CaseId("case_fire_forget"), to="+15555550100", body="hi")
    elapsed = time.monotonic() - started

    assert isinstance(result, str)
    assert result.startswith("out_")
    # Generous bound — the only work is one SQLite INSERT. If this ever
    # creeps over a fraction of a second, something is wrong (e.g. an
    # accidental wait loop has crept back in).
    assert elapsed < 0.25


def test_sender_persists_item_as_pending(tmp_path: Path) -> None:
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "q.db")
    sender = build_queued_twilio_sender(queue=queue, clock=FrozenClock())

    item_id = sender(case_id=CaseId("case_persist"), to="+15555550101", body="persisted")

    stored = queue.get(item_id)
    assert stored is not None
    assert stored.state == OutboundState.PENDING
    assert stored.case_id == CaseId("case_persist")
    assert stored.to_phone == "+15555550101"
    assert stored.body == "persisted"
    assert stored.twilio_sid == ""  # worker fills this in later


# ---------------------------------------------------------------------------
# Worker wake contract
# ---------------------------------------------------------------------------


def test_sender_wakes_worker_via_notify_callback(tmp_path: Path) -> None:
    """Every successful enqueue must invoke ``notify_worker``."""
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "q.db")
    notifications: list[None] = []
    sender = build_queued_twilio_sender(
        queue=queue,
        clock=FrozenClock(),
        notify_worker=lambda: notifications.append(None),
    )

    sender(case_id=CaseId("case_wake"), to="+15555550102", body="ping")
    sender(case_id=CaseId("case_wake"), to="+15555550102", body="pong")

    assert len(notifications) == 2


def test_sender_swallows_notify_failure(tmp_path: Path) -> None:
    """A broken notifier must not break enqueue — the item is already saved."""
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "q.db")

    def _broken() -> None:
        raise RuntimeError("worker is unhappy")

    sender = build_queued_twilio_sender(
        queue=queue,
        clock=FrozenClock(),
        notify_worker=_broken,
    )

    item_id = sender(case_id=CaseId("case_robust"), to="+15555550103", body="ok")

    stored = queue.get(item_id)
    assert stored is not None
    assert stored.state == OutboundState.PENDING


def test_sender_works_without_notify_callback(tmp_path: Path) -> None:
    """``notify_worker`` is optional — tests / standalone uses don't need a worker."""
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "q.db")
    sender = build_queued_twilio_sender(queue=queue, clock=FrozenClock())

    item_id = sender(case_id=CaseId("case_solo"), to="+15555550104", body="alone")

    assert queue.get(item_id) is not None


# ---------------------------------------------------------------------------
# Enqueue failure surfaces as QueueEnqueueError
# ---------------------------------------------------------------------------


def test_sender_raises_queue_enqueue_error_when_queue_breaks(tmp_path: Path) -> None:
    class BrokenQueue:
        def enqueue(self, **kwargs: object) -> object:
            raise RuntimeError("disk on fire")

    sender = build_queued_twilio_sender(
        queue=cast(OutboundQueue, BrokenQueue()),
        clock=FrozenClock(),
    )

    with pytest.raises(QueueEnqueueError, match="disk on fire"):
        sender(case_id=CaseId("case_broken"), to="+15555550105", body="nope")


# ---------------------------------------------------------------------------
# Multiple enqueues land in FIFO claim order
# ---------------------------------------------------------------------------


def test_sequential_sends_create_distinct_items_in_order(tmp_path: Path) -> None:
    """Each call returns a fresh item id; the queue holds them in enqueue order."""
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "q.db")
    clock = FrozenClock()
    sender = build_queued_twilio_sender(queue=queue, clock=clock)

    ids = [
        sender(case_id=CaseId("case_fifo"), to="+15555550110", body="A"),
        sender(case_id=CaseId("case_fifo"), to="+15555550110", body="B"),
        sender(case_id=CaseId("case_fifo"), to="+15555550110", body="C"),
    ]

    assert len(set(ids)) == 3  # all distinct
    listed = queue.list_for_case(CaseId("case_fifo"))
    assert [item.body for item in listed] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# Backward-compat advance hour — confirm there's no hidden sleep / poll
# ---------------------------------------------------------------------------


def test_sender_returns_under_a_second_for_a_burst(tmp_path: Path) -> None:
    """A burst of 50 enqueues should complete in well under a second.

    Regression guard against a future maintainer accidentally putting
    back a wait loop ("just a quick poll, what could go wrong").
    """
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "q.db")
    sender = build_queued_twilio_sender(queue=queue, clock=FrozenClock())

    started = time.monotonic()
    for i in range(50):
        sender(case_id=CaseId(f"case_burst_{i:03d}"), to="+15555550111", body=f"msg {i}")
    elapsed = time.monotonic() - started

    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# max_attempts honored when enqueueing
# ---------------------------------------------------------------------------


def test_sender_passes_through_max_attempts(tmp_path: Path) -> None:
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "q.db")
    sender = build_queued_twilio_sender(
        queue=queue,
        clock=FrozenClock(),
        max_attempts=7,
    )

    item_id = sender(case_id=CaseId("case_attempts"), to="+15555550112", body="x")

    stored = queue.get(item_id)
    assert stored is not None
    assert stored.max_attempts == 7
