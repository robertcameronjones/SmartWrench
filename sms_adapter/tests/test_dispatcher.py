"""Tests for :class:`LiveSmsDispatcher`.

The dispatcher is the SMS-side of :class:`guidepoint.case.SmsDispatcher`.
The driver calls :meth:`dispatch_outbound` once per ``PlaceCall`` action
and :meth:`record_inbound` once per inbound webhook delivery.

Contracts these tests pin:

- ``dispatch_outbound`` composes via the LLM, sends via the queued
  sender, persists the assistant turn in history (with the queue's
  ``item_id`` as the audit handle), and emits a ``sms.outbound``
  audit event. Returns the queue item id.
- ``dispatch_outbound`` orders the operations so a failing send does
  not write an assistant turn (preventing a phantom message in the
  transcript).
- ``record_inbound`` appends the user turn to history and emits a
  ``sms.inbound`` audit event.
- Both methods raise / log per the documented contract — they never
  silently swallow LLM or sender failures (other than for the
  audit-event tail).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import final

import pytest

from guidepoint.case import (
    CallStage,
    Case,
    CaseEvent,
    CaseId,
    CaseState,
    OfferedSlot,
    ServiceEvent,
    SlotId,
    TriggerId,
)
from guidepoint.case._repository import build_json_case_repository, JsonCasePaths
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
    TurnRole,
    build_json_history_store,
    build_json_routing_store,
    build_sms_dispatcher,
    build_sms_message_composer,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


@final
@dataclass
class FakeLlm:
    replies: list[str]
    fail_on_next: bool = False
    calls: list[tuple[str, tuple[Turn, ...]]] = field(default_factory=list)

    def __call__(self, *, system: str, history: tuple[Turn, ...]) -> str:
        self.calls.append((system, history))
        if self.fail_on_next:
            raise RuntimeError("simulated LLM failure")
        if not self.replies:
            raise AssertionError("FakeLlm exhausted")
        return self.replies.pop(0)


@final
@dataclass
class FakeSender:
    """Records every send; returns a synthetic item id per call.

    Set ``fail_on_next`` to make the next call raise — the dispatcher
    must not persist the assistant turn when the sender fails.
    """

    fail_on_next: bool = False
    sent: list[tuple[str, str, str]] = field(default_factory=list)

    def __call__(self, *, case_id: CaseId, to: str, body: str) -> str:
        if self.fail_on_next:
            raise RuntimeError("simulated queue failure")
        self.sent.append((str(case_id), to, body))
        return f"ITEM_{len(self.sent):04d}"


@final
class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 5, 10, 12, 0, tzinfo=UTC)


def _prompt_paths() -> PromptPaths:
    return PromptPaths(
        system=_REPO_ROOT / "11Labs" / "config" / "system-prompt.md",
        post_booking=_REPO_ROOT / "11Labs" / "config" / "prompt-post-booking.md",
        voice=_REPO_ROOT / "11Labs" / "config" / "voice.md",
        sms=_REPO_ROOT / "sms_adapter" / "config" / "sms.md",
    )


def _seed_case(tmp_path: Path):
    case = Case(
        case_id=CaseId("case_disp"),
        trigger_id=TriggerId("trig_d"),
        correlation_id="corr_d",
        customer=CustomerRecord(
            id=CustomerId("c"),
            first_name="Robert",
            last_name="Jones",
            phone="+15555550001",
        ),
        dealer=DealerRecord(
            id=DealerId("d"),
            name="Village Jeep",
            phone="5559990000",
            address="1 Main St",
            ride_radius_miles=10,
        ),
        vehicle=VehicleRecord(
            vin=VehicleVin("1C4RJFBG5NC123456"),
            owner_id=CustomerId("c"),
            year=2025,
            make="Jeep",
            model="GC",
            odometer_miles=100,
            current_location=Location(
                latitude=42.0, longitude=-83.0, description="here"
            ),
        ),
        service_event=ServiceEvent(type="maintenance", summary="oil change"),
        offered_slots=(
            OfferedSlot(
                id=SlotId("slot_a"),
                starts_at=datetime(2026, 5, 12, 13, 30, tzinfo=UTC),
                display="Tuesday 8:30 AM",
            ),
        ),
        state=CaseState.CREATED,
        attempt_count=0,
        created_at=datetime(2026, 5, 10, tzinfo=UTC),
        initial_channel="sms",
    )
    paths = JsonCasePaths.for_root(tmp_path)
    repo = build_json_case_repository(paths=paths)
    repo.save(case)
    return case, repo


def _build_dispatcher(
    tmp_path: Path, *, llm: FakeLlm, sender: FakeSender
):
    case, case_repo = _seed_case(tmp_path)
    history = build_json_history_store(root=tmp_path / "history")
    routing = build_json_routing_store(path=tmp_path / "routing.json")
    bus = build_event_bus(payload_type=CaseEvent)
    clock = FixedClock()
    composer = build_sms_message_composer(
        llm_complete=llm,
        history=history,
        case_repo=case_repo,
        clock=clock,
        prompt_paths=_prompt_paths(),
        bus=bus,
    )
    dispatcher = build_sms_dispatcher(
        composer=composer,
        sender=sender,
        routing=routing,
        case_repo=case_repo,
    )
    return case, dispatcher, history, case_repo, routing


@pytest.mark.asyncio
async def test_dispatch_outbound_composes_sends_and_records(tmp_path: Path) -> None:
    llm = FakeLlm(replies=["Hi from Kate."])
    sender = FakeSender()
    case, dispatcher, history, _, _ = _build_dispatcher(tmp_path, llm=llm, sender=sender)

    item_id = await dispatcher.dispatch_outbound(
        case_id=case.case_id,
        to_phone=case.customer.phone,
        stage=CallStage.OUTREACH,
    )

    assert item_id == "ITEM_0001"
    assert sender.sent == [(str(case.case_id), case.customer.phone, "Hi from Kate.")]
    turns = history.load(case.case_id)
    assert len(turns) == 1
    assert turns[0].role is TurnRole.ASSISTANT
    assert turns[0].text == "Hi from Kate."
    # The audit handle on the assistant turn is the queue item_id;
    # the real Twilio MessageSid arrives later via OutboundDispatched.
    assert turns[0].twilio_sid == "ITEM_0001"


@pytest.mark.asyncio
async def test_dispatch_outbound_binds_routing_for_inbound_recovery(
    tmp_path: Path,
) -> None:
    """The dispatcher MUST bind phone -> case_id before sending so that
    the simulator webhook can resolve inbound replies back to the case.

    Regression guard for the bug that broke the SMS happy path in the
    first refactor pass: nothing in the new flow was calling
    ``routing.bind()``, so customer replies all hit the
    ``unknown_phone`` log and were dropped silently.
    """

    llm = FakeLlm(replies=["Hi from Kate."])
    sender = FakeSender()
    case, dispatcher, _, _, routing = _build_dispatcher(
        tmp_path, llm=llm, sender=sender
    )

    await dispatcher.dispatch_outbound(
        case_id=case.case_id,
        to_phone=case.customer.phone,
        stage=CallStage.OUTREACH,
    )

    entry = routing.find_entry(case.customer.phone)
    assert entry is not None
    assert entry.conversation_id == str(case.case_id)
    assert entry.channel == "sms"


@pytest.mark.asyncio
async def test_dispatch_outbound_skips_history_when_sender_fails(tmp_path: Path) -> None:
    """A failing send must not leave a phantom assistant turn in history."""

    llm = FakeLlm(replies=["this should not stick"])
    sender = FakeSender(fail_on_next=True)
    case, dispatcher, history, _, _ = _build_dispatcher(tmp_path, llm=llm, sender=sender)

    with pytest.raises(RuntimeError, match="simulated queue failure"):
        await dispatcher.dispatch_outbound(
            case_id=case.case_id,
            to_phone=case.customer.phone,
            stage=CallStage.OUTREACH,
        )

    # LLM was called; sender raised; history must remain untouched.
    assert len(llm.calls) == 1
    assert history.load(case.case_id) == ()


@pytest.mark.asyncio
async def test_dispatch_outbound_propagates_llm_failure_without_sending(
    tmp_path: Path,
) -> None:
    llm = FakeLlm(replies=[], fail_on_next=True)
    sender = FakeSender()
    case, dispatcher, history, _, _ = _build_dispatcher(
        tmp_path, llm=llm, sender=sender
    )

    with pytest.raises(RuntimeError, match="simulated LLM failure"):
        await dispatcher.dispatch_outbound(
            case_id=case.case_id,
            to_phone=case.customer.phone,
            stage=CallStage.OUTREACH,
        )

    assert sender.sent == []
    assert history.load(case.case_id) == ()


@pytest.mark.asyncio
async def test_record_inbound_appends_history(tmp_path: Path) -> None:
    llm = FakeLlm(replies=[])
    sender = FakeSender()
    case, dispatcher, history, _, _ = _build_dispatcher(
        tmp_path, llm=llm, sender=sender
    )

    await dispatcher.record_inbound(
        case_id=case.case_id,
        from_phone=case.customer.phone,
        body="1",
        message_sid="SMin_001",
    )

    turns = history.load(case.case_id)
    assert len(turns) == 1
    assert turns[0].role is TurnRole.USER
    assert turns[0].text == "1"
    assert turns[0].twilio_sid == "SMin_001"


def test_release_routing_unbinds_phone(tmp_path: Path) -> None:
    """The driver calls release_routing when a case turns terminal so
    a future inbound on the same phone is treated as unknown_phone."""

    llm = FakeLlm(replies=[])
    sender = FakeSender()
    case, dispatcher, _, _, routing = _build_dispatcher(
        tmp_path, llm=llm, sender=sender
    )
    routing.bind(
        phone=case.customer.phone,
        conversation_id=str(case.case_id),
        user_id="",
        channel="sms",
    )
    assert routing.find_entry(case.customer.phone) is not None

    dispatcher.release_routing(to_phone=case.customer.phone)

    assert routing.find_entry(case.customer.phone) is None
