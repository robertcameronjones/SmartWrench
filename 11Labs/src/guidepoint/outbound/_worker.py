"""Outbound worker — edge-triggered drain of the outbound queue.

The worker holds one :class:`asyncio.Event` ("wake") and does nothing
until something sets it. The two legitimate setters are:

- :meth:`OutboundWorker.notify` — called by the sender after an
  ``enqueue`` so a freshly-arrived item is processed immediately.
- :meth:`OutboundWorker.notify` again — called by the simulator
  slider (or the production business-hours service) the instant the
  gate flips ``open=True`` so held items drain immediately.

There is **no polling**. The worker sleeps until a wake, checks the
business-hours boolean, and either drains every ready item in FIFO
order or goes back to sleep. Transient-failure retries self-schedule
their own wake via ``loop.call_later`` so even backoff is edge-driven.

Result reporting is push, not pull. After each successful dispatch
the worker calls an async ``on_dispatched`` callback (typically wired
to :meth:`CaseDriver.on_signal`) with a fresh :class:`OutboundDispatched`
signal carrying the case id, item id, Twilio MessageSid, and target
phone. The case driver routes that into the case's existing signal
queue; the reducer audits it with a ``RecordEvent`` action — no state
transition.

Blocked sends (consent revoked between enqueue and dispatch) and
failed sends (transient errors exhausted) are logged at warn level
via structlog only. The state machine has its own paths for both
(``CustomerOptedOut``, session inactivity timeout → ``CallEnded``);
duplicate signalling would just create noise.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import final

import structlog

from guidepoint.case._signals import OutboundDispatched
from guidepoint.clock import Clock
from guidepoint.outbound._ports import (
    BusinessHoursPort,
    OutboundDispatcher,
    SmsConsentPort,
)
from guidepoint.persistence._outbound import OutboundItem, OutboundQueue

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Dispatcher-raised error vocabulary
# ---------------------------------------------------------------------------


class TransientDispatchError(RuntimeError):
    """Channel dispatch failed in a way worth retrying.

    HTTP 5xx, timeout, rate limit. The worker requeues with backoff
    and schedules a wake at the retry time.
    """


class PermanentDispatchError(RuntimeError):
    """Channel dispatch failed in a way retries can't fix.

    Invalid phone format, account suspended, message exceeds channel
    limits. The worker marks the item ``FAILED`` immediately.
    """


# ---------------------------------------------------------------------------
# Config + callbacks
# ---------------------------------------------------------------------------


#: Async callback the worker invokes after each successful dispatch. The
#: signal it receives is already a typed ``OutboundDispatched`` ready to
#: feed into ``CaseDriver.on_signal``. None disables push-reporting.
OnDispatchedCallback = Callable[[OutboundDispatched], Awaitable[None]]


@final
@dataclass(frozen=True, slots=True)
class OutboundWorkerConfig:
    """Tunables for :class:`OutboundWorker`. Frozen so they round-trip safely.

    Notably *no* poll interval: the worker is event-driven end to end.
    """

    #: Initial backoff after a transient dispatch error. Doubles per attempt.
    initial_backoff: timedelta = timedelta(seconds=2)

    #: Cap on the backoff window so it doesn't grow unbounded.
    max_backoff: timedelta = timedelta(minutes=5)

    #: An ``IN_FLIGHT`` row whose ``claimed_at`` is older than this on
    #: worker startup is considered orphaned and reclaimed to ``PENDING``.
    reclaim_after: timedelta = timedelta(minutes=1)


@final
class OutboundWorker:
    """Edge-triggered drain of the outbound queue."""

    def __init__(
        self,
        *,
        queue: OutboundQueue,
        dispatcher: OutboundDispatcher,
        consent: SmsConsentPort,
        hours: BusinessHoursPort,
        clock: Clock,
        config: OutboundWorkerConfig | None = None,
        on_dispatched: OnDispatchedCallback | None = None,
    ) -> None:
        self._queue = queue
        self._dispatcher = dispatcher
        self._consent = consent
        self._hours = hours
        self._clock = clock
        self._config = config or OutboundWorkerConfig()
        self._on_dispatched = on_dispatched
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def notify(self) -> None:
        """Wake the worker. Idempotent.

        Call from the worker's event loop. Cross-thread callers must
        use :meth:`notify_threadsafe` instead — ``asyncio.Event.set`` is
        not thread-safe.
        """
        self._wake.set()

    def notify_threadsafe(self) -> None:
        """Wake the worker from another thread. Idempotent.

        Schedules :meth:`notify` on the worker's event loop via
        ``loop.call_soon_threadsafe``. Safe to call before
        :meth:`start` — falls back to setting the event directly
        (worker isn't running yet so it'll see the set on first wait).
        """
        loop = self._loop
        if loop is None:
            self._wake.set()
            return
        loop.call_soon_threadsafe(self._wake.set)

    def set_on_dispatched(self, callback: OnDispatchedCallback | None) -> None:
        """Install (or clear) the post-dispatch callback.

        Useful when the case driver — which owns the callback's target
        (``case_driver.on_signal``) — is built after the worker. The
        simulator wiring layer takes advantage of this to break the
        worker ↔ driver circular dependency.
        """
        self._on_dispatched = callback

    async def start(self) -> None:
        """Reclaim stale rows, spawn the run task, and pre-wake once.

        The pre-wake ensures any items already PENDING in the queue (from
        a prior process, or from enqueues that landed between worker
        startup boundaries) are drained without waiting for a new event.
        """
        if self._task is not None and not self._task.done():
            raise RuntimeError("OutboundWorker.start called twice")
        cutoff = self._clock.now() - self._config.reclaim_after
        reclaimed = self._queue.reclaim_stale_in_flight(older_than=cutoff)
        if reclaimed:
            _log.info("outbound.worker.startup.reclaimed", count=reclaimed)
        self._stopped = False
        self._loop = asyncio.get_running_loop()
        self._wake.set()
        self._task = asyncio.create_task(self._run(), name="outbound-worker")

    async def stop(self) -> None:
        """Ask the worker to finish + exit. Idempotent."""
        self._stopped = True
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
                with suppress(BaseException):
                    await self._task
            self._task = None

    # ------------------------------------------------------------------
    # Test-friendly entry points — one synchronous drain pass per call.
    # ------------------------------------------------------------------

    async def tick(self) -> OutboundItem | None:
        """Process exactly one ready item (or no-op). Used by tests.

        Returns the processed item (in its final state) or ``None`` if
        the gate was closed or nothing was ready.
        """
        if not self._hours.hours_open():
            return None
        item = self._queue.claim_next_ready(now=self._clock.now())
        if item is None:
            return None
        return await self._process(item)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Main loop. Sleeps until ``notify`` then drains."""
        while not self._stopped:
            await self._wake.wait()
            self._wake.clear()
            if self._stopped:
                break
            if not self._hours.hours_open():
                # Gate is closed; sit and wait for the next wake. The
                # slider's ``put_business_hours`` will set the event the
                # moment it flips open.
                continue
            await self._drain_ready()

    async def _drain_ready(self) -> None:
        """Claim + process every ready item until the queue is empty or hours close."""
        while not self._stopped and self._hours.hours_open():
            item = self._queue.claim_next_ready(now=self._clock.now())
            if item is None:
                return
            try:
                await self._process(item)
            except Exception:  # noqa: BLE001 - worker must never die
                _log.exception(
                    "outbound.worker.process_errored",
                    item_id=item.item_id,
                )

    async def _process(self, item: OutboundItem) -> OutboundItem:
        """Dispatch one claimed item; return its final-state row."""
        # Consent race guard. The case state machine already terminates
        # opted-out cases via ``CustomerOptedOut``; this catches the
        # narrow window where a STOP arrived between enqueue and the
        # worker getting to the item. Block silently — the case is
        # already being torn down, no need to signal it again.
        if not self._consent.sms_consent_for_phone(item.to_phone):
            blocked = self._queue.mark_blocked(
                item_id=item.item_id,
                reason=f"consent revoked for {item.to_phone}",
            )
            _log.warning(
                "outbound.worker.blocked",
                item_id=item.item_id,
                case_id=str(item.case_id),
                to=item.to_phone,
            )
            return blocked

        try:
            sid = await asyncio.to_thread(
                self._dispatcher, to=item.to_phone, body=item.body
            )
        except PermanentDispatchError as exc:
            failed = self._queue.mark_failed(
                item_id=item.item_id,
                last_error=f"permanent: {exc}",
            )
            _log.warning(
                "outbound.worker.failed.permanent",
                item_id=item.item_id,
                case_id=str(item.case_id),
                error=str(exc),
            )
            return failed
        except (TransientDispatchError, Exception) as exc:  # noqa: BLE001
            return self._handle_transient(item, exc)

        sent = self._queue.mark_sent(
            item_id=item.item_id,
            twilio_sid=sid,
            sent_at=self._clock.now(),
        )
        _log.info(
            "outbound.worker.sent",
            item_id=item.item_id,
            case_id=str(item.case_id),
            to=item.to_phone,
            twilio_sid=sid,
            attempts=item.attempts,
        )
        # Push the "I sent it" signal back into the case driver's
        # existing signal queue. This is the single result-reporting
        # path; the reducer audits it and does not change state.
        if self._on_dispatched is not None:
            try:
                await self._on_dispatched(
                    OutboundDispatched(
                        timestamp=self._clock.now(),
                        case_id=sent.case_id,
                        item_id=sent.item_id,
                        twilio_sid=sent.twilio_sid,
                        to_phone=sent.to_phone,
                    )
                )
            except Exception:  # noqa: BLE001 - reporting must not break sending
                _log.exception(
                    "outbound.worker.on_dispatched_errored",
                    item_id=sent.item_id,
                )
        return sent

    def _handle_transient(
        self, item: OutboundItem, exc: BaseException
    ) -> OutboundItem:
        """Retry with backoff or mark FAILED if attempts are exhausted.

        On retry, schedule a wake at the retry time so the worker
        re-considers the item without polling.
        """
        if item.attempts >= item.max_attempts:
            failed = self._queue.mark_failed(
                item_id=item.item_id,
                last_error=f"transient (exhausted): {type(exc).__name__}: {exc}",
            )
            _log.warning(
                "outbound.worker.failed.exhausted",
                item_id=item.item_id,
                case_id=str(item.case_id),
                attempts=item.attempts,
                max_attempts=item.max_attempts,
                error=str(exc),
            )
            return failed
        backoff = self._backoff_for_attempt(item.attempts)
        retry_at = self._clock.now() + backoff
        requeued = self._queue.mark_retry(
            item_id=item.item_id,
            retry_at=retry_at,
            last_error=f"transient: {type(exc).__name__}: {exc}",
        )
        _log.info(
            "outbound.worker.retry",
            item_id=item.item_id,
            case_id=str(item.case_id),
            attempts=item.attempts,
            retry_at=retry_at.isoformat(),
            error=str(exc),
        )
        # Self-schedule a wake at retry_at so we don't need a polling
        # loop to discover items have come back to PENDING.
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(backoff.total_seconds(), self._wake.set)
        except RuntimeError:
            # Not in an event loop (e.g., synchronous tick() under a
            # test that doesn't keep the loop spinning). Tests that
            # care about retry timing drive notify() explicitly.
            pass
        return requeued

    def _backoff_for_attempt(self, attempts: int) -> timedelta:
        """Exponential backoff capped at ``max_backoff``."""
        seconds = self._config.initial_backoff.total_seconds()
        for _ in range(max(0, attempts - 1)):
            seconds *= 2
        capped = min(seconds, self._config.max_backoff.total_seconds())
        return timedelta(seconds=capped)


__all__ = [
    "OnDispatchedCallback",
    "OutboundWorker",
    "OutboundWorkerConfig",
    "PermanentDispatchError",
    "TransientDispatchError",
]
