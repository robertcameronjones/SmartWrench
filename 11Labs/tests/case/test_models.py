"""Validation tests for case-domain boundary models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from guidepoint.case import (
    Case,
    CaseState,
    OfferedSlot,
    PostCallReport,
    ServiceEvent,
    SlotId,
    TranscriptTurn,
)
from tests.case._helpers import sample_case


class TestServiceEvent:
    def test_valid(self) -> None:
        s = ServiceEvent(type="maintenance", summary="x", narrative="y")
        assert s.summary == "x"

    def test_invalid_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ServiceEvent(type="oil_change", summary="x")  # type: ignore[arg-type]


class TestOfferedSlot:
    def test_round_trip(self) -> None:
        slot = OfferedSlot(
            id=SlotId("s"),
            starts_at=datetime(2026, 5, 12, tzinfo=UTC),
            display="Mon 9:00",
        )
        assert OfferedSlot.model_validate(slot.model_dump()) == slot


class TestCaseStates:
    def test_terminal_states_marked(self) -> None:
        assert CaseState.BOOKED.is_terminal
        assert CaseState.UNREACHABLE.is_terminal
        assert CaseState.CANCELLED.is_terminal
        assert CaseState.ESCALATED.is_terminal
        assert CaseState.DECLINED.is_terminal

    def test_non_terminal_states(self) -> None:
        assert not CaseState.CREATED.is_terminal
        assert not CaseState.READY_TO_CALL.is_terminal
        assert not CaseState.CALLING.is_terminal
        assert not CaseState.BETWEEN_ATTEMPTS.is_terminal


class TestCase:
    def test_to_variables_includes_every_expected_key(self) -> None:
        flat = sample_case().to_variables()
        for key in (
            "case_id",
            "trigger_id",
            "customer_first_name",
            "customer_full_name",
            "customer_phone",
            "dealer_name",
            "dealer_phone",
            "ride_radius_miles",
            "vehicle_year",
            "vehicle_make",
            "vehicle_vin",
            "vehicle_odometer_miles",
            "service_reason_type",
            "service_reason_summary",
            "service_reason_narrative",
            "slot_count",
            "slot_options",
        ):
            assert key in flat

    def test_to_variables_values_are_strings(self) -> None:
        flat = sample_case().to_variables()
        assert all(isinstance(v, str) for v in flat.values())
        assert flat["vehicle_year"] == "2025"
        assert flat["slot_count"] == "1"

    def test_variable_keys_matches_to_variables(self) -> None:
        case = sample_case()
        assert Case.variable_keys() == frozenset(case.to_variables().keys())

    def test_round_trip(self) -> None:
        case = sample_case()
        assert Case.model_validate(case.model_dump()) == case

    def test_extra_field_rejected(self) -> None:
        case = sample_case()
        bad = {**case.model_dump(mode="json"), "rogue": True}
        with pytest.raises(ValidationError):
            Case.model_validate(bad)


class TestTranscriptTurn:
    def test_round_trip(self) -> None:
        turn = TranscriptTurn(role="agent", message="hi", time_in_call_seconds=2.0)
        assert TranscriptTurn.model_validate(turn.model_dump()) == turn

    def test_negative_time_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TranscriptTurn(role="agent", message="hi", time_in_call_seconds=-1.0)

    def test_invalid_role_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TranscriptTurn(role="bot", message="hi", time_in_call_seconds=0.0)  # type: ignore[arg-type]

    def test_empty_message_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TranscriptTurn(role="agent", message="", time_in_call_seconds=0.0)


class TestPostCallReport:
    def test_round_trip(self) -> None:
        report = PostCallReport(
            elevenlabs_conversation_id="conv_x",
            status="done",
            started_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
            ended_at=datetime(2026, 5, 10, 12, 0, 30, tzinfo=UTC),
            duration_seconds=30.0,
        )
        assert PostCallReport.model_validate(report.model_dump()) == report

    def test_default_business_outcome_is_inconclusive(self) -> None:
        report = PostCallReport(
            elevenlabs_conversation_id="c",
            status="done",
            started_at=datetime(2026, 5, 10, tzinfo=UTC),
            ended_at=datetime(2026, 5, 10, tzinfo=UTC),
            duration_seconds=0.0,
        )
        assert report.business_outcome == "inconclusive"
        assert report.transcript == ()

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PostCallReport.model_validate(
                {
                    "elevenlabs_conversation_id": "c",
                    "status": "done",
                    "started_at": "2026-05-10T12:00:00Z",
                    "ended_at": "2026-05-10T12:00:30Z",
                    "duration_seconds": 30.0,
                    "rogue": True,
                }
            )
