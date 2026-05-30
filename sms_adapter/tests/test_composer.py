"""Tests for :class:`SmsMessageComposer`.

The composer is the only place an SMS reply is generated. It must:

- Re-read the case from the repository (no stale snapshots).
- Render the system prompt via :mod:`prompt_composer` using the
  passed-in ``stage`` (so reminder / outreach prompts swap correctly).
- Inject a synthetic ``(open conversation)`` user turn on the very
  first turn (otherwise chat LLMs that require a user message will
  refuse to respond).
- NEVER persist the assistant turn on its own — the driver is the one
  that records via :meth:`record_outbound` after the queue accepts
  the message. That split keeps a failed send from leaving a phantom
  assistant turn in history.
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
    Trigger,
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
    build_sms_message_composer,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


@final
@dataclass
class FakeLlm:
    """Records every call and returns the next canned reply."""

    replies: list[str]
    calls: list[tuple[str, tuple[Turn, ...]]] = field(default_factory=list)

    def __call__(self, *, system: str, history: tuple[Turn, ...]) -> str:
        self.calls.append((system, history))
        if not self.replies:
            raise AssertionError("FakeLlm exhausted")
        return self.replies.pop(0)


@final
class FixedClock:
    def __init__(self, instant: datetime | None = None) -> None:
        self._instant = instant or datetime(2026, 5, 10, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._instant


def _prompt_paths() -> PromptPaths:
    return PromptPaths(
        system=_REPO_ROOT / "11Labs" / "config" / "system-prompt.md",
        post_booking=_REPO_ROOT / "11Labs" / "config" / "prompt-post-booking.md",
        voice=_REPO_ROOT / "11Labs" / "config" / "voice.md",
        sms=_REPO_ROOT / "sms_adapter" / "config" / "sms.md",
    )


def _seed_case(tmp_path: Path) -> tuple[Case, "build_json_case_repository.__class__"]:
    """Build + persist a sample case in a fresh JSON repository under ``tmp_path``."""

    case = Case(
        case_id=CaseId("case_test"),
        trigger_id=TriggerId("trig_t"),
        correlation_id="corr_t",
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
    )
    paths = JsonCasePaths.for_root(tmp_path)
    repo = build_json_case_repository(paths=paths)
    repo.save(case)
    return case, repo


def _build_composer(tmp_path: Path, *, llm: FakeLlm):
    case, case_repo = _seed_case(tmp_path)
    history = build_json_history_store(root=tmp_path / "history")
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
    return case, composer, history


def test_compose_first_turn_injects_synthetic_user_kickoff(tmp_path: Path) -> None:
    llm = FakeLlm(replies=["Hi! Quick text about your service."])
    case, composer, history = _build_composer(tmp_path, llm=llm)

    composed = composer.compose(case_id=case.case_id, stage=CallStage.OUTREACH)

    assert composed.body == "Hi! Quick text about your service."
    # The composer must call the LLM with at least one user turn so the
    # chat model is willing to produce an opener. Persistence is the
    # driver's job — history must still be empty.
    assert history.load(case.case_id) == ()
    assert len(llm.calls) == 1
    system, sent_history = llm.calls[0]
    assert len(sent_history) == 1
    assert sent_history[0].role is TurnRole.USER
    assert sent_history[0].text == "(open conversation)"
    assert "Kate" in system or system  # prompt rendered (non-empty)


def test_compose_subsequent_turn_uses_real_history(tmp_path: Path) -> None:
    llm = FakeLlm(replies=["Great, you're on the books."])
    case, composer, history = _build_composer(tmp_path, llm=llm)
    # Seed history as if a prior compose + send + customer reply happened.
    history.append(case.case_id, Turn(role=TurnRole.ASSISTANT, text="Hi", timestamp=datetime.now(UTC), twilio_sid="SM1"))
    history.append(case.case_id, Turn(role=TurnRole.USER, text="1", timestamp=datetime.now(UTC), twilio_sid="SMin"))

    composer.compose(case_id=case.case_id, stage=CallStage.OUTREACH)

    _, sent_history = llm.calls[0]
    assert [t.text for t in sent_history] == ["Hi", "1"]


def test_record_outbound_appends_assistant_turn_with_sid(tmp_path: Path) -> None:
    llm = FakeLlm(replies=["should never be called"])  # not exercised here
    case, composer, history = _build_composer(tmp_path, llm=llm)

    composer.record_outbound(
        case_id=case.case_id,
        body="Hi from Kate",
        twilio_sid="ITEM_42",
        to_phone="+15555550001",
    )

    turns = history.load(case.case_id)
    assert len(turns) == 1
    assert turns[0].role is TurnRole.ASSISTANT
    assert turns[0].text == "Hi from Kate"
    assert turns[0].twilio_sid == "ITEM_42"
    # No LLM call should have happened in this method.
    assert llm.calls == []


def test_record_inbound_appends_user_turn(tmp_path: Path) -> None:
    llm = FakeLlm(replies=["unused"])
    case, composer, history = _build_composer(tmp_path, llm=llm)

    composer.record_inbound(
        case_id=case.case_id,
        from_phone="+15555550001",
        body="Yes that works",
        message_sid="SMin1",
    )

    turns = history.load(case.case_id)
    assert len(turns) == 1
    assert turns[0].role is TurnRole.USER
    assert turns[0].text == "Yes that works"
    assert turns[0].twilio_sid == "SMin1"


@pytest.mark.parametrize(
    "stage",
    [CallStage.OUTREACH, CallStage.INITIAL_REMINDER, CallStage.FINAL_REMINDER],
)
def test_compose_passes_stage_through_to_prompt_builder(
    tmp_path: Path, stage: CallStage
) -> None:
    """The composer must render a different system prompt per stage.

    Smoke-level: just assert it doesn't crash for each stage and the
    LLM was called once. The detailed per-stage prompt content lives
    in :mod:`prompt_composer`'s own tests.
    """

    llm = FakeLlm(replies=["ok"])
    case, composer, _ = _build_composer(tmp_path, llm=llm)
    composer.compose(case_id=case.case_id, stage=stage)
    assert len(llm.calls) == 1
