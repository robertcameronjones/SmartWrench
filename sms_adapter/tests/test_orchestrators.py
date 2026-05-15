"""Orchestrators: open / inbound / close. End-to-end with fakes."""

from __future__ import annotations

import pytest

from sms_adapter import (
    InboundForUnknownPhoneError,
    SmsContext,
    TurnRole,
    close_conversation,
    handle_inbound,
    open_conversation,
)


@pytest.mark.asyncio
async def test_open_conversation_binds_phone_and_sends_opening_message(
    make_deps, fake_twilio, context: SmsContext
) -> None:
    deps, llm = make_deps(["Hi Sarah! It's Kate, time for service?"])

    reply = await open_conversation(context, deps=deps)

    assert reply == "Hi Sarah! It's Kate, time for service?"
    # Twilio was asked to send to the customer
    assert fake_twilio.sent == [("+13135551212", "Hi Sarah! It's Kate, time for service?")]
    # Routing was bound
    assert deps.routing.find_conversation_id("+13135551212") == "conv_test_001"
    # History records the assistant's opening turn (no user turn — bootstrap is not persisted)
    history = deps.history.load("conv_test_001")
    assert len(history) == 1
    assert history[0].role is TurnRole.ASSISTANT
    assert history[0].twilio_sid.startswith("SM_fake_")


@pytest.mark.asyncio
async def test_open_conversation_rejects_phone_already_bound_to_other_conversation(
    make_deps, context: SmsContext
) -> None:
    deps, _ = make_deps(["opening"])
    # Pre-bind the phone to a different conversation
    deps.routing.bind(phone="+13135551212", conversation_id="conv_OTHER")
    with pytest.raises(RuntimeError, match="already bound"):
        await open_conversation(context, deps=deps)


@pytest.mark.asyncio
async def test_handle_inbound_routes_to_conversation_appends_calls_llm_sends_reply(
    make_deps, fake_twilio, context: SmsContext
) -> None:
    deps, llm = make_deps([
        "Hi Sarah! It's Kate, time for service?",  # opening
        "Great. How's Tuesday at 8:30am?",        # response to "yes"
    ])
    # Open conversation first.
    await open_conversation(context, deps=deps)
    fake_twilio.sent.clear()  # focus on inbound side

    # Customer texts back.
    def lookup(conversation_id: str) -> SmsContext | None:
        return context if conversation_id == "conv_test_001" else None

    reply = await handle_inbound(
        from_number="+13135551212",
        body="yes please",
        deps=deps,
        context_lookup=lookup,
        message_sid="SM_inbound_001",
    )

    assert reply == "Great. How's Tuesday at 8:30am?"
    assert fake_twilio.sent == [("+13135551212", "Great. How's Tuesday at 8:30am?")]

    history = deps.history.load("conv_test_001")
    # opening assistant + user inbound + assistant reply = 3 turns
    assert [t.role for t in history] == [TurnRole.ASSISTANT, TurnRole.USER, TurnRole.ASSISTANT]
    assert history[1].text == "yes please"
    assert history[1].twilio_sid == "SM_inbound_001"


@pytest.mark.asyncio
async def test_handle_inbound_for_unknown_phone_raises(make_deps) -> None:
    deps, _ = make_deps([])
    with pytest.raises(InboundForUnknownPhoneError) as exc:
        await handle_inbound(
            from_number="+19998887777",
            body="hello?",
            deps=deps,
            context_lookup=lambda _cid: None,
        )
    assert exc.value.phone == "+19998887777"


@pytest.mark.asyncio
async def test_close_conversation_unbinds_phone_but_keeps_history(
    make_deps, context: SmsContext
) -> None:
    deps, _ = make_deps(["opening"])
    await open_conversation(context, deps=deps)
    assert deps.routing.find_conversation_id("+13135551212") == "conv_test_001"

    await close_conversation(
        conversation_id="conv_test_001",
        phone="+13135551212",
        reason="booked",
        deps=deps,
    )

    assert deps.routing.find_conversation_id("+13135551212") is None
    # History persists
    assert len(deps.history.load("conv_test_001")) == 1
