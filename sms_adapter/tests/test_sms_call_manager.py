"""Phase 5 conformance tests for the SMS ``CallManager`` surface.

The SMS adapter has historically only exposed the v1
``CallSession.place(case)`` shape. Phase 5 adds the v2
``CallManager.start(*, case, stage, attempt_number)`` Protocol and
rebases ``place`` onto it. These tests pin:

- ``SmsCallSession`` structurally satisfies both Protocols.
- ``build_sms_call_manager`` returns the same instance typed as
  ``CallManager``.
- ``start`` carries ``stage`` through to the per-turn audit trail and
  respects the explicit ``attempt_number`` the v2 driver will pass.
- ``place`` keeps the v1 attempt-number convention so the existing
  voice/SMS dispatch in ``CaseManager`` is untouched.

The Twilio + LLM surfaces are stubbed with the same fakes used in
``test_call_session.py``; we only run the *start* path here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import final

import pytest

from guidepoint.case import (
    CallManager,
    CallStage,
    CaseEvent,
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
    build_sms_call_manager,
    build_sms_call_session,
)
from sms_adapter._call_session import SmsCallSession

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _prompt_paths() -> PromptPaths:
    return PromptPaths(
        system=_REPO_ROOT / "11Labs" / "config" / "system-prompt.md",
        post_booking=_REPO_ROOT / "11Labs" / "config" / "prompt-post-booking.md",
        voice=_REPO_ROOT / "11Labs" / "config" / "voice.md",
        sms=_REPO_ROOT / "sms_adapter" / "config" / "sms.md",
    )


# --------------------------------------------------------------------------- #
# Minimal fakes — kept tight; the long-tail behaviour is covered in           #
# ``test_call_session.py``.                                                   #
# --------------------------------------------------------------------------- #


@final
@dataclass
class FakeTwilio:
    sent: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, *, case_id: str, to: str, body: str) -> str:
        del case_id
        self.sent.append((to, body))
        return f"SM{len(self.sent):032x}"


@final
@dataclass
class FakeLlm:
    replies: list[str]

    def __call__(self, *, system: str, history: tuple[Turn, ...]) -> str:
        if not self.replies:
            raise AssertionError("FakeLlm exhausted")
        return self.replies.pop(0)


@final
class FixedClock:
    def __init__(self, *, instant: datetime | None = None) -> None:
        self._instant = instant or datetime(2026, 5, 10, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._instant


def _sample_case(case_repo, *, attempt_count: int = 0):
    trig = Trigger(
        id=TriggerId("trig_sms_5"),
        vehicle_vin=VehicleVin("1C4RJFBG5NC123456"),
        dealer_id=DealerId("d"),
        service_event=ServiceEvent(type="maintenance", summary="oil change"),
        channel_preference="sms",
        offered_slots=(
            OfferedSlot(
                id=SlotId("slot_a"),
                starts_at=datetime(2026, 5, 12, 13, 30, tzinfo=UTC),
                display="Tuesday 8:30 AM",
            ),
        ),
        created_at=datetime(2026, 5, 10, tzinfo=UTC),
    )
    customer = CustomerRecord(
        id=CustomerId("c"), first_name="Robert", last_name="Jones", phone="+15555550100"
    )
    dealer = DealerRecord(
        id=DealerId("d"),
        name="Village Jeep",
        phone="5559990000",
        address="1 Main St",
        ride_radius_miles=10,
    )
    vehicle = VehicleRecord(
        vin=VehicleVin("1C4RJFBG5NC123456"),
        owner_id=CustomerId("c"),
        year=2025,
        make="Jeep",
        model="GC",
        odometer_miles=100,
        current_location=Location(latitude=42.0, longitude=-83.0, description="here"),
    )
    case = create_case_from_trigger(
        trigger=trig,
        customer=customer,
        dealer=dealer,
        vehicle=vehicle,
        clock=FixedClock(),
    ).model_copy(update={"attempt_count": attempt_count})
    case_repo.save(case)
    return case


def _build(tmp_path: Path, llm_replies: list[str] | None = None):
    repo = build_json_case_repository(paths=JsonCasePaths(cases_dir=tmp_path / "cases"))
    history = build_json_history_store(root=tmp_path / "history")
    routing = build_json_routing_store(path=tmp_path / "routing.json")
    bus = build_event_bus(payload_type=CaseEvent)
    clock = FixedClock()
    twilio = FakeTwilio()
    llm = FakeLlm(replies=llm_replies or ["hi"])
    session = build_sms_call_session(
        twilio_send=twilio,
        llm_complete=llm,
        history=history,
        routing=routing,
        prompt_paths=_prompt_paths(),
        case_repo=repo,
        bus=bus,
        clock=clock,
        inactivity_timeout=timedelta(milliseconds=50),
    )
    return session, repo, twilio


# --------------------------------------------------------------------------- #
# Protocol conformance.                                                       #
# --------------------------------------------------------------------------- #


def test_sms_session_is_a_call_manager(tmp_path: Path) -> None:
    session, _repo, _twilio = _build(tmp_path)
    assert hasattr(session, "start") and callable(session.start)
    assert hasattr(session, "place") and callable(session.place)
    cm: CallManager = session  # type: ignore[assignment]
    assert cm is session


def test_build_sms_call_manager_returns_call_manager(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths(cases_dir=tmp_path / "cases"))
    history = build_json_history_store(root=tmp_path / "history")
    routing = build_json_routing_store(path=tmp_path / "routing.json")
    bus = build_event_bus(payload_type=CaseEvent)
    mgr = build_sms_call_manager(
        twilio_send=FakeTwilio(),
        llm_complete=FakeLlm(replies=["hi"]),
        history=history,
        routing=routing,
        prompt_paths=_prompt_paths(),
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
    )
    # Both Protocols satisfied by the one underlying instance.
    assert hasattr(mgr, "start")
    assert hasattr(mgr, "place")
    assert isinstance(mgr, SmsCallSession)


# --------------------------------------------------------------------------- #
# start() — stage carried through; explicit attempt_number honoured.          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_start_records_stage_in_opening_event(tmp_path: Path) -> None:
    session, repo, twilio = _build(tmp_path, llm_replies=["opener"])
    case = _sample_case(repo, attempt_count=2)

    # Inactivity timeout will fire quickly; we only care about the
    # opening event recorded *before* the loop times out.
    outcome = await session.start(
        case=case, stage=CallStage.INITIAL_REMINDER, attempt_number=5
    )
    assert outcome.business_outcome == "inconclusive"

    fresh = repo.get(case.case_id)
    opened = [e for e in fresh.events if e.event == "sms.opened"]
    assert opened, "expected sms.opened event"
    assert "stage=initial_reminder" in opened[0].detail
    assert opened[0].attempt_number == 5


@pytest.mark.asyncio
async def test_place_uses_v1_attempt_number_convention(tmp_path: Path) -> None:
    session, repo, twilio = _build(tmp_path, llm_replies=["opener"])
    case = _sample_case(repo, attempt_count=2)

    await session.place(case)

    fresh = repo.get(case.case_id)
    opened = [e for e in fresh.events if e.event == "sms.opened"]
    assert opened
    # v1 convention: attempt_count + 1.
    assert opened[0].attempt_number == 3
    assert "stage=outreach" in opened[0].detail
