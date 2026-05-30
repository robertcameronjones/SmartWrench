"""Tests for ``OutboundWorker``.

Uses fakes for the three ports (queue, dispatcher, consent, hours) so
the worker logic is exercised without any real I/O. Time is driven by a
controllable :class:`FakeClock` so backoff calculations are exact.

Properties pinned:

- Hours-closed → no claim, no dispatch.
- Hours-open + consent-revoked → BLOCKED, no dispatch.
- Happy path → SENT with Twilio sid.
- Permanent error → FAILED with reason.
- Transient error → retry with backoff; exhausted → FAILED.
- Crash recovery via startup reclaim.
- FIFO ordering preserved across multiple ticks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from guidepoint.case._models import CaseId
from guidepoint.outbound import (
    OutboundWorker,
    OutboundWorkerConfig,
    PermanentDispatchError,
    TransientDispatchError,
)
from guidepoint.persistence import OutboundState, build_sqlite_outbound_queue

NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """Controllable clock for deterministic backoff testing."""

    def __init__(self, *, start: datetime = NOW) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def advance(self, delta: timedelta) -> None:
        self._t += delta


class FakeHours:
    """Toggleable business hours gate."""

    def __init__(self, *, open: bool = True) -> None:
        self.open = open

    def hours_open(self) -> bool:
        return self.open


class FakeConsent:
    """Per-phone allow/deny consent gate."""

    def __init__(self, *, default_allow: bool = True) -> None:
        self._default = default_allow
        self._explicit: dict[str, bool] = {}

    def revoke(self, phone: str) -> None:
        self._explicit[phone] = False

    def grant(self, phone: str) -> None:
        self._explicit[phone] = True

    def sms_consent_for_phone(self, phone: str) -> bool:
        return self._explicit.get(phone, self._default)


class RecordingDispatcher:
    """Captures every send call; behaviour driven by ``script``.

    ``script`` is a list of either:
        - a str  → return that string as the Twilio sid
        - an exception instance → raise it
    Items are consumed in order.
    """

    def __init__(self, *, script: list[str | Exception] | None = None) -> None:
        self.script: list[str | Exception] = list(script or [])
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *, to: str, body: str) -> str:
        self.calls.append((to, body))
        if not self.script:
            return f"SM_default_{len(self.calls)}"
        next_action = self.script.pop(0)
        if isinstance(next_action, Exception):
            raise next_action
        return next_action


def _build_worker(
    tmp_path: Path,
    *,
    dispatcher: RecordingDispatcher | None = None,
    consent: FakeConsent | None = None,
    hours: FakeHours | None = None,
    clock: FakeClock | None = None,
    config: OutboundWorkerConfig | None = None,
):
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "worker.db")
    worker = OutboundWorker(
        queue=queue,
        dispatcher=dispatcher or RecordingDispatcher(),
        consent=consent or FakeConsent(),
        hours=hours or FakeHours(),
        clock=clock or FakeClock(),
        config=config or OutboundWorkerConfig(),
    )
    return queue, worker


def _enq(queue, *, body: str = "hello", phone: str = "+15555550100", at: datetime = NOW):
    return queue.enqueue(
        case_id=CaseId("case_001"),
        to_phone=phone,
        body=body,
        enqueued_at=at,
    )


# ---------------------------------------------------------------------------
# Hours gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hours_closed_skips_claim_and_dispatch(tmp_path: Path) -> None:
    dispatcher = RecordingDispatcher()
    hours = FakeHours(open=False)
    queue, worker = _build_worker(tmp_path, dispatcher=dispatcher, hours=hours)
    item = _enq(queue)

    result = await worker.tick()

    assert result is None
    assert dispatcher.calls == []
    # Item remains PENDING — no claim happened, attempts stays 0.
    refetched = queue.get(item.item_id)
    assert refetched is not None
    assert refetched.state == OutboundState.PENDING
    assert refetched.attempts == 0


@pytest.mark.asyncio
async def test_hours_reopen_drains_held_queue(tmp_path: Path) -> None:
    dispatcher = RecordingDispatcher(script=["SM_release_1"])
    hours = FakeHours(open=False)
    queue, worker = _build_worker(tmp_path, dispatcher=dispatcher, hours=hours)
    _enq(queue, body="held while closed")

    # Closed → held.
    assert await worker.tick() is None
    assert dispatcher.calls == []

    # Slider flips open → next tick drains.
    hours.open = True
    result = await worker.tick()

    assert result is not None
    assert result.state == OutboundState.SENT
    assert dispatcher.calls == [("+15555550100", "held while closed")]


# ---------------------------------------------------------------------------
# Consent gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consent_revoked_blocks_item_without_dispatch(tmp_path: Path) -> None:
    dispatcher = RecordingDispatcher()
    consent = FakeConsent()
    consent.revoke("+15555550100")
    queue, worker = _build_worker(tmp_path, dispatcher=dispatcher, consent=consent)
    item = _enq(queue)

    result = await worker.tick()

    assert result is not None
    assert result.state == OutboundState.BLOCKED
    assert "consent revoked" in result.last_error
    assert dispatcher.calls == []
    assert queue.get(item.item_id).state == OutboundState.BLOCKED  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_dispatch_marks_sent_with_twilio_sid(tmp_path: Path) -> None:
    dispatcher = RecordingDispatcher(script=["SMxxxx"])
    queue, worker = _build_worker(tmp_path, dispatcher=dispatcher)
    item = _enq(queue, body="hello world")

    result = await worker.tick()

    assert result is not None
    assert result.state == OutboundState.SENT
    assert result.twilio_sid == "SMxxxx"
    assert result.sent_at is not None
    assert dispatcher.calls == [("+15555550100", "hello world")]


@pytest.mark.asyncio
async def test_fifo_order_preserved_across_ticks(tmp_path: Path) -> None:
    """Plain FIFO via single-worker drain. Clock is past all hold_until."""
    dispatcher = RecordingDispatcher(script=["SM_a", "SM_b", "SM_c"])
    # Worker clock starts well after the enqueue times so every item is
    # immediately ready — we are testing ordering, not hold timing.
    clock = FakeClock(start=NOW + timedelta(minutes=1))
    queue, worker = _build_worker(tmp_path, dispatcher=dispatcher, clock=clock)
    _enq(queue, body="A", at=NOW)
    _enq(queue, body="B", at=NOW + timedelta(seconds=1))
    _enq(queue, body="C", at=NOW + timedelta(seconds=2))

    for _ in range(3):
        await worker.tick()

    assert [body for _, body in dispatcher.calls] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# Errors — permanent + transient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permanent_error_marks_failed_immediately(tmp_path: Path) -> None:
    dispatcher = RecordingDispatcher(
        script=[PermanentDispatchError("invalid phone format")]
    )
    queue, worker = _build_worker(tmp_path, dispatcher=dispatcher)
    item = _enq(queue)

    result = await worker.tick()

    assert result is not None
    assert result.state == OutboundState.FAILED
    assert "invalid phone format" in result.last_error
    assert "permanent" in result.last_error
    # Only one attempt, no retry.
    assert result.attempts == 1


@pytest.mark.asyncio
async def test_transient_error_requeues_with_backoff(tmp_path: Path) -> None:
    dispatcher = RecordingDispatcher(script=[TransientDispatchError("HTTP 503")])
    clock = FakeClock()
    config = OutboundWorkerConfig(
        initial_backoff=timedelta(seconds=2),
        max_backoff=timedelta(minutes=5),
    )
    queue, worker = _build_worker(
        tmp_path, dispatcher=dispatcher, clock=clock, config=config
    )
    item = _enq(queue)

    result = await worker.tick()

    assert result is not None
    assert result.state == OutboundState.PENDING
    assert result.attempts == 1
    # 2s backoff after first failure.
    assert result.hold_until == NOW + timedelta(seconds=2)
    assert "HTTP 503" in result.last_error


@pytest.mark.asyncio
async def test_transient_backoff_doubles_per_attempt(tmp_path: Path) -> None:
    dispatcher = RecordingDispatcher(
        script=[
            TransientDispatchError("503"),
            TransientDispatchError("503"),
            TransientDispatchError("503"),
        ]
    )
    clock = FakeClock()
    config = OutboundWorkerConfig(
        initial_backoff=timedelta(seconds=2),
        max_backoff=timedelta(minutes=5),
    )
    queue, worker = _build_worker(
        tmp_path,
        dispatcher=dispatcher,
        clock=clock,
        config=config,
    )
    item = _enq(queue, body="retry me")
    # max_attempts default is 3.

    # Attempt 1.
    await worker.tick()
    after_first = queue.get(item.item_id)
    assert after_first is not None
    assert after_first.hold_until == NOW + timedelta(seconds=2)

    # Advance the clock past the hold and try again.
    clock.advance(timedelta(seconds=3))
    await worker.tick()
    after_second = queue.get(item.item_id)
    assert after_second is not None
    # Second failure → 4s backoff from the new "now".
    assert after_second.hold_until == clock.now() + timedelta(seconds=4)

    # Final attempt — budget exhausted (attempts will hit max_attempts=3).
    clock.advance(timedelta(seconds=5))
    await worker.tick()
    final = queue.get(item.item_id)
    assert final is not None
    assert final.state == OutboundState.FAILED
    assert "exhausted" in final.last_error


@pytest.mark.asyncio
async def test_generic_exception_treated_as_transient(tmp_path: Path) -> None:
    """An unknown exception is requeued, not dropped."""
    dispatcher = RecordingDispatcher(script=[ValueError("twilio sdk barfed")])
    queue, worker = _build_worker(tmp_path, dispatcher=dispatcher)
    item = _enq(queue)

    result = await worker.tick()

    assert result is not None
    assert result.state == OutboundState.PENDING
    assert "twilio sdk barfed" in result.last_error
    assert result.attempts == 1


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_reclaims_stale_in_flight_rows(tmp_path: Path) -> None:
    db = tmp_path / "recover.db"
    queue = build_sqlite_outbound_queue(db_path=db)
    queue.enqueue(
        case_id=CaseId("case_recover"),
        to_phone="+15555550100",
        body="crashed mid-send",
        enqueued_at=NOW,
    )
    # Simulate a worker that claimed but never finished.
    queue.claim_next_ready(now=NOW)

    # New process spawns a fresh worker, time has advanced.
    later_clock = FakeClock(start=NOW + timedelta(minutes=10))
    config = OutboundWorkerConfig(reclaim_after=timedelta(minutes=1))
    fresh_queue = build_sqlite_outbound_queue(db_path=db)
    dispatcher = RecordingDispatcher(script=["SM_recovered"])
    worker = OutboundWorker(
        queue=fresh_queue,
        dispatcher=dispatcher,
        consent=FakeConsent(),
        hours=FakeHours(),
        clock=later_clock,
        config=config,
    )

    await worker.start()
    # Give the worker one real tick window to drain.
    try:
        # Manual tick is the deterministic path; start() also drained on entry.
        result = await worker.tick()
    finally:
        await worker.stop()

    assert result is not None
    assert result.state == OutboundState.SENT
    assert dispatcher.calls == [("+15555550100", "crashed mid-send")]


# ---------------------------------------------------------------------------
# Lifecycle (start/stop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_stop_clean(tmp_path: Path) -> None:
    queue, worker = _build_worker(tmp_path)

    await worker.start()
    await worker.stop()

    # Idempotent — calling stop twice should not raise.
    await worker.stop()


@pytest.mark.asyncio
async def test_start_twice_raises(tmp_path: Path) -> None:
    queue, worker = _build_worker(tmp_path)

    await worker.start()
    try:
        with pytest.raises(RuntimeError, match="start called twice"):
            await worker.start()
    finally:
        await worker.stop()
