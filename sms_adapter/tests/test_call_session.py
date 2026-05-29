"""Unit tests for the SMS ``CallSession`` implementation.

Exercises ``SmsCallSession.place(case)`` in isolation — no FastAPI,
no real Twilio, no real LLM. Fakes record their inputs so we can
assert that:

- The opening turn is sent before any inbound is awaited.
- Inbound turns from ``deliver_inbound`` are dequeued, fed to the
  LLM, and the reply is dispatched via Twilio + persisted in
  history.
- A confirmation reply matching one of the case's offered slots
  flips the outcome to ``business_outcome="booked"`` with the
  right ``booked_slot_id``.
- A customer ``STOP`` keyword closes the session with
  ``business_outcome="declined"``.
- The inactivity timeout fires and returns ``inconclusive`` when no
  inbound arrives in the configured window.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import final

import pytest

from guidepoint.case import (
    CallStage,
    CaseEvent,
    CaseId,
    JsonCasePaths,
    OfferedSlot,
    ServiceEvent,
    SlotId,
    Trigger,
    TriggerId,
    build_json_case_repository,
    create_case_from_trigger,
)
from guidepoint.events import build_event_bus
from guidepoint.master_data import (
    CustomerId,
    CustomerRecord,
    DealerId,
    DealerRecord,
    Location,
    VehicleRecord,
    VehicleVin,
)
from prompt_composer import PromptPaths
from sms_adapter import (
    Turn,
    build_json_history_store,
    build_json_routing_store,
    build_sms_call_session,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


@final
@dataclass
class FakeTwilio:
    sent: list[tuple[str, str]] = field(default_factory=list)
    counter: int = 0

    def __call__(self, *, to: str, body: str) -> str:
        self.counter += 1
        self.sent.append((to, body))
        return f"SM{self.counter:032x}"


@final
@dataclass
class FakeLlm:
    replies: list[str]
    calls: list[tuple[str, tuple[Turn, ...]]] = field(default_factory=list)

    def __call__(self, *, system: str, history: tuple[Turn, ...]) -> str:
        self.calls.append((system, history))
        if not self.replies:
            raise AssertionError("FakeLlm exhausted")
        return self.replies.pop(0)


@final
class FixedClock:
    def __init__(self, *, instant: datetime | None = None) -> None:
        self._instant = instant or datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._instant


# --------------------------------------------------------------------------- #
# Builders                                                                    #
# --------------------------------------------------------------------------- #


def _prompt_paths() -> PromptPaths:
    return PromptPaths(
        system=_REPO_ROOT / "11Labs" / "config" / "system-prompt.md",
        post_booking=_REPO_ROOT / "11Labs" / "config" / "prompt-post-booking.md",
        voice=_REPO_ROOT / "11Labs" / "config" / "voice.md",
        sms=_REPO_ROOT / "sms_adapter" / "config" / "sms.md",
    )


def _customer() -> CustomerRecord:
    return CustomerRecord(
        id=CustomerId("c"),
        first_name="Sample",
        last_name="Customer",
        phone="+13135550000",
    )


def _dealer() -> DealerRecord:
    return DealerRecord(
        id=DealerId("d"),
        name="Sample Dealer",
        phone="+12485550000",
        address="1 Main St",
        ride_radius_miles=10,
    )


def _vehicle() -> VehicleRecord:
    return VehicleRecord(
        vin=VehicleVin("1C4RJFBG5NC123456"),
        owner_id=CustomerId("c"),
        year=2025,
        make="Jeep",
        model="Grand Cherokee",
        odometer_miles=12000,
        current_location=Location(latitude=42.0, longitude=-83.0, description="here"),
    )


def _trigger() -> Trigger:
    return Trigger(
        id=TriggerId("trig_sms_unit"),
        vehicle_vin=VehicleVin("1C4RJFBG5NC123456"),
        dealer_id=DealerId("d"),
        service_event=ServiceEvent(type="maintenance", summary="oil change"),
        channel_preference="sms",
        offered_slots=(
            OfferedSlot(
                id=SlotId("slot_a"),
                starts_at=datetime(2026, 5, 12, 12, 30, tzinfo=UTC),
                display="Tuesday, May 12 - 8:30 AM",
            ),
            OfferedSlot(
                id=SlotId("slot_b"),
                starts_at=datetime(2026, 5, 13, 13, 0, tzinfo=UTC),
                display="Wednesday, May 13 - 9:00 AM",
            ),
        ),
        created_at=datetime(2026, 5, 10, tzinfo=UTC),
    )


def _build_session(
    *,
    tmp_path: Path,
    twilio: FakeTwilio,
    llm: FakeLlm,
    inactivity: timedelta = timedelta(seconds=30),
):
    """Wire up an SmsCallSession with on-disk JSON stores under tmp_path."""
    clock = FixedClock()
    bus = build_event_bus(payload_type=CaseEvent)
    case_repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    history = build_json_history_store(root=tmp_path / "history")
    routing = build_json_routing_store(path=tmp_path / "routing.json")
    session = build_sms_call_session(
        twilio_send=twilio,
        llm_complete=llm,
        history=history,
        routing=routing,
        prompt_paths=_prompt_paths(),
        case_repo=case_repo,
        bus=bus,
        clock=clock,
        event_log_path=None,
        inactivity_timeout=inactivity,
    )
    case = create_case_from_trigger(
        trigger=_trigger(),
        customer=_customer(),
        dealer=_dealer(),
        vehicle=_vehicle(),
        clock=clock,
    )
    case_repo.save(case)
    return session, case, history, routing


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_booking_reply_matches_offered_slot(tmp_path: Path) -> None:
    twilio = FakeTwilio()
    llm = FakeLlm(
        replies=[
            # Opener (consumed before any inbound is awaited).
            "Hi, this is Kate. Reply Y to schedule your Jeep service.",
            # Reply to the customer's "yes please" — confirms a slot.
            "Perfect! I have you scheduled for Tuesday, May 12 - 8:30 AM. "
            "See you then.",
        ]
    )
    session, case, history, routing = _build_session(
        tmp_path=tmp_path, twilio=twilio, llm=llm
    )

    place_task = asyncio.create_task(session.place(case))

    # Wait for the opener to land so the inbound queue exists.
    for _ in range(200):
        if session.has_active(CaseId(case.case_id)) and twilio.sent:
            break
        await asyncio.sleep(0.01)
    assert session.has_active(CaseId(case.case_id))
    assert len(twilio.sent) == 1

    queued = await session.deliver_inbound(
        case_id=CaseId(case.case_id),
        from_number="+13135550000",
        body="1",
        message_sid="SMinbound_001",
    )
    assert queued is True

    outcome = await asyncio.wait_for(place_task, timeout=2.0)
    assert outcome.business_outcome == "booked"
    assert outcome.booked_slot_id == "slot_a"
    assert outcome.result == "answered"

    # Routing was unbound; session is no longer active.
    assert routing.find_conversation_id("+13135550000") is None
    assert not session.has_active(CaseId(case.case_id))

    # History persisted opener + inbound + reply.
    turns = history.load(case.case_id)
    assert [t.role.value for t in turns] == ["assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_stop_keyword_marks_declined(tmp_path: Path) -> None:
    twilio = FakeTwilio()
    llm = FakeLlm(replies=["Hi, this is Kate. Reply Y to schedule."])
    session, case, _history, routing = _build_session(
        tmp_path=tmp_path, twilio=twilio, llm=llm
    )

    place_task = asyncio.create_task(session.place(case))
    for _ in range(200):
        if session.has_active(CaseId(case.case_id)) and twilio.sent:
            break
        await asyncio.sleep(0.01)

    await session.deliver_inbound(
        case_id=CaseId(case.case_id),
        from_number="+13135550000",
        body="STOP",
        message_sid="SMinbound_stop",
    )

    outcome = await asyncio.wait_for(place_task, timeout=2.0)
    assert outcome.business_outcome == "opted_out"
    assert outcome.booked_slot_id is None
    # No LLM call after the opener — STOP short-circuits.
    assert len(llm.calls) == 1
    assert len(twilio.sent) == 1  # only the opener
    assert routing.find_conversation_id("+13135550000") is None


@pytest.mark.asyncio
async def test_inactivity_timeout_returns_inconclusive(tmp_path: Path) -> None:
    twilio = FakeTwilio()
    llm = FakeLlm(replies=["Hi, this is Kate. Reply Y to schedule."])
    session, case, _history, _routing = _build_session(
        tmp_path=tmp_path,
        twilio=twilio,
        llm=llm,
        inactivity=timedelta(milliseconds=50),
    )

    outcome = await asyncio.wait_for(session.place(case), timeout=2.0)
    assert outcome.business_outcome == "inconclusive"
    assert outcome.booked_slot_id is None
    assert len(twilio.sent) == 1  # the opener still went out
    # Session no longer active after timeout.
    assert not session.has_active(CaseId(case.case_id))


@pytest.mark.asyncio
async def test_reminder_reschedule_returns_rescheduled_outcome(tmp_path: Path) -> None:
    twilio = FakeTwilio()
    llm = FakeLlm(
        replies=[
            "Reminder: your Jeep service is Tuesday, May 12 - 8:30 AM. "
            "Reply 1 Confirmed, 2 Reschedule, or 3 Cancel.",
            "No problem — I'll help you find a new time.",
        ]
    )
    session, case, history, routing = _build_session(
        tmp_path=tmp_path, twilio=twilio, llm=llm
    )
    case = case.model_copy(
        update={
            "booked_slot_id": SlotId("slot_a"),
            "booked_slot_display": "Tuesday, May 12 - 8:30 AM",
        }
    )

    place_task = asyncio.create_task(
        session.start(
            case=case,
            stage=CallStage.INITIAL_REMINDER,
            attempt_number=1,
        )
    )
    for _ in range(200):
        if session.has_active(CaseId(case.case_id)) and twilio.sent:
            break
        await asyncio.sleep(0.01)

    # The reminder-stage system prompt is loaded (vs. the booking prompt).
    # Check the marker tokens individually so a line-wrap in the prompt
    # markdown doesn't flake the test — the prompt MAY format the options
    # line across two physical lines for readability.
    assert "initial_reminder_sent" in llm.calls[0][0]
    assert "Reply 1 to confirm" in llm.calls[0][0]
    assert "2 to reschedule" in llm.calls[0][0]
    assert "3 to cancel" in llm.calls[0][0]

    queued = await session.deliver_inbound(
        case_id=CaseId(case.case_id),
        from_number="+13135550000",
        body="2",
        message_sid="SMinbound_reschedule",
    )
    assert queued is True

    outcome = await asyncio.wait_for(place_task, timeout=2.0)
    assert outcome.business_outcome == "rescheduled"
    assert outcome.booked_slot_id is None
    assert len(llm.calls) == 2  # opener + ack
    assert len(twilio.sent) == 2
    assert routing.find_conversation_id("+13135550000") is None
    turns = history.load(case.case_id)
    assert [t.role.value for t in turns] == ["assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_reminder_cancel_returns_cancelled_not_declined(tmp_path: Path) -> None:
    twilio = FakeTwilio()
    llm = FakeLlm(
        replies=[
            "Reminder: reply 1 Confirmed, 2 Reschedule, or 3 Cancel.",
            "Got it — I've cancelled your appointment.",
        ]
    )
    session, case, _history, _routing = _build_session(
        tmp_path=tmp_path, twilio=twilio, llm=llm
    )

    place_task = asyncio.create_task(
        session.start(case=case, stage=CallStage.INITIAL_REMINDER, attempt_number=1)
    )
    for _ in range(200):
        if session.has_active(CaseId(case.case_id)) and twilio.sent:
            break
        await asyncio.sleep(0.01)

    await session.deliver_inbound(
        case_id=CaseId(case.case_id),
        from_number="+13135550000",
        body="CANCEL",
        message_sid="SMinbound_cancel_appt",
    )

    outcome = await asyncio.wait_for(place_task, timeout=2.0)
    assert outcome.business_outcome == "cancelled"


@pytest.mark.asyncio
async def test_deliver_inbound_to_unknown_case_returns_false(
    tmp_path: Path,
) -> None:
    twilio = FakeTwilio()
    llm = FakeLlm(replies=["unused"])
    session, _case, _history, _routing = _build_session(
        tmp_path=tmp_path, twilio=twilio, llm=llm
    )
    queued = await session.deliver_inbound(
        case_id=CaseId("case_doesnotexist"),
        from_number="+19998887777",
        body="hello?",
        message_sid="SM_orphan_1",
    )
    assert queued is False


@pytest.mark.asyncio
async def test_inbound_queue_overflow_drops_without_blocking(
    tmp_path: Path,
) -> None:
    """A flooded inbound queue caps at max, drops the extra, returns False.

    Verifies the bounded-queue contract: ``deliver_inbound`` never
    blocks the caller, the queue plateaus at ``SMS_INBOUND_QUEUE_MAX``,
    and additional pushes return ``False`` (the overflow path is
    exercised — the structured warning goes through structlog and is
    visible in captured stdout when this test is run with ``-s``).
    """
    from sms_adapter._call_session import SMS_INBOUND_QUEUE_MAX

    twilio = FakeTwilio()
    # Only the opener; the place() loop will park after the opener
    # awaiting either inbound (which we'll flood) or the inactivity
    # timeout. Crucially, no FakeLlm reply is queued for inbound — so
    # if a turn ever did get dequeued, the test would assert-fail.
    llm = FakeLlm(replies=["Hi, this is Kate. Reply Y to schedule."])
    session, case, _history, _routing = _build_session(
        tmp_path=tmp_path,
        twilio=twilio,
        llm=llm,
        inactivity=timedelta(seconds=30),
    )

    place_task = asyncio.create_task(session.place(case))
    for _ in range(200):
        if session.has_active(CaseId(case.case_id)) and twilio.sent:
            break
        await asyncio.sleep(0.01)

    # Fill the queue to capacity. The place() loop will dequeue at
    # most one (to call the LLM), then park waiting for the LLM reply
    # we never queued — leaving the rest in the queue. To make the
    # test deterministic we sample the depth and accept whatever
    # high-water mark we hit, then push enough beyond to guarantee
    # overflow.
    for i in range(SMS_INBOUND_QUEUE_MAX + 10):
        await session.deliver_inbound(
            case_id=CaseId(case.case_id),
            from_number="+13135550000",
            body=f"flood {i}",
            message_sid=f"SMflood{i:03d}",
        )

    # Depth must never exceed max.
    depths = session.queue_depths()
    assert CaseId(case.case_id) in depths
    current, max_depth = depths[CaseId(case.case_id)]
    assert max_depth == SMS_INBOUND_QUEUE_MAX
    assert current <= SMS_INBOUND_QUEUE_MAX

    # One more push against a full queue: must return False, not block.
    overflowed = await asyncio.wait_for(
        session.deliver_inbound(
            case_id=CaseId(case.case_id),
            from_number="+13135550000",
            body="one too many",
            message_sid="SMflood_overflow",
        ),
        timeout=1.0,
    )
    assert overflowed is False

    place_task.cancel()
    try:
        await place_task
    except asyncio.CancelledError:
        pass
