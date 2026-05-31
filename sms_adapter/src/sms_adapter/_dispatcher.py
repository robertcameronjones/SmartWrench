"""Live :class:`guidepoint.case.SmsDispatcher` implementation.

Glues together the four pieces an SMS turn needs:

1. :class:`SmsMessageComposer` — LLM call + prompt + history load.
2. :class:`TwilioSend` (the queued sender) — enqueues the message
   for the outbound worker to dispatch.
3. The SMS history + audit log — appends one assistant turn after the
   send is accepted so the next composer call sees it.
4. The :class:`RoutingStore` — binds the customer's phone to the
   active case id so inbound texts find their way back through the
   simulator webhook to :meth:`CaseDriver.on_inbound_sms`.

The dispatcher is intentionally thin: it neither classifies inbound
text nor decides state transitions (those live in the reducer) and
it does not run any per-case loop (the queue worker owns delivery
timing). One instance is shared by the simulator's :class:`CaseDriver`
across every active SMS case.

Routing lifecycle
-----------------
The binding ``phone -> case_id`` happens inside
:meth:`dispatch_outbound`, before the message is enqueued. That
ordering matters: if the binding happened after the send, a fast
customer reply could race in before the routing table knew the case
existed and we would log it as ``unknown_phone`` (the bug that broke
the SMS happy path in the first refactor pass).

A :meth:`release_routing` hook is exposed for the driver to call
when a case reaches a terminal state, so stale entries don't pile
up. The reducer's terminal check still drops late inbound for
already-closed cases on its own, but unbinding keeps the routing
table small and the audit logs honest.
"""

from __future__ import annotations

import asyncio
from typing import final

import structlog

from guidepoint.case import CallStage, CaseId
from guidepoint.case._models import Channel
from guidepoint.case._ports import SmsDispatcher
from guidepoint.case._repository import CaseRepository

from sms_adapter import RoutingStore, SmsMessageComposer, TwilioSend

_log = structlog.get_logger(__name__)


