"""Live :class:`guidepoint.case.SmsDispatcher` implementation.

Glues together the three pieces an SMS turn needs:

1. :class:`SmsMessageComposer` — LLM call + prompt + history load.
2. :class:`TwilioSend` (the queued sender) — enqueues the message
   for the outbound worker to dispatch.
3. The SMS history + audit log — appends one assistant turn after the
   send is accepted so the next composer call sees it.

The dispatcher is intentionally thin: it neither classifies inbound
text nor decides state transitions (those live in the reducer) and
it does not run any per-case loop (the queue worker owns delivery
timing). One instance is shared by the simulator's :class:`CaseDriver`
across every active SMS case.
"""

from __future__ import annotations

import asyncio
from typing import final

import structlog

from guidepoint.case import CallStage, CaseId
from guidepoint.case._ports import SmsDispatcher

from sms_adapter import SmsMessageComposer, TwilioSend

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
    ) -> None:
        self._composer = composer
        self._sender = sender

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

        1. Compose first. If the LLM fails we never enqueue, never
           charge the customer for a half-sent message.
        2. Enqueue. If the sender raises we have nothing to record
           and we surface the exception to the driver, which logs
           ``sms_dispatcher.dispatch_outbound.failed``.
        3. Record outbound only after the queue accepts the item. The
           assistant turn now sits in history with the queued
           ``item_id`` as its handle; the real Twilio MessageSid will
           arrive later via the :class:`OutboundDispatched` signal.
        4. Emit the ``sms.outbound`` audit event last so the timeline
           reflects what actually shipped.
        """

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
) -> LiveSmsDispatcher:
    """Construct the process-wide :class:`LiveSmsDispatcher`."""

    return LiveSmsDispatcher(composer=composer, sender=sender)


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
