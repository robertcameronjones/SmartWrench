"""Tests for ``ingest_post_call_report`` — the post-call cleanup helper."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from guidepoint.case import (
    CaseEvent,
    JsonCasePaths,
    PostCallReport,
    SlotId,
    TranscriptTurn,
    build_json_case_repository,
    ingest_post_call_report,
)
from guidepoint.events import build_event_bus
from tests.case._helpers import FixedClock, sample_case


def _booked_report() -> PostCallReport:
    return PostCallReport(
        elevenlabs_conversation_id="conv_abc",
        status="done",
        started_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 5, 10, 12, 0, 30, tzinfo=UTC),
        duration_seconds=30.0,
        transcript=(
            TranscriptTurn(role="agent", message="Hi, this is Kate.", time_in_call_seconds=2.0),
            TranscriptTurn(role="user", message="Hi Kate.", time_in_call_seconds=4.5),
        ),
        booked_slot_id=SlotId("slot_a"),
        business_outcome="booked",
    )


def _failed_report() -> PostCallReport:
    return PostCallReport(
        elevenlabs_conversation_id="conv_failed",
        status="failed",
        started_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 5, 10, 12, 0, 5, tzinfo=UTC),
        duration_seconds=5.0,
        transcript=(),
        business_outcome="inconclusive",
        error_detail="ElevenLabs SDK timeout",
    )


@pytest.mark.asyncio
async def test_ingest_done_report_returns_answered_outcome(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    repo.save(sample_case())
    bus = build_event_bus(payload_type=CaseEvent)
    case = repo.get(sample_case().case_id)

    outcome = await ingest_post_call_report(
        case=case,
        attempt_number=1,
        report=_booked_report(),
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
    )

    assert outcome.result == "answered"
    assert outcome.business_outcome == "booked"
    assert outcome.booked_slot_id == "slot_a"
    assert outcome.elevenlabs_conversation_id == "conv_abc"
    assert outcome.duration_seconds == 30.0
    assert "Kate: Hi, this is Kate." in outcome.transcript
    assert "Customer: Hi Kate." in outcome.transcript


@pytest.mark.asyncio
async def test_ingest_failed_report_returns_error_outcome(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    repo.save(sample_case())
    bus = build_event_bus(payload_type=CaseEvent)
    case = repo.get(sample_case().case_id)

    outcome = await ingest_post_call_report(
        case=case,
        attempt_number=1,
        report=_failed_report(),
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
    )

    assert outcome.result == "error"
    assert outcome.error_detail == "ElevenLabs SDK timeout"
    assert outcome.transcript == ""


@pytest.mark.asyncio
async def test_ingest_appends_transcript_event_to_case(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    repo.save(sample_case())
    bus = build_event_bus(payload_type=CaseEvent)
    case = repo.get(sample_case().case_id)

    await ingest_post_call_report(
        case=case,
        attempt_number=1,
        report=_booked_report(),
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
    )

    persisted = repo.get(case.case_id)
    audits = [e for e in persisted.events if e.event == "conversation.transcript_received"]
    assert len(audits) == 1
    audit = audits[0]
    assert audit.source == "elevenlabs"
    assert audit.attempt_number == 1
    assert "conversation_id=conv_abc" in audit.detail
    assert "turns=2" in audit.detail
    assert "status=done" in audit.detail


@pytest.mark.asyncio
async def test_ingest_publishes_to_bus(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    repo.save(sample_case())
    bus = build_event_bus(payload_type=CaseEvent)
    case = repo.get(sample_case().case_id)

    received: list[CaseEvent] = []

    async def consume() -> None:
        async for event in bus.subscribe():
            received.append(event)
            if event.event == "conversation.transcript_received":
                return

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await ingest_post_call_report(
        case=case,
        attempt_number=1,
        report=_booked_report(),
        case_repo=repo,
        bus=bus,
        clock=FixedClock(),
    )
    await asyncio.wait_for(consumer, timeout=2.0)
    assert any(e.event == "conversation.transcript_received" for e in received)
