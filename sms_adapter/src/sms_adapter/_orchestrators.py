"""The three input handlers.

These are *thin*. Each one:
  1. Reads / appends history.
  2. Calls ``take_turn`` (the only place the LLM is invoked).
  3. Sends the reply via Twilio.
  4. Persists the reply.

If you find yourself adding conversation policy here, move it to
``_take_turn.py`` instead. These are I/O glue.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import final

from sms_adapter import (
    SmsContext,
    SmsDeps,
    Turn,
    TurnRole,
    take_turn,
)
from sms_adapter._event_log import log_event

_log = logging.getLogger(__name__)


@final
class InboundForUnknownPhoneError(LookupError):
    """An inbound SMS arrived from a phone with no active conversation.

    Either the conversation was never opened, or it was closed and the
    customer is texting after the case ended. The webhook surfaces this
    so the operator can decide what to do — usually log and ignore.
    """

    def __init__(self, phone: str) -> None:
        super().__init__(f"No active SMS conversation for phone {phone!r}")
        self.phone = phone


async def open_conversation(ctx: SmsContext, *, deps: SmsDeps) -> str:
    """Bind the phone, ask the LLM for the opening message, send it.

    Returns the opening message body (also persisted in history).
    """
    # Fail fast if this phone is already active — would otherwise route
    # the next inbound to the wrong case.
    existing = deps.routing.find_conversation_id(ctx.customer_phone)
    if existing is not None and existing != ctx.conversation_id:
        raise RuntimeError(
            f"Phone {ctx.customer_phone!r} already bound to conversation "
            f"{existing!r}; close it before opening a new one."
        )

    deps.routing.bind(phone=ctx.customer_phone, conversation_id=ctx.conversation_id)

    history = deps.history.load(ctx.conversation_id)  # usually empty here
    reply = take_turn(
        ctx=ctx,
        history=history,
        inbound=None,
        prompt_paths=deps.prompt_paths,
        llm_complete=deps.llm_complete,
    )

    sid = deps.twilio_send(to=ctx.customer_phone, body=reply)
    log_event(
        deps.event_log_path,
        f"us->sms to={ctx.customer_phone} conv={ctx.conversation_id} sid={sid}",
        reply,
    )
    deps.history.append(
        ctx.conversation_id,
        Turn(role=TurnRole.ASSISTANT, text=reply, timestamp=datetime.now(UTC), twilio_sid=sid),
    )
    _log.info(
        "sms.opened conversation=%s phone=%s sid=%s",
        ctx.conversation_id,
        ctx.customer_phone,
        sid,
    )
    return reply


async def handle_inbound(
    *,
    from_number: str,
    body: str,
    deps: SmsDeps,
    # Optional context passthrough for variables (the routing store only
    # holds conversation_id; the variables came from the Fire button and
    # are remembered out-of-band by whoever owns the live conversation).
    # In v1 the simulator stashes the SmsContext in a module-level dict
    # keyed by conversation_id; we look it up here.
    context_lookup: "ContextLookup | None" = None,
    # Defaulting to None for caller flexibility; actual wiring is below.
    message_sid: str = "",
    # Twilio gives us To and NumMedia too — accepted for forward-compat.
    to_number: str = "",
) -> str:
    """Route an inbound SMS to its conversation, take a turn, send the reply.

    Returns the assistant's reply body.

    Raises ``InboundForUnknownPhoneError`` if the phone has no active
    conversation.
    """
    conversation_id = deps.routing.find_conversation_id(from_number)
    log_event(
        deps.event_log_path,
        f"sms->us from={from_number} conv={conversation_id} sid={message_sid}",
        body,
    )
    if conversation_id is None:
        raise InboundForUnknownPhoneError(from_number)

    if context_lookup is None:
        raise RuntimeError(
            "handle_inbound requires a context_lookup to recover the "
            "SmsContext (variables) for the conversation. Wire one at startup."
        )
    ctx = context_lookup(conversation_id)
    if ctx is None:
        raise InboundForUnknownPhoneError(from_number)

    inbound_turn = Turn(
        role=TurnRole.USER,
        text=body,
        timestamp=datetime.now(UTC),
        twilio_sid=message_sid,
    )
    deps.history.append(conversation_id, inbound_turn)

    # 2. Load full history (now including the new inbound) and take a turn.
    history = deps.history.load(conversation_id)
    reply = take_turn(
        ctx=ctx,
        history=history,
        inbound=body,
        prompt_paths=deps.prompt_paths,
        llm_complete=deps.llm_complete,
    )

    sid = deps.twilio_send(to=ctx.customer_phone, body=reply)
    log_event(
        deps.event_log_path,
        f"us->sms to={ctx.customer_phone} conv={conversation_id} sid={sid}",
        reply,
    )
    deps.history.append(
        conversation_id,
        Turn(role=TurnRole.ASSISTANT, text=reply, timestamp=datetime.now(UTC), twilio_sid=sid),
    )
    _log.info(
        "sms.inbound conversation=%s phone=%s in_sid=%s out_sid=%s",
        conversation_id,
        from_number,
        message_sid,
        sid,
    )
    return reply


async def close_conversation(
    *,
    conversation_id: str,
    phone: str,
    reason: str,
    deps: SmsDeps,
) -> None:
    """Stop accepting inbound for this conversation. History is preserved."""
    deps.routing.unbind(phone=phone)
    _log.info(
        "sms.closed conversation=%s phone=%s reason=%s",
        conversation_id,
        phone,
        reason,
    )


# ---------------------------------------------------------------------------
# Context lookup callback
# ---------------------------------------------------------------------------


class ContextLookup:
    """Callable: ``conversation_id -> SmsContext | None``.

    Defined as a class so it shows up clearly in the public API. The
    simulator wires one of these at startup that closes over its
    in-memory dict of active SmsContexts.
    """

    def __call__(self, conversation_id: str) -> SmsContext | None: ...


__all__ = [
    "ContextLookup",
    "InboundForUnknownPhoneError",
    "close_conversation",
    "handle_inbound",
    "open_conversation",
]
