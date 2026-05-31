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
        assert CaseState.DECLINED.is_terminal
        assert CaseState.CANCELLED.is_terminal
        assert CaseState.ABANDONED.is_terminal
        assert CaseState.NO_SHOW.is_terminal
        assert CaseState.RESCHEDULE_FAILED.is_terminal
        assert CaseState.OPTED_OUT.is_terminal
        assert CaseState.COMPLETED.is_terminal

    def test_non_terminal_states(self) -> None:
        for state in (
            CaseState.CREATED,
            CaseState.CONTACTING_CUSTOMER,
            CaseState.BOOKED,
            CaseState.INITIAL_REMINDER_DUE,
            CaseState.INITIAL_REMINDER_SENT,
            CaseState.RESCHEDULING,
            CaseState.FINAL_REMINDER_DUE,
            CaseState.FINAL_REMINDER_SENT,
            CaseState.SHOWED,
        ):
            assert not state.is_terminal, f"{state.value} should be non-terminal"


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
            "dealer_address",
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
            "booked_slot_display",
            "context_notes",
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

    def test_initial_channel_defaults_to_voice(self) -> None:
        case = sample_case()
        assert case.initial_channel == "voice"

    def test_reschedule_count_defaults_to_zero(self) -> None:
        assert sample_case().reschedule_count == 0

    def test_context_notes_defaults_empty(self) -> None:
        assert sample_case().context_notes == ""

    def test_context_notes_length_capped(self) -> None:
        case = sample_case()
        bad = {**case.model_dump(mode="json"), "context_notes": "x" * 201}
        with pytest.raises(ValidationError):
            Case.model_validate(bad)

    def test_booked_slot_display_defaults_empty(self) -> None:
        assert sample_case().booked_slot_display == ""

    def test_to_variables_uses_booked_slot_display(self) -> None:
        case = sample_case()
        case = case.model_copy(
            update={
                "booked_slot_id": SlotId("slot_a"),
                "booked_slot_display": "Tuesday, May 12, 2026 - 8:30 AM",
            }
        )
        assert case.to_variables()["booked_slot_display"] == (
            "Tuesday, May 12, 2026 - 8:30 AM"
        )

    def test_to_variables_falls_back_to_offered_slot_display(self) -> None:
        case = sample_case().model_copy(update={"booked_slot_id": SlotId("slot_a")})
        flat = case.to_variables()
        assert "slot_a" not in flat["booked_slot_display"]
        assert flat["booked_slot_display"] == case.offered_slots[0].display

    def test_to_variables_channel_sourced_from_initial_channel(self) -> None:
        flat = sample_case().to_variables()
        # Phase 7 may rename the dict key; today we keep it stable for
        # the existing system-prompt.md template.
        assert flat["channel"] == "voice"


class TestLegacyMigration:
    """The v1→v2 model validator: legacy JSON loads as v2 shape."""

    def _v1_case_dict(self) -> dict[str, object]:
        """A minimal case dict in v1 shape (no validator-rejected fields)."""
        case = sample_case()
        raw = case.model_dump(mode="json")
        # Reshape into v1: drop v2 fields, rename initial_channel back.
        raw["channel"] = raw.pop("initial_channel")
        raw.pop("reschedule_count", None)
        raw.pop("context_notes", None)
        return raw

    def test_legacy_channel_field_migrates_to_initial_channel(self) -> None:
        v1 = self._v1_case_dict()
        v1["channel"] = "sms"
        loaded = Case.model_validate(v1)
        assert loaded.initial_channel == "sms"

    def test_legacy_calling_state_migrates_to_contacting_customer(self) -> None:
        v1 = self._v1_case_dict()
        v1["state"] = "calling"
        loaded = Case.model_validate(v1)
        assert loaded.state == CaseState.CONTACTING_CUSTOMER

    def test_legacy_ready_to_call_state_migrates_to_contacting_customer(self) -> None:
        v1 = self._v1_case_dict()
        v1["state"] = "ready_to_call"
        loaded = Case.model_validate(v1)
        assert loaded.state == CaseState.CONTACTING_CUSTOMER

    def test_legacy_between_attempts_state_migrates_to_contacting_customer(self) -> None:
        v1 = self._v1_case_dict()
        v1["state"] = "between_attempts"
        loaded = Case.model_validate(v1)
        assert loaded.state == CaseState.CONTACTING_CUSTOMER

    def test_legacy_unreachable_state_migrates_to_abandoned(self) -> None:
        v1 = self._v1_case_dict()
        v1["state"] = "unreachable"
        loaded = Case.model_validate(v1)
        assert loaded.state == CaseState.ABANDONED

    def test_legacy_escalated_state_migrates_to_abandoned(self) -> None:
        v1 = self._v1_case_dict()
        v1["state"] = "escalated"
        loaded = Case.model_validate(v1)
        assert loaded.state == CaseState.ABANDONED

    def test_legacy_awaiting_reminder_migrates_to_initial_reminder_due(self) -> None:
        v1 = self._v1_case_dict()
        v1["state"] = "awaiting_reminder"
        loaded = Case.model_validate(v1)
        assert loaded.state == CaseState.INITIAL_REMINDER_DUE

    def test_legacy_reminded_migrates_to_initial_reminder_sent(self) -> None:
        v1 = self._v1_case_dict()
        v1["state"] = "reminded"
        loaded = Case.model_validate(v1)
        assert loaded.state == CaseState.INITIAL_REMINDER_SENT

    def test_legacy_confirmed_migrates_to_final_reminder_due(self) -> None:
        v1 = self._v1_case_dict()
        v1["state"] = "confirmed"
        loaded = Case.model_validate(v1)
        assert loaded.state == CaseState.FINAL_REMINDER_DUE

    def test_legacy_day_of_migrates_to_final_reminder_sent(self) -> None:
        v1 = self._v1_case_dict()
        v1["state"] = "day_of"
        loaded = Case.model_validate(v1)
        assert loaded.state == CaseState.FINAL_REMINDER_SENT

    def test_legacy_case_defaults_reschedule_count(self) -> None:
        v1 = self._v1_case_dict()
        loaded = Case.model_validate(v1)
        assert loaded.reschedule_count == 0

    def test_legacy_case_defaults_context_notes(self) -> None:
        v1 = self._v1_case_dict()
        loaded = Case.model_validate(v1)
        assert loaded.context_notes == ""


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
