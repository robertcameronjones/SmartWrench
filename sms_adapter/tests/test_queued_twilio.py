"""Tests for ``build_queued_twilio_sender``.

The queued sender bridges the synchronous ``TwilioSend`` shape that
``SmsCallSession`` expects to the asynchronous ``OutboundQueue``. The
tests exercise:

- Happy path: enqueue + worker drains + SID returned.
- Blocked (consent revoked) → :class:`SmsConsentError`.
- Failed (transient errors exhausted) → ``RuntimeError`` with reason.
- Timeout (hours closed, never drained) → :class:`QueueWaitTimeout`,
  but the queue item is still PENDING for later delivery.

The tests run the worker in a separate thread (or tick it manually)
so the sender's blocking poll loop sees real state transitions.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from guidepoint.case._models import CaseId
from guidepoint.outbound import (
    OutboundWorker,
    OutboundWorkerConfig,
    PermanentDispatchError,
)
from guidepoint.persistence import OutboundState, build_sqlite_outbound_queue

from sms_adapter import (
    QueueWaitTimeout,
    SmsConsentError,
    build_queued_twilio_sender,
)

NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Test doubles (mirror the worker tests; kept local to avoid cross-pkg deps)
# ---------------------------------------------------------------------------


class SystemClock:
    """Real wall clock — needed so the sender's blocking poll measures
    real elapsed time against ``max_wait``."""

    def now(self) -> datetime:
        # Anchor against NOW + wall-time offset so timestamps look sensible.
        # We don't actually use this for hold_until comparisons in these
        # tests (worker side uses its own clock); for the sender's wait
        # loop it just needs to advance monotonically.
        return datetime.now(UTC)


class FakeHours:
    def __init__(self, *, open: bool = True) -> None:
        self.open = open

    def hours_open(self) -> bool:
        return self.open


class FakeConsent:
    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow

    def sms_consent_for_phone(self, phone: str) -> bool:
        return self.allow


class RecordingDispatcher:
    def __init__(self, *, script: list | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *, to: str, body: str) -> str:
        self.calls.append((to, body))
        if not self.script:
            return f"SM_default_{len(self.calls)}"
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


def _start_worker_in_thread(worker: OutboundWorker) -> tuple[threading.Thread, asyncio.AbstractEventLoop]:
    """Spawn an event loop in a daemon thread and run the worker on it."""
    loop = asyncio.new_event_loop()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(worker.start())
        loop.run_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread, loop


def _stop_worker_thread(
    worker: OutboundWorker,
    thread: threading.Thread,
    loop: asyncio.AbstractEventLoop,
) -> None:
    fut = asyncio.run_coroutine_threadsafe(worker.stop(), loop)
    fut.result(timeout=10)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=10)


# ---------------------------------------------------------------------------
# Happy path — sender enqueues, worker drains, SID returns
# ---------------------------------------------------------------------------


def test_sender_returns_sid_when_worker_drains(tmp_path: Path) -> None:
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "q.db")
    dispatcher = RecordingDispatcher(script=["SM_happy_path"])
    worker = OutboundWorker(
        queue=queue,
        dispatcher=dispatcher,
        consent=FakeConsent(),
        hours=FakeHours(),
        clock=SystemClock(),
        config=OutboundWorkerConfig(poll_interval=timedelta(milliseconds=50)),
    )
    thread, loop = _start_worker_in_thread(worker)
    try:
        sender = build_queued_twilio_sender(
            queue=queue,
            clock=SystemClock(),
            poll_interval=timedelta(milliseconds=50),
            max_wait=timedelta(seconds=5),
        )
        sid = sender(case_id=CaseId("case_x"), to="+15555550100", body="hello")
    finally:
        _stop_worker_thread(worker, thread, loop)

    assert sid == "SM_happy_path"
    assert dispatcher.calls == [("+15555550100", "hello")]


# ---------------------------------------------------------------------------
# Consent revoked — worker marks BLOCKED, sender raises SmsConsentError
# ---------------------------------------------------------------------------


def test_sender_raises_consent_error_when_blocked(tmp_path: Path) -> None:
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "q.db")
    dispatcher = RecordingDispatcher()
    worker = OutboundWorker(
        queue=queue,
        dispatcher=dispatcher,
        consent=FakeConsent(allow=False),
        hours=FakeHours(),
        clock=SystemClock(),
        config=OutboundWorkerConfig(poll_interval=timedelta(milliseconds=50)),
    )
    thread, loop = _start_worker_in_thread(worker)
    try:
        sender = build_queued_twilio_sender(
            queue=queue,
            clock=SystemClock(),
            poll_interval=timedelta(milliseconds=50),
            max_wait=timedelta(seconds=5),
        )
        with pytest.raises(SmsConsentError):
            sender(case_id=CaseId("case_x"), to="+15555550100", body="hello")
    finally:
        _stop_worker_thread(worker, thread, loop)

    assert dispatcher.calls == []


# ---------------------------------------------------------------------------
# Permanent dispatch error → FAILED → RuntimeError
# ---------------------------------------------------------------------------


def test_sender_raises_runtime_error_on_permanent_failure(tmp_path: Path) -> None:
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "q.db")
    dispatcher = RecordingDispatcher(
        script=[PermanentDispatchError("invalid phone")]
    )
    worker = OutboundWorker(
        queue=queue,
        dispatcher=dispatcher,
        consent=FakeConsent(),
        hours=FakeHours(),
        clock=SystemClock(),
        config=OutboundWorkerConfig(poll_interval=timedelta(milliseconds=50)),
    )
    thread, loop = _start_worker_in_thread(worker)
    try:
        sender = build_queued_twilio_sender(
            queue=queue,
            clock=SystemClock(),
            poll_interval=timedelta(milliseconds=50),
            max_wait=timedelta(seconds=5),
        )
        with pytest.raises(RuntimeError, match="invalid phone"):
            sender(case_id=CaseId("case_x"), to="+1555", body="bad")
    finally:
        _stop_worker_thread(worker, thread, loop)


# ---------------------------------------------------------------------------
# Timeout — hours closed, sender bails but item survives
# ---------------------------------------------------------------------------


def test_sender_times_out_when_hours_closed_but_item_persists(tmp_path: Path) -> None:
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "q.db")
    dispatcher = RecordingDispatcher()
    hours = FakeHours(open=False)
    worker = OutboundWorker(
        queue=queue,
        dispatcher=dispatcher,
        consent=FakeConsent(),
        hours=hours,
        clock=SystemClock(),
        config=OutboundWorkerConfig(poll_interval=timedelta(milliseconds=50)),
    )
    thread, loop = _start_worker_in_thread(worker)
    try:
        sender = build_queued_twilio_sender(
            queue=queue,
            clock=SystemClock(),
            poll_interval=timedelta(milliseconds=50),
            max_wait=timedelta(milliseconds=400),
        )
        with pytest.raises(QueueWaitTimeout):
            sender(case_id=CaseId("case_closed"), to="+15555550100", body="held")
    finally:
        _stop_worker_thread(worker, thread, loop)

    # The dispatcher was never called — hours gate kept it from running.
    assert dispatcher.calls == []
    # But the queue still has the item PENDING (or possibly IN_FLIGHT if
    # the worker grabbed it at the exact moment hours flipped). Either
    # way it's not terminal — the message will go out later.
    items = queue.list_for_case(CaseId("case_closed"))
    assert len(items) == 1
    assert items[0].state in {OutboundState.PENDING, OutboundState.IN_FLIGHT}


def test_held_item_delivers_when_hours_reopen(tmp_path: Path) -> None:
    """Operator promise: closed-hours messages aren't lost."""
    queue = build_sqlite_outbound_queue(db_path=tmp_path / "q.db")
    dispatcher = RecordingDispatcher(script=["SM_after_reopen"])
    hours = FakeHours(open=False)
    worker = OutboundWorker(
        queue=queue,
        dispatcher=dispatcher,
        consent=FakeConsent(),
        hours=hours,
        clock=SystemClock(),
        config=OutboundWorkerConfig(poll_interval=timedelta(milliseconds=50)),
    )
    thread, loop = _start_worker_in_thread(worker)
    try:
        sender = build_queued_twilio_sender(
            queue=queue,
            clock=SystemClock(),
            poll_interval=timedelta(milliseconds=50),
            max_wait=timedelta(milliseconds=300),
        )
        # First call times out; item is still PENDING.
        with pytest.raises(QueueWaitTimeout):
            sender(case_id=CaseId("case_x"), to="+15555550100", body="overnight")

        # Flip hours open — worker drains on its next tick.
        hours.open = True

        # Give the worker a chance to pick the item up.
        for _ in range(40):
            items = queue.list_for_case(CaseId("case_x"))
            if items and items[0].state == OutboundState.SENT:
                break
            import time as _t
            _t.sleep(0.05)
    finally:
        _stop_worker_thread(worker, thread, loop)

    assert dispatcher.calls == [("+15555550100", "overnight")]
    items = queue.list_for_case(CaseId("case_x"))
    assert items[0].state == OutboundState.SENT
    assert items[0].twilio_sid == "SM_after_reopen"
