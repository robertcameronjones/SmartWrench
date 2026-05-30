"""Queue-backed :class:`TwilioSend` — bridge from ``SmsCallSession`` to the queue.

The :class:`SmsCallSession` was written assuming a synchronous Twilio
sender that returns a MessageSid. The outbound queue is asynchronous —
items land in a SQLite table and a worker drains them. This module
glues the two together:

1. ``_send`` enqueues the outbound item with the case id.
2. It then polls the queue until the item reaches a terminal state.
3. On ``SENT``, returns the Twilio MessageSid.
4. On ``BLOCKED`` (consent revoked), raises :class:`SmsConsentError` so
   the session can react like before.
5. On ``FAILED`` (transient errors exhausted), raises ``RuntimeError``
   surfacing the last error.
6. On timeout (worker still hasn't reached the item — typically because
   the business-hours gate is closed), raises :class:`QueueWaitTimeout`.
   The session treats this like a send failure and bails out; the
   queue item lives on and the worker will send it whenever the gate
   reopens. Operators get the message later than the session expected,
   but they still get it.

Timeout policy
==============

``max_wait`` defaults to 30 seconds, which covers:

- Worker tick interval (250ms) + Twilio round-trip (~1s) during open
  hours, with margin.
- A short business-hours close + reopen blip.

It does **not** cover an overnight close. That's deliberate: keeping
the session task parked overnight is worse than letting it bail —
the customer reply that eventually arrives will spawn a fresh session
on demand.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import final

from guidepoint.case._models import CaseId
from guidepoint.clock import Clock
from guidepoint.persistence import OutboundQueue, OutboundState

from sms_adapter import TwilioSend
from sms_adapter._gated_twilio import SmsConsentError


@final
class QueueWaitTimeout(RuntimeError):
    """Raised when the worker hasn't reached a queued item within ``max_wait``.

    The item is still in the queue and will be sent when the worker
    next picks it up (typically when business hours reopen). The
    caller (``SmsCallSession``) treats this as a send failure for its
    own bookkeeping, but the actual delivery is still pending.
    """


def build_queued_twilio_sender(
    *,
    queue: OutboundQueue,
    clock: Clock,
    poll_interval: timedelta = timedelta(milliseconds=100),
    max_wait: timedelta = timedelta(seconds=30),
    max_attempts: int = 3,
) -> TwilioSend:
    """Return a :class:`TwilioSend` that routes through the outbound queue.

    The returned callable is synchronous (matches ``TwilioSend``) and
    blocks until the queued item reaches a terminal state or
    ``max_wait`` elapses. Callers wrap it in ``asyncio.to_thread`` to
    avoid blocking the event loop.

    See module docstring for timeout semantics.
    """

    poll_seconds = poll_interval.total_seconds()
    if poll_seconds <= 0:
        raise ValueError("poll_interval must be positive")
    if max_wait.total_seconds() <= 0:
        raise ValueError("max_wait must be positive")

    def _send(*, case_id: CaseId, to: str, body: str) -> str:
        item = queue.enqueue(
            case_id=case_id,
            to_phone=to,
            body=body,
            enqueued_at=clock.now(),
            max_attempts=max_attempts,
        )
        deadline = clock.now() + max_wait
        while True:
            current = queue.get(item.item_id)
            if current is None:
                raise RuntimeError(
                    f"outbound queue lost item {item.item_id!r} "
                    f"for case {case_id!r}"
                )
            if current.state == OutboundState.SENT:
                return current.twilio_sid
            if current.state == OutboundState.BLOCKED:
                raise SmsConsentError(
                    f"SMS blocked: {current.last_error or 'consent refused'}"
                )
            if current.state == OutboundState.FAILED:
                raise RuntimeError(
                    f"SMS dispatch failed for {to!r}: {current.last_error}"
                )
            if clock.now() >= deadline:
                raise QueueWaitTimeout(
                    f"SMS to {to!r} still queued after {max_wait}; "
                    f"queue item {item.item_id!r} state={current.state.value}"
                )
            time.sleep(poll_seconds)

    return _send


__all__ = [
    "QueueWaitTimeout",
    "build_queued_twilio_sender",
]
