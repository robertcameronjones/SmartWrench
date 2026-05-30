"""Tests for the SQLite-backed outbound queue.

The queue is content-agnostic: tests pin its ordering, claim atomicity,
state transitions, retry/hold semantics, and crash recovery — not what
the items contain. Per-case ordering falls out of plain FIFO under a
single drainer; the per-case test exercises that property.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from guidepoint.case._models import CaseId
from guidepoint.persistence import (
    OutboundQueue,
    OutboundState,
    build_sqlite_outbound_queue,
)

NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


@pytest.fixture
def queue(tmp_path: Path) -> OutboundQueue:
    """Fresh SQLite queue in a temp directory."""
    return build_sqlite_outbound_queue(db_path=tmp_path / "test.db")


def _enq(
    queue: OutboundQueue,
    *,
    case_id: str = "case_001",
    phone: str = "+15555550100",
    body: str = "hello",
    at: datetime = NOW,
    hold_until: datetime | None = None,
    max_attempts: int = 3,
):
    return queue.enqueue(
        case_id=CaseId(case_id),
        to_phone=phone,
        body=body,
        enqueued_at=at,
        hold_until=hold_until,
        max_attempts=max_attempts,
    )


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


def test_enqueue_persists_item_with_pending_state(queue: OutboundQueue) -> None:
    item = _enq(queue, body="first send")

    assert item.state == OutboundState.PENDING
    assert item.body == "first send"
    assert item.attempts == 0
    assert item.max_attempts == 3
    assert item.twilio_sid == ""
    assert item.sent_at is None
    assert item.claimed_at is None
    # Default hold_until is the enqueue time itself (immediately ready).
    assert item.hold_until == item.enqueued_at


def test_enqueue_assigns_unique_ids(queue: OutboundQueue) -> None:
    a = _enq(queue, body="a")
    b = _enq(queue, body="b")

    assert a.item_id != b.item_id
    assert a.item_id.startswith("out_")
    assert b.item_id.startswith("out_")


def test_enqueue_round_trips_through_get(queue: OutboundQueue) -> None:
    item = _enq(queue, body="round trip")

    fetched = queue.get(item.item_id)
    assert fetched == item


def test_enqueue_respects_explicit_hold_until(queue: OutboundQueue) -> None:
    hold = NOW + timedelta(hours=8)
    item = _enq(queue, hold_until=hold)

    assert item.hold_until == hold


def test_get_returns_none_for_missing_item(queue: OutboundQueue) -> None:
    assert queue.get("out_does_not_exist") is None


# ---------------------------------------------------------------------------
# Claim — atomic, FIFO, gated by hold_until
# ---------------------------------------------------------------------------


def test_claim_returns_none_when_queue_empty(queue: OutboundQueue) -> None:
    assert queue.claim_next_ready(now=NOW) is None


def test_claim_returns_oldest_ready_item_first(queue: OutboundQueue) -> None:
    first = _enq(queue, body="first", at=NOW)
    _enq(queue, body="second", at=NOW + timedelta(seconds=1))
    _enq(queue, body="third", at=NOW + timedelta(seconds=2))

    claimed = queue.claim_next_ready(now=NOW + timedelta(seconds=5))

    assert claimed is not None
    assert claimed.item_id == first.item_id
    assert claimed.body == "first"


def test_claim_flips_state_to_in_flight_and_increments_attempts(queue: OutboundQueue) -> None:
    item = _enq(queue)

    claimed = queue.claim_next_ready(now=NOW + timedelta(seconds=1))

    assert claimed is not None
    assert claimed.state == OutboundState.IN_FLIGHT
    assert claimed.attempts == 1
    assert claimed.claimed_at == NOW + timedelta(seconds=1)
    # The persisted row must also reflect the claim.
    refetched = queue.get(item.item_id)
    assert refetched is not None
    assert refetched.state == OutboundState.IN_FLIGHT


def test_claim_skips_items_held_until_future(queue: OutboundQueue) -> None:
    held = _enq(queue, body="held", hold_until=NOW + timedelta(hours=1))
    ready = _enq(
        queue, body="ready", at=NOW + timedelta(seconds=5)
    )  # enqueued later but ready now

    claimed = queue.claim_next_ready(now=NOW + timedelta(seconds=10))

    assert claimed is not None
    # The held item is older but still on hold; the worker must skip it.
    assert claimed.item_id == ready.item_id
    assert queue.get(held.item_id).state == OutboundState.PENDING  # type: ignore[union-attr]


def test_claim_releases_held_item_once_hold_elapses(queue: OutboundQueue) -> None:
    held = _enq(queue, body="held", hold_until=NOW + timedelta(hours=1))

    # Not yet ready.
    assert queue.claim_next_ready(now=NOW) is None
    # Now ready.
    claimed = queue.claim_next_ready(now=NOW + timedelta(hours=1, seconds=1))

    assert claimed is not None
    assert claimed.item_id == held.item_id


def test_claim_does_not_re_pick_in_flight_item(queue: OutboundQueue) -> None:
    _enq(queue, body="first")
    second = _enq(queue, body="second", at=NOW + timedelta(seconds=1))

    first_claim = queue.claim_next_ready(now=NOW + timedelta(seconds=5))
    second_claim = queue.claim_next_ready(now=NOW + timedelta(seconds=5))

    assert first_claim is not None
    assert second_claim is not None
    assert first_claim.item_id != second_claim.item_id
    assert second_claim.item_id == second.item_id


def test_per_case_fifo_order_preserved_under_single_drainer(
    queue: OutboundQueue,
) -> None:
    """Plain FIFO + single drainer = intra-case ordering for free."""
    a1 = _enq(queue, case_id="case_A", body="A1", at=NOW)
    b1 = _enq(queue, case_id="case_B", body="B1", at=NOW + timedelta(seconds=1))
    a2 = _enq(queue, case_id="case_A", body="A2", at=NOW + timedelta(seconds=2))

    drained = []
    while (item := queue.claim_next_ready(now=NOW + timedelta(seconds=10))):
        drained.append(item.item_id)

    assert drained == [a1.item_id, b1.item_id, a2.item_id]


# ---------------------------------------------------------------------------
# Terminal transitions
# ---------------------------------------------------------------------------


def test_mark_sent_records_sid_and_terminal_state(queue: OutboundQueue) -> None:
    item = _enq(queue)
    queue.claim_next_ready(now=NOW + timedelta(seconds=1))

    sent_at = NOW + timedelta(seconds=2)
    updated = queue.mark_sent(item_id=item.item_id, twilio_sid="SM123abc", sent_at=sent_at)

    assert updated.state == OutboundState.SENT
    assert updated.twilio_sid == "SM123abc"
    assert updated.sent_at == sent_at
    assert updated.state.is_terminal
    assert updated.last_error == ""


def test_mark_blocked_records_reason(queue: OutboundQueue) -> None:
    item = _enq(queue)
    queue.claim_next_ready(now=NOW + timedelta(seconds=1))

    updated = queue.mark_blocked(item_id=item.item_id, reason="customer opted out")

    assert updated.state == OutboundState.BLOCKED
    assert updated.last_error == "customer opted out"
    assert updated.state.is_terminal


def test_mark_failed_records_reason(queue: OutboundQueue) -> None:
    item = _enq(queue, max_attempts=1)
    queue.claim_next_ready(now=NOW + timedelta(seconds=1))

    updated = queue.mark_failed(item_id=item.item_id, last_error="HTTP 500 from Twilio")

    assert updated.state == OutboundState.FAILED
    assert updated.last_error == "HTTP 500 from Twilio"
    assert updated.state.is_terminal


# ---------------------------------------------------------------------------
# Retry — returns to PENDING with future hold_until
# ---------------------------------------------------------------------------


def test_mark_retry_returns_item_to_pending_with_future_hold(queue: OutboundQueue) -> None:
    item = _enq(queue)
    queue.claim_next_ready(now=NOW + timedelta(seconds=1))
    retry_at = NOW + timedelta(minutes=5)

    updated = queue.mark_retry(
        item_id=item.item_id,
        retry_at=retry_at,
        last_error="transient HTTP 503",
    )

    assert updated.state == OutboundState.PENDING
    assert updated.hold_until == retry_at
    assert updated.last_error == "transient HTTP 503"
    # The attempts counter was already incremented at claim time and stays put.
    assert updated.attempts == 1


def test_retried_item_is_re_claimable_once_hold_elapses(queue: OutboundQueue) -> None:
    item = _enq(queue)
    queue.claim_next_ready(now=NOW + timedelta(seconds=1))
    queue.mark_retry(
        item_id=item.item_id,
        retry_at=NOW + timedelta(minutes=5),
        last_error="503",
    )

    # Before hold elapses: nothing ready.
    assert queue.claim_next_ready(now=NOW + timedelta(minutes=1)) is None
    # After hold elapses: same item is claimable again, attempts += 1.
    re_claimed = queue.claim_next_ready(now=NOW + timedelta(minutes=6))

    assert re_claimed is not None
    assert re_claimed.item_id == item.item_id
    assert re_claimed.attempts == 2
    assert re_claimed.state == OutboundState.IN_FLIGHT


# ---------------------------------------------------------------------------
# Crash recovery — reclaim stale in_flight rows
# ---------------------------------------------------------------------------


def test_reclaim_stale_in_flight_returns_items_to_pending(queue: OutboundQueue) -> None:
    item = _enq(queue)
    queue.claim_next_ready(now=NOW)  # claimed_at = NOW
    # Worker "crashed" — item is stuck in IN_FLIGHT.

    # Reclaim everything claimed before NOW + 5min.
    reclaimed = queue.reclaim_stale_in_flight(older_than=NOW + timedelta(minutes=5))

    assert reclaimed == 1
    updated = queue.get(item.item_id)
    assert updated is not None
    assert updated.state == OutboundState.PENDING
    assert updated.claimed_at is None


def test_reclaim_does_not_touch_recent_in_flight(queue: OutboundQueue) -> None:
    item = _enq(queue)
    queue.claim_next_ready(now=NOW + timedelta(minutes=10))  # claimed recently

    reclaimed = queue.reclaim_stale_in_flight(older_than=NOW + timedelta(minutes=5))

    assert reclaimed == 0
    assert queue.get(item.item_id).state == OutboundState.IN_FLIGHT  # type: ignore[union-attr]


def test_reclaim_ignores_terminal_rows(queue: OutboundQueue) -> None:
    item = _enq(queue)
    queue.claim_next_ready(now=NOW)
    queue.mark_sent(item_id=item.item_id, twilio_sid="SMxyz", sent_at=NOW)

    reclaimed = queue.reclaim_stale_in_flight(older_than=NOW + timedelta(hours=1))

    assert reclaimed == 0
    assert queue.get(item.item_id).state == OutboundState.SENT  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Forensics + ops
# ---------------------------------------------------------------------------


def test_list_for_case_returns_items_in_enqueue_order(queue: OutboundQueue) -> None:
    a1 = _enq(queue, case_id="case_A", body="A1", at=NOW)
    b1 = _enq(queue, case_id="case_B", body="B1", at=NOW + timedelta(seconds=1))
    a2 = _enq(queue, case_id="case_A", body="A2", at=NOW + timedelta(seconds=2))

    history = queue.list_for_case(CaseId("case_A"))

    assert [i.item_id for i in history] == [a1.item_id, a2.item_id]
    assert b1.item_id not in {i.item_id for i in history}


def test_pending_depth_counts_only_pending_rows(queue: OutboundQueue) -> None:
    _enq(queue, body="one")
    _enq(queue, body="two", at=NOW + timedelta(seconds=1))
    third = _enq(queue, body="three", at=NOW + timedelta(seconds=2))
    # Simulate a normal worker cycle: claim + mark_sent on the oldest.
    claimed_first = queue.claim_next_ready(now=NOW + timedelta(seconds=5))
    assert claimed_first is not None
    queue.mark_sent(
        item_id=claimed_first.item_id,
        twilio_sid="SM_first",
        sent_at=NOW + timedelta(seconds=6),
    )
    # Claim the second item and leave it IN_FLIGHT (worker still working).
    queue.claim_next_ready(now=NOW + timedelta(seconds=7))

    # 3 enqueued, 1 SENT, 1 IN_FLIGHT, 1 still PENDING (the third row).
    assert queue.pending_depth() == 1
    assert queue.get(third.item_id).state == OutboundState.PENDING  # type: ignore[union-attr]


def test_pending_depth_zero_when_queue_empty(queue: OutboundQueue) -> None:
    assert queue.pending_depth() == 0


# ---------------------------------------------------------------------------
# Persistence across instantiation (the "walled kingdom" survives restart)
# ---------------------------------------------------------------------------


def test_queue_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "persistent.db"
    first = build_sqlite_outbound_queue(db_path=db)
    item = first.enqueue(
        case_id=CaseId("case_persist"),
        to_phone="+15555550100",
        body="durable",
        enqueued_at=NOW,
    )

    second = build_sqlite_outbound_queue(db_path=db)
    refetched = second.get(item.item_id)

    assert refetched is not None
    assert refetched.body == "durable"
    assert refetched.state == OutboundState.PENDING


def test_reopen_can_drain_pending_items_from_prior_session(tmp_path: Path) -> None:
    db = tmp_path / "persistent.db"
    first = build_sqlite_outbound_queue(db_path=db)
    first.enqueue(
        case_id=CaseId("case_persist"),
        to_phone="+15555550100",
        body="durable",
        enqueued_at=NOW,
    )
    # First process exits without draining.

    second = build_sqlite_outbound_queue(db_path=db)
    claimed = second.claim_next_ready(now=NOW + timedelta(minutes=1))

    assert claimed is not None
    assert claimed.body == "durable"