@final
class LiveSmsDispatcher(SmsDispatcher):
    """Process-wide :class:`SmsDispatcher` for the live SMS channel.

    Construct via :func:`build_sms_dispatcher` — the constructor exists
    only to make typing trivial in tests that pass fakes for the
    composer or sender.
    """

    def __init__(
        self,
        *,
        composer: SmsMessageComposer,
        sender: TwilioSend,
        routing: RoutingStore,
        case_repo: CaseRepository,
        channel: Channel = "sms",
    ) -> None:
        self._composer = composer
        self._sender = sender
        self._routing = routing
        self._case_repo = case_repo
        self._channel = channel

    async def dispatch_outbound(
        self,
        *,
        case_id: CaseId,
        to_phone: str,
        stage: CallStage,
    ) -> str:
        """Compose-and-send one assistant reply, return queue item id.

        The composer's LLM call is synchronous (LiteLLM under the
        hood); the queued sender's enqueue is synchronous SQLite. Both
        are wrapped in :func:`asyncio.to_thread` so the event loop
        stays free during the round-trip.

        Order of operations is deliberate:

        1. Bind the routing entry first. Without this the simulator
           webhook cannot translate the customer's phone back to the
           case id and inbound replies are silently dropped as
           ``unknown_phone``. Idempotent — re-binding the same phone
           to the same case is a no-op on the JSON store.
        2. Compose. If the LLM fails we never enqueue, never charge
           the customer for a half-sent message; the routing
           bind-ahead is harmless because the customer will never
           have a thread to reply into.
        3. Enqueue. If the sender raises we have nothing to record
           and we surface the exception to the driver, which logs
           ``sms_dispatcher.dispatch_outbound.failed``.
        4. Record outbound only after the queue accepts the item. The
           assistant turn now sits in history with the queued
           ``item_id`` as its handle; the real Twilio MessageSid will
           arrive later via the :class:`OutboundDispatched` signal.
        5. Emit the ``sms.outbound`` audit event last so the timeline
           reflects what actually shipped.
        """

        self._bind_routing(case_id=case_id, to_phone=to_phone)
        composed = await asyncio.to_thread(
            self._composer.compose, case_id=case_id, stage=stage
        )
        body = composed.body
        item_id = await asyncio.to_thread(
            self._sender, case_id=case_id, to=to_phone, body=body
        )
        self._composer.record_outbound(
            case_id=case_id,
            body=body,
            twilio_sid=item_id,
            to_phone=to_phone,
        )
        await self._composer.emit_event(
            case_id=case_id,
            event="sms.outbound",
            detail=(
                f"stage={stage.value} to={to_phone} item={item_id} "
                f"body={_clip(body)!r}"
            ),
        )
        return item_id

    def _bind_routing(self, *, case_id: CaseId, to_phone: str) -> None:
        """Bind ``to_phone -> case_id`` in the routing store.

        Reads the case for ``user_id`` so per-user case repos can
        later route inbound on it. Idempotent and synchronous.
        Errors are logged but do not abort the outbound path — a
        missing routing binding manifests as ``unknown_phone`` on
        inbound, which the operator will see in the structured logs.
        """

        try:
            case = self._case_repo.get(case_id)
        except Exception:
            _log.warning(
                "live_sms_dispatcher.routing.bind.case_missing", case_id=case_id
            )
            self._routing.bind(
                phone=to_phone,
                conversation_id=str(case_id),
                user_id="",
                channel=self._channel,
            )
            return
        try:
            self._routing.bind(
                phone=to_phone,
                conversation_id=str(case_id),
                user_id=case.user_id,
                channel=case.initial_channel,
            )
        except Exception:
            _log.exception(
                "live_sms_dispatcher.routing.bind.failed",
                case_id=case_id,
                phone=to_phone,
            )

    def release_routing(self, *, to_phone: str) -> None:
        """Unbind a phone from any case it currently routes to.

        Called by the :class:`CaseDriver` when a case enters a
        terminal state so a future inbound on the same phone is
        treated as ``unknown_phone`` (rather than being silently
        dropped by the reducer's terminal guard). Synchronous,
        idempotent, swallows all errors.
        """

        try:
            self._routing.unbind(phone=to_phone)
        except Exception:
            _log.exception(
                "live_sms_dispatcher.routing.unbind.failed", phone=to_phone
            )

    async def record_inbound(
        self,
        *,
        case_id: CaseId,
        from_phone: str,
        body: str,
        message_sid: str,
    ) -> None:
        """Append the customer turn to history + audit log.

        Synchronous file IO under the hood; called from the driver's
        async webhook entry point. The driver always invokes this
        before publishing the :class:`InboundSmsReceived` signal, so
        the next LLM compose call sees the inbound text in history.
        """

        try:
            self._composer.record_inbound(
                case_id=case_id,
                from_phone=from_phone,
                body=body,
                message_sid=message_sid,
            )
        except Exception:
            _log.exception(
                "live_sms_dispatcher.record_inbound.history_failed",
                case_id=case_id,
                from_phone=from_phone,
                message_sid=message_sid,
            )
            return
        try:
            await self._composer.emit_event(
                case_id=case_id,
                event="sms.inbound",
                detail=(
                    f"from={from_phone} sid={message_sid} body={_clip(body)!r}"
                ),
            )
        except Exception:
            _log.exception(
                "live_sms_dispatcher.record_inbound.audit_failed",
                case_id=case_id,
                from_phone=from_phone,
                message_sid=message_sid,
            )


def build_sms_dispatcher(
    *,
    composer: SmsMessageComposer,
    sender: TwilioSend,
    routing: RoutingStore,
    case_repo: CaseRepository,
    channel: Channel = "sms",
) -> LiveSmsDispatcher:
    """Construct the process-wide :class:`LiveSmsDispatcher`."""

    return LiveSmsDispatcher(
        composer=composer,
        sender=sender,
        routing=routing,
        case_repo=case_repo,
        channel=channel,
    )


def _clip(text: str, *, limit: int = 160) -> str:
    """One-line audit-friendly summary of an SMS body."""

    clipped = text.strip().replace("\n", " ")
    if len(clipped) > limit:
        clipped = clipped[: limit - 1] + "\u2026"
    return clipped


__all__ = [
    "LiveSmsDispatcher",
    "build_sms_dispatcher",
]
