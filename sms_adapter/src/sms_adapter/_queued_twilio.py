"""Queue-backed :class:`TwilioSend` — fire-and-forget bridge to the outbound queue.

The :class:`SmsCallSession` was written assuming a synchronous Twilio
sender that returns a MessageSid. The outbound queue is asynchronous —
items land in a SQLite table and the :class:`OutboundWorker` drains
them. This module is the *fire-and-forget* glue between the two:

1. ``_send`` enqueues the outbound item with the case id.
2. It wakes the worker (one ``asyncio.Event`` set) so the item is
   considered immediately rather than waiting for the next tick.
3. It returns the queue ``item_id`` and is done. No polling, no wait.

That ``item_id`` flows into the session's ``Turn.twilio_sid`` slot as
the audit handle. When the worker eventually dispatches the message
to Twilio, it pushes an :class:`OutboundDispatched` signal carrying
the same ``item_id`` plus the real Twilio MessageSid into the case
driver's signal queue. The reducer joins the two via ``RecordEvent``;
the operator audit log shows both the "queued" moment (session
``Turn``) and the "sent" moment (case event) with a shared id.

Why fire-and-forget
===================

The earlier polling design parked the session task waiting for the
worker. That meant a single closed-hours window held the session
hostage for 30 seconds, then surfaced as a ``QueueWaitTimeout`` that
the session treated as a send failure, which then triggered the call
manager's retry logic — the "retry loop" you saw on Render. Fire-
and-forget cuts that whole feedback loop: enqueue, return, let the
queue do its job. Closed hours just means the item waits in SQLite
until the worker is woken by the hours-open transition.

If Twilio (or the worker) is genuinely broken, the operator notices
via:

- Queue depth on the ``/health/queues`` endpoint.
- ``outbound.worker.failed.*`` structlog warns.
- The session's own inactivity timeout, which eventually fires
  ``CallEnded(inconclusive)`` and lets the case move on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import final

from guidepoint.case._models import CaseId
from guidepoint.clock import Clock
from guidepoint.persistence import OutboundQueue

from sms_adapter import TwilioSend

#: Thread-safe wake hook the sender invokes after each enqueue. Built
#: by the simulator wiring layer from ``worker.notify_threadsafe`` so
#: the worker re-checks the queue immediately.
WorkerWake = Callable[[], None]


@final
class QueueEnqueueError(RuntimeError):
    """Raised when the outbound queue itself refuses an enqueue.

    Distinct from ``Twilio`` failures (those happen later inside the
    worker). This is the "we couldn't even park the message in SQLite"
    case — disk full, schema mismatch, etc. — and is surfaced to the
    session so the operator sees something is structurally wrong.
    """


def build_queued_twilio_sender(
    *,
    queue: OutboundQueue,
    clock: Clock,
    notify_worker: WorkerWake | None = None,
    max_attempts: int = 3,
) -> TwilioSend:
    """Return a :class:`TwilioSend` that enqueues and wakes the worker.

    The returned callable is synchronous (matches :class:`TwilioSend`)
    but it does no waiting — it enqueues the item, optionally wakes
    the worker, and returns the queue ``item_id`` immediately. Callers
    can still wrap it in ``asyncio.to_thread`` if they want to keep the
    event loop tidy, but the operation is so light (one SQLite INSERT
    plus an event set) that it's not strictly required.

    ``notify_worker`` should be ``worker.notify_threadsafe`` from the
    same simulator wiring layer. Optional only for tests that want to
    enqueue without spinning a worker — the queue itself works fine
    without a wake.
    """

    def _send(*, case_id: CaseId, to: str, body: str) -> str:
        try:
            item = queue.enqueue(
                case_id=case_id,
                to_phone=to,
                body=body,
                enqueued_at=clock.now(),
                max_attempts=max_attempts,
            )
        except Exception as exc:  # noqa: BLE001 - re-raise as our type
            raise QueueEnqueueError(
                f"failed to enqueue SMS for case {case_id!r}: {exc}"
            ) from exc
        if notify_worker is not None:
            try:
                notify_worker()
            except Exception:  # noqa: BLE001 - never let wake failure block enqueue
                # The item is safely in the queue; worst case the worker
                # picks it up on its next wake from another source.
                pass
        return item.item_id

    return _send


__all__ = [
    "QueueEnqueueError",
    "WorkerWake",
    "build_queued_twilio_sender",
]
