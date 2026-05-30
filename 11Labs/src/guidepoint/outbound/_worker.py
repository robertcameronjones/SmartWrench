"""Outbound worker — polls the queue, applies gates, dispatches.

The worker is a tiny asyncio task. On every tick it:

1. Asks the :class:`BusinessHoursPort` whether sending is allowed.
   If not, sleeps the poll interval and re-checks.
2. Claims the oldest ready item from the :class:`OutboundQueue`. If
   nothing is ready, sleeps and re-polls.
3. Checks :class:`SmsConsentPort` for the item's destination phone.
   If consent is gone, marks the item ``BLOCKED`` and continues.
4. Invokes the channel :class:`OutboundDispatcher`. On success, marks
   the item ``SENT`` with the channel-assigned id (Twilio's MessageSid
   for SMS). On a :class:`PermanentDispatchError`, marks ``FAILED``.
   On a :class:`TransientDispatchError` (or any other exception),
   either retries with exponential backoff or — if the attempt budget
   is exhausted — marks ``FAILED``.

The worker holds no state of its own. Crash mid-dispatch leaves an
item ``IN_FLIGHT``; on next process start :meth:`OutboundQueue.
reclaim_stale_in_flight` puts it back to ``PENDING``. Twilio dedupes
nothing for us — at-least-once delivery is the contract. The
``attempts`` column lets operators see double-send candidates.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import final

import structlog

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
    """Channel dispatch failed in a way that's worth retrying.

    Examples: HTTP 5xx from Twilio, network timeout, rate limit.
    The worker requeues the item with a backoff delay.
    """


class PermanentDispatchError(RuntimeError):
    """Channel dispatch failed in a way retries can't fix.

    Examples: invalid phone format, account suspended, body exceeds
    channel limits. The worker marks the item ``FAILED`` immediately.
    """


# ---------------------------------------------------------------------------
# Config + worker
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class OutboundWorkerConfig:
    """Tunables for :class:`OutboundWorker`. Frozen so they round-trip safely."""

    #: How long between ticks when the queue is empty or all gates are closed.
    poll_interval: timedelta = timedelta(milliseconds=250)

    #: Initial backoff after a transient dispatch error. Doubles per attempt.
    initial_backoff: timedelta = timedelta(seconds=2)

    #: Cap on the backoff window so it doesn't grow unbounded.
    max_backoff: timedelta = timedelta(minutes=5)

    #: A worker tick that started before ``now - reclaim_after`` and never
    #: completed is considered crashed. Items it claimed get returned to
    #: ``PENDING`` so a fresh tick can re-grab them.
    reclaim_after: timedelta = timedelta(minutes=1)


@final
class OutboundWorker:
    """Drains an :class:`OutboundQueue` into a channel dispatcher.

    The worker is single-shot per process: instantiate one, call
    :meth:`start` to spawn its background task, and :meth:`stop` to
    request clean shutdown. ``await``\\ ing :meth:`stop` blocks until
    the current tick finishes.
    """

    def __init__(
        self,
        *,
        queue: OutboundQueue,
        dispatcher: OutboundDispatcher,
        consent: SmsConsentPort,
        hours: BusinessHoursPort,
        clock: Clock,
        config: OutboundWorkerConfig | None = None,
    ) -> None:
        self._queue = queue
        self._dispatcher = dispatcher
        self._consent = consent
        self._hours = hours
        self._clock = clock
        self._config = config or OutboundWorkerConfig()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the worker's background task.

        Reclaims any stale ``IN_FLIGHT`` items from a previous process
        before the first tick runs, so crash-recovery is built into the
        startup path.
        """
        if self._task is not None and not self._task.done():
            raise RuntimeError("OutboundWorker.start called twice")
        cutoff = self._clock.now() - self._config.reclaim_after
        reclaimed = self._queue.reclaim_stale_in_flight(older_than=cutoff)
        if reclaimed:
            _log.info("outbound.worker.startup.reclaimed", count=reclaimed)
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="outbound-worker")

    async def stop(self) -> None:
        """Ask the worker to finish its current tick and exit."""
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - drain
                    pass
            self._task = None

    # ------------------------------------------------------------------
    # Tick — public for ergonomic tests (no need to spin a real task)
    # ------------------------------------------------------------------

    async def tick(self) -> OutboundItem | None:
        """Run exactly one drain attempt. Returns the item processed (if any).

        Test-facing entry point. Production callers should use
        :meth:`start` / :meth:`stop`; tests use ``tick`` to step the
        worker deterministically.
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
        poll_seconds = self._config.poll_interval.total_seconds()
        while not self._stop_event.is_set():
            try:
                processed = await self.tick()
            except Exception:  # noqa: BLE001 - worker must not die
                _log.exception("outbound.worker.tick_errored")
                processed = None
            if processed is None:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=poll_seconds
                    )
                except TimeoutError:
                    pass

    async def _process(self, item: OutboundItem) -> OutboundItem:
        """Dispatch one claimed item; return the final-state item."""
        # Consent gate. Permanent — blocked items stay blocked even if
        # the customer later opts back in (a new send would succeed,
        # but this queued one is dead).
        if not self._consent.sms_consent_for_phone(item.to_phone):
            blocked = self._queue.mark_blocked(
                item_id=item.item_id,
                reason=f"sms consent revoked for {item.to_phone}",
            )
            _log.info(
                "outbound.worker.blocked.consent",
                item_id=item.item_id,
                case_id=str(item.case_id),
                to=item.to_phone,
            )
            return blocked

        # Dispatch. Three outcome classes:
        #   - success → mark_sent
        #   - permanent error → mark_failed
        #   - transient error / unknown exception → retry-or-fail
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
        return sent

    def _handle_transient(
        self, item: OutboundItem, exc: BaseException
    ) -> OutboundItem:
        """Either retry with backoff or mark FAILED if budget exhausted."""
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
        return requeued

    def _backoff_for_attempt(self, attempts: int) -> timedelta:
        """Exponential backoff capped at ``max_backoff``.

        ``attempts`` is the post-claim count, so the first failure has
        ``attempts == 1``. Backoff doubles from there: 2s, 4s, 8s, ...
        """
        seconds = self._config.initial_backoff.total_seconds()
        # Double per failure beyond the first.
        for _ in range(max(0, attempts - 1)):
            seconds *= 2
        capped = min(seconds, self._config.max_backoff.total_seconds())
        return timedelta(seconds=capped)


__all__ = [
    "OutboundWorker",
    "OutboundWorkerConfig",
    "PermanentDispatchError",
    "TransientDispatchError",
]
