"""Tests for the live ElevenLabs ``CallSession`` adapter.

Network is faked via a ``FakeElevenLabs`` stand-in that records
SDK calls and returns deterministic responses. We do **not** place
real outbound calls in tests.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, final

import pytest

from guidepoint.case import (
    CaseError,
    CaseEvent,
    JsonCasePaths,
    LiveCallSessionSettings,
    build_json_case_repository,
    build_live_call_session,
)
from guidepoint.events import build_event_bus
from tests.case._helpers import FixedClock, sample_case

# --------------------------------------------------------------------------- #
# Fakes mimicking the ElevenLabs SDK shape                                    #
# --------------------------------------------------------------------------- #


@final
@dataclass
class _FakeOutboundResponse:
    conversation_id: str


@final
@dataclass
class _FakeAnalysis:
    data_collection_results: dict[str, Any]


@final
@dataclass
class _FakeMetadata:
    call_duration_secs: float = 12.5


@final
@dataclass
class _FakeConversation:
    status: str
    transcript: list[dict[str, Any]] = field(default_factory=list)
    analysis: _FakeAnalysis | None = None
    metadata: _FakeMetadata = field(default_factory=_FakeMetadata)
    audio_url: str = ""


@final
class _FakeTwilio:
    def __init__(self, response: _FakeOutboundResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def outbound_call(self, **kwargs: Any) -> _FakeOutboundResponse:
        self.calls.append(kwargs)
        return self._response


@final
class _FakeConversations:
    def __init__(self, sequence: list[_FakeConversation]) -> None:
        self._sequence = list(sequence)
        self.gets: list[str] = []

    def get(self, *, conversation_id: str) -> _FakeConversation:
        self.gets.append(conversation_id)
        if len(self._sequence) == 1:
            return self._sequence[0]
        return self._sequence.pop(0)


@final
class _FakeConvai:
    def __init__(
        self, twilio: _FakeTwilio, conversations: _FakeConversations
    ) -> None:
        self.twilio = twilio
        self.conversations = conversations


@final
class _FakeElevenLabs:
    def __init__(
        self, *, response: _FakeOutboundResponse, conversations: _FakeConversations
    ) -> None:
        self.conversational_ai = _FakeConvai(_FakeTwilio(response), conversations)


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


def test_build_live_call_session_rejects_blank_agent_id(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    bus = build_event_bus(payload_type=CaseEvent)
    with pytest.raises(CaseError, match="agent_id required"):
        build_live_call_session(
            client=_FakeElevenLabs(
                response=_FakeOutboundResponse(conversation_id="conv_x"),
                conversations=_FakeConversations([_FakeConversation(status="done")]),
            ),  # type: ignore[arg-type]
            agent_id="",
            phone_number_id="phn_x",
            case_repo=repo,
            bus=bus,
            clock=FixedClock(),
        )


def test_build_live_call_session_rejects_blank_phone_number_id(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    bus = build_event_bus(payload_type=CaseEvent)
    with pytest.raises(CaseError, match="phone_number_id required"):
        build_live_call_session(
            client=_FakeElevenLabs(
                response=_FakeOutboundResponse(conversation_id="conv_x"),
                conversations=_FakeConversations([_FakeConversation(status="done")]),
            ),  # type: ignore[arg-type]
            agent_id="agent_x",
            phone_number_id="",
            case_repo=repo,
            bus=bus,
            clock=FixedClock(),
        )


@pytest.mark.asyncio
async def test_place_returns_booked_outcome_on_done_with_scheduled_fields(
    tmp_path: Path,
) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    repo.save(sample_case())
    bus = build_event_bus(payload_type=CaseEvent)
    fake = _FakeElevenLabs(
        response=_FakeOutboundResponse(conversation_id="conv_real"),
        conversations=_FakeConversations(
            [
                _FakeConversation(
                    status="done",
                    transcript=[
                        {"role": "agent", "message": "Hi Robert.", "time_in_call_secs": 2.0},
                        {"role": "user", "message": "Tuesday works.", "time_in_call_secs": 8.0},
                    ],
                    analysis=_FakeAnalysis(
                        data_collection_results={
                            "scheduled_date": "2026-05-12",
                            "scheduled_time": "08:30",
                        }
                    ),
                ),
            ]
        ),
    )
    session = build_live_call_session(
        client=fake,  # type: ignore[arg-type]
        agent_id="agent_x",
        phone_number_id="phn_x",
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
        settings=LiveCallSessionSettings(poll_interval_seconds=0.0, max_wait_seconds=10.0),
    )
    outcome = await session.place(repo.get(sample_case().case_id))
    assert outcome.result == "answered"
    assert outcome.business_outcome == "booked"
    assert outcome.booked_slot_id == "slot_chosen_20260512_0830"
    assert outcome.elevenlabs_conversation_id == "conv_real"
    assert "Kate: Hi Robert." in outcome.transcript
    assert "Customer: Tuesday works." in outcome.transcript


@final
@dataclass
class _FakeDataCollectionResult:
    """Mirrors the ElevenLabs ``DataCollectionResult`` wrapper shape.

    Real responses carry ``.value`` (the collected string or ``None``),
    ``.json_schema``, and ``.rationale``. We only ever read ``.value``;
    the others exist so ``str(wrapper)`` is the giant repr that bit us
    in case ``case_c514b77a212d2999``.
    """

    value: str | None
    rationale: str = ""
    json_schema: str = "LiteralJsonSchemaProperty(type='string')"


@pytest.mark.asyncio
async def test_place_inconclusive_when_wrapper_value_is_none(tmp_path: Path) -> None:
    """Regression for case_c514b77a212d2999: wrapper present, value None."""
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    repo.save(sample_case())
    bus = build_event_bus(payload_type=CaseEvent)
    fake = _FakeElevenLabs(
        response=_FakeOutboundResponse(conversation_id="conv_choke"),
        conversations=_FakeConversations(
            [
                _FakeConversation(
                    status="done",
                    transcript=[],
                    analysis=_FakeAnalysis(
                        data_collection_results={
                            "scheduled_date": _FakeDataCollectionResult(
                                value=None,
                                rationale="agent errored before collecting date",
                            ),
                            "scheduled_time": _FakeDataCollectionResult(
                                value=None,
                                rationale="agent errored before collecting time",
                            ),
                        }
                    ),
                ),
            ]
        ),
    )
    session = build_live_call_session(
        client=fake,  # type: ignore[arg-type]
        agent_id="agent_x",
        phone_number_id="phn_x",
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
        settings=LiveCallSessionSettings(poll_interval_seconds=0.0, max_wait_seconds=10.0),
    )
    outcome = await session.place(repo.get(sample_case().case_id))
    assert outcome.business_outcome == "inconclusive"
    assert outcome.booked_slot_id is None


@pytest.mark.asyncio
async def test_place_booked_when_wrapper_values_populated(tmp_path: Path) -> None:
    """Real ElevenLabs shape: wrapper objects with non-empty .value."""
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    repo.save(sample_case())
    bus = build_event_bus(payload_type=CaseEvent)
    fake = _FakeElevenLabs(
        response=_FakeOutboundResponse(conversation_id="conv_booked"),
        conversations=_FakeConversations(
            [
                _FakeConversation(
                    status="done",
                    transcript=[],
                    analysis=_FakeAnalysis(
                        data_collection_results={
                            "scheduled_date": _FakeDataCollectionResult(value="2026-05-12"),
                            "scheduled_time": _FakeDataCollectionResult(value="08:30"),
                        }
                    ),
                ),
            ]
        ),
    )
    session = build_live_call_session(
        client=fake,  # type: ignore[arg-type]
        agent_id="agent_x",
        phone_number_id="phn_x",
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
        settings=LiveCallSessionSettings(poll_interval_seconds=0.0, max_wait_seconds=10.0),
    )
    outcome = await session.place(repo.get(sample_case().case_id))
    assert outcome.business_outcome == "booked"
    assert outcome.booked_slot_id == "slot_chosen_20260512_0830"


@pytest.mark.asyncio
async def test_place_inconclusive_when_wrapper_value_is_empty_string(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    repo.save(sample_case())
    bus = build_event_bus(payload_type=CaseEvent)
    fake = _FakeElevenLabs(
        response=_FakeOutboundResponse(conversation_id="conv_empty"),
        conversations=_FakeConversations(
            [
                _FakeConversation(
                    status="done",
                    transcript=[],
                    analysis=_FakeAnalysis(
                        data_collection_results={
                            "scheduled_date": _FakeDataCollectionResult(value="   "),
                            "scheduled_time": _FakeDataCollectionResult(value=""),
                        }
                    ),
                ),
            ]
        ),
    )
    session = build_live_call_session(
        client=fake,  # type: ignore[arg-type]
        agent_id="agent_x",
        phone_number_id="phn_x",
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
        settings=LiveCallSessionSettings(poll_interval_seconds=0.0, max_wait_seconds=10.0),
    )
    outcome = await session.place(repo.get(sample_case().case_id))
    assert outcome.business_outcome == "inconclusive"
    assert outcome.booked_slot_id is None


@pytest.mark.asyncio
async def test_place_returns_inconclusive_when_no_scheduled_fields(
    tmp_path: Path,
) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    repo.save(sample_case())
    bus = build_event_bus(payload_type=CaseEvent)
    fake = _FakeElevenLabs(
        response=_FakeOutboundResponse(conversation_id="conv_x"),
        conversations=_FakeConversations(
            [
                _FakeConversation(
                    status="done",
                    transcript=[],
                    analysis=_FakeAnalysis(data_collection_results={}),
                )
            ]
        ),
    )
    session = build_live_call_session(
        client=fake,  # type: ignore[arg-type]
        agent_id="agent_x",
        phone_number_id="phn_x",
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
        settings=LiveCallSessionSettings(poll_interval_seconds=0.0, max_wait_seconds=10.0),
    )
    outcome = await session.place(repo.get(sample_case().case_id))
    assert outcome.result == "answered"
    assert outcome.business_outcome == "inconclusive"
    assert outcome.booked_slot_id is None


@pytest.mark.asyncio
async def test_place_returns_error_outcome_when_status_failed(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    repo.save(sample_case())
    bus = build_event_bus(payload_type=CaseEvent)
    fake = _FakeElevenLabs(
        response=_FakeOutboundResponse(conversation_id="conv_failed"),
        conversations=_FakeConversations([_FakeConversation(status="failed")]),
    )
    session = build_live_call_session(
        client=fake,  # type: ignore[arg-type]
        agent_id="agent_x",
        phone_number_id="phn_x",
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
        settings=LiveCallSessionSettings(poll_interval_seconds=0.0, max_wait_seconds=10.0),
    )
    outcome = await session.place(repo.get(sample_case().case_id))
    assert outcome.result == "error"
    assert "status=failed" in outcome.error_detail


@pytest.mark.asyncio
async def test_place_passes_agent_id_phone_and_dynamic_variables(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    case = sample_case()
    repo.save(case)
    bus = build_event_bus(payload_type=CaseEvent)
    fake = _FakeElevenLabs(
        response=_FakeOutboundResponse(conversation_id="conv_x"),
        conversations=_FakeConversations(
            [_FakeConversation(status="done", transcript=[], analysis=None)]
        ),
    )
    session = build_live_call_session(
        client=fake,  # type: ignore[arg-type]
        agent_id="agent_x",
        phone_number_id="phn_x",
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
        settings=LiveCallSessionSettings(poll_interval_seconds=0.0, max_wait_seconds=10.0),
    )
    await session.place(repo.get(case.case_id))
    sdk_call = fake.conversational_ai.twilio.calls[0]
    assert sdk_call["agent_id"] == "agent_x"
    assert sdk_call["agent_phone_number_id"] == "phn_x"
    assert sdk_call["to_number"] == case.customer.phone
    assert "slot_options" in sdk_call["conversation_initiation_client_data"]["dynamic_variables"]


@pytest.mark.asyncio
async def test_place_appends_dialing_and_placed_events(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    case = sample_case()
    repo.save(case)
    bus = build_event_bus(payload_type=CaseEvent)
    fake = _FakeElevenLabs(
        response=_FakeOutboundResponse(conversation_id="conv_x"),
        conversations=_FakeConversations(
            [_FakeConversation(status="done", transcript=[], analysis=None)]
        ),
    )
    session = build_live_call_session(
        client=fake,  # type: ignore[arg-type]
        agent_id="agent_x",
        phone_number_id="phn_x",
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
        settings=LiveCallSessionSettings(poll_interval_seconds=0.0, max_wait_seconds=10.0),
    )
    await session.place(repo.get(case.case_id))
    persisted = repo.get(case.case_id)
    names = [e.event for e in persisted.events]
    assert "call.dialing" in names
    assert "call.placed" in names
    assert "conversation.transcript_received" in names


@pytest.mark.asyncio
async def test_place_raises_case_error_when_sdk_fails(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    case = sample_case()
    repo.save(case)
    bus = build_event_bus(payload_type=CaseEvent)

    class _BoomTwilio:
        def outbound_call(self, **_: Any) -> Any:
            raise RuntimeError("simulated SDK explosion")

    class _BoomConvai:
        twilio = _BoomTwilio()
        conversations = _FakeConversations([_FakeConversation(status="done")])

    class _BoomClient:
        conversational_ai = _BoomConvai()

    session = build_live_call_session(
        client=_BoomClient(),  # type: ignore[arg-type]
        agent_id="agent_x",
        phone_number_id="phn_x",
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
        settings=LiveCallSessionSettings(poll_interval_seconds=0.0, max_wait_seconds=10.0),
    )
    with pytest.raises(CaseError, match="outbound_call failed"):
        await session.place(repo.get(case.case_id))
    persisted = repo.get(case.case_id)
    assert any(e.event == "call.placement_failed" for e in persisted.events)


@pytest.mark.asyncio
async def test_place_publishes_to_event_bus(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    case = sample_case()
    repo.save(case)
    bus = build_event_bus(payload_type=CaseEvent)
    fake = _FakeElevenLabs(
        response=_FakeOutboundResponse(conversation_id="conv_x"),
        conversations=_FakeConversations(
            [_FakeConversation(status="done", transcript=[], analysis=None)]
        ),
    )
    session = build_live_call_session(
        client=fake,  # type: ignore[arg-type]
        agent_id="agent_x",
        phone_number_id="phn_x",
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
        settings=LiveCallSessionSettings(poll_interval_seconds=0.0, max_wait_seconds=10.0),
    )

    received: list[CaseEvent] = []

    async def consume() -> None:
        async for event in bus.subscribe():
            received.append(event)
            if event.event == "conversation.transcript_received":
                return

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await session.place(repo.get(case.case_id))
    await asyncio.wait_for(consumer, timeout=2.0)
    assert any(e.event == "call.placed" for e in received)


# Mark unused datetime import if pyright complains
_ = datetime(2026, 5, 10, tzinfo=UTC)
