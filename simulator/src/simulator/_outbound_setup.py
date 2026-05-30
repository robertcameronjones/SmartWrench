"""Compose the outbound queue + worker + queued-twilio sender.

One job: stand up the persistent outbound dispatch pipeline and return
the three handles the rest of the simulator needs:

- ``queue``: the :class:`OutboundQueue` (for ops/health endpoints).
- ``worker``: the :class:`OutboundWorker`; the app lifespan starts it
  at boot and stops it at shutdown.
- ``sender``: a :class:`TwilioSend` that enqueues into ``queue`` and
  blocks until the worker drains the item. Passed into
  :func:`build_sms_session` in place of the live Twilio sender, so the
  call session sees the same interface but every send goes through the
  queue.

Wiring rules
============
The queue is **always** SQLite — it's the persistence the queue was
designed around, and the file is independent of the case repo backend
(JSON repo + SQLite queue is supported). The DB file lives next to the
case DB at ``<project_root>/data/outbound_queue.db``.

Returns ``None`` when SMS env vars are missing (consistent with
:func:`build_sms_session`); the rest of the app boots fine and the
Fire route surfaces a 503 if the operator selects channel=sms.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import final

import structlog

from guidepoint.case._models import CaseId
from guidepoint.clock import Clock
from guidepoint.outbound import (
    BusinessHoursPort,
    OutboundDispatcher,
    OutboundWorker,
    OutboundWorkerConfig,
    SmsConsentPort,
)
from guidepoint.persistence import OutboundQueue, build_sqlite_outbound_queue
from sms import send_sms

from sms_adapter import TwilioSend, build_queued_twilio_sender

_log = structlog.get_logger(__name__)


@final
@dataclass(frozen=True, slots=True)
class OutboundBundle:
    """The three handles the app needs from the outbound subsystem."""

    queue: OutboundQueue
    worker: OutboundWorker
    sender: TwilioSend


def build_outbound_dispatch(
    *,
    project_root: Path,
    clock: Clock,
    consent: SmsConsentPort,
    hours: BusinessHoursPort,
    dispatcher: OutboundDispatcher | None = None,
    db_path: Path | None = None,
    worker_config: OutboundWorkerConfig | None = None,
) -> OutboundBundle | None:
    """Build the queue + worker + queued sender, or ``None`` if SMS is unwired.

    ``dispatcher`` defaults to a live Twilio dispatcher built from the
    standard ``TWILIO_*`` env vars. Pass an injected one for tests.

    ``db_path`` defaults to ``<project_root>/data/outbound_queue.db``;
    override for tests so they get their own scratch file.
    """
    if dispatcher is None:
        built = _build_live_dispatcher_from_env()
        if built is None:
            return None
        dispatcher = built

    resolved_db_path = db_path or (project_root / "data" / "outbound_queue.db")
    resolved_db_path.parent.mkdir(parents=True, exist_ok=True)

    queue = build_sqlite_outbound_queue(db_path=resolved_db_path)
    worker = OutboundWorker(
        queue=queue,
        dispatcher=dispatcher,
        consent=consent,
        hours=hours,
        clock=clock,
        config=worker_config,
    )
    sender = build_queued_twilio_sender(queue=queue, clock=clock)

    _log.info(
        "simulator.outbound.configured",
        db_path=str(resolved_db_path),
    )
    return OutboundBundle(queue=queue, worker=worker, sender=sender)


def _build_live_dispatcher_from_env() -> OutboundDispatcher | None:
    """Construct the real Twilio sender, or ``None`` if env vars are missing.

    Mirrors the env-var contract used by ``_sms_setup.build_sms_session``
    so the two stay in lockstep — if one bails because Twilio creds are
    absent, so does the other.
    """
    account_sid = (os.environ.get("TWILIO_ACCOUNT_SID") or "").strip()
    auth_token = (os.environ.get("TWILIO_AUTH_TOKEN") or "").strip()
    from_number = (os.environ.get("TWILIO_FROM_NUMBER") or "").strip()
    missing = [
        name
        for name, value in (
            ("TWILIO_ACCOUNT_SID", account_sid),
            ("TWILIO_AUTH_TOKEN", auth_token),
            ("TWILIO_FROM_NUMBER", from_number),
        )
        if not value
    ]
    if missing:
        _log.warning("simulator.outbound.disabled", missing_env=missing)
        return None

    def _dispatch(*, to: str, body: str) -> str:
        # The OutboundDispatcher protocol does not pass case_id (the
        # worker has already used it to claim the queue row); Twilio
        # itself doesn't need it.
        return send_sms(
            to=to,
            body=body,
            account_sid=account_sid,
            auth_token=auth_token,
            from_number=from_number,
        )

    return _dispatch


# CaseId import is exposed for callers that need to type-annotate the
# bundle in adjacent modules without re-importing.
__all__ = [
    "CaseId",
    "OutboundBundle",
    "build_outbound_dispatch",
]
