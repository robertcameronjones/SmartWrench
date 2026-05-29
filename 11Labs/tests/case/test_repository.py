"""JSON-file CaseRepository round-trip tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from guidepoint.case import (
    CallAttempt,
    CallOutcome,
    CaseEvent,
    CaseId,
    CaseNotFoundError,
    CaseState,
    JsonCasePaths,
    SlotId,
    build_json_case_repository,
)
from tests.case._helpers import sample_case


class TestJsonCasePaths:
    def test_layout_under_root(self, tmp_path: Path) -> None:
        paths = JsonCasePaths.for_root(tmp_path)
        assert paths.cases_dir == (tmp_path / "fixtures" / "cases").resolve()


class TestSaveAndGet:
    def test_round_trip(self, tmp_path: Path) -> None:
        repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
        case = sample_case()
        repo.save(case)
        assert repo.get(CaseId("case_1")) == case

    def test_get_missing_raises(self, tmp_path: Path) -> None:
        repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
        with pytest.raises(CaseNotFoundError):
            repo.get(CaseId("nope"))


class TestStateTransitions:
    def test_update_state(self, tmp_path: Path) -> None:
        repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
        repo.save(sample_case())
        updated = repo.update_state(
            CaseId("case_1"), new_state=CaseState.CONTACTING_CUSTOMER
        )
        assert updated.state == CaseState.CONTACTING_CUSTOMER
        assert repo.get(CaseId("case_1")).state == CaseState.CONTACTING_CUSTOMER

    def test_update_outcome(self, tmp_path: Path) -> None:
        repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
        repo.save(sample_case())
        closed = datetime(2026, 5, 11, tzinfo=UTC)
        updated = repo.update_outcome(
            CaseId("case_1"),
            new_state=CaseState.BOOKED,
            outcome_detail="booked slot_a",
            booked_slot_id=SlotId("slot_a"),
            booked_slot_display="Tuesday, May 12, 2026 - 8:30 AM",
            closed_at=closed,
        )
        assert updated.state == CaseState.BOOKED
        assert updated.booked_slot_id == "slot_a"
        assert updated.booked_slot_display == "Tuesday, May 12, 2026 - 8:30 AM"
        assert updated.closed_at == closed


class TestAppends:
    def test_append_event(self, tmp_path: Path) -> None:
        repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
        repo.save(sample_case())
        event = CaseEvent(
            event_id="evt_1",
            case_id=CaseId("case_1"),
            correlation_id="corr_test",
            timestamp=datetime(2026, 5, 11, tzinfo=UTC),
            event="case.created",
        )
        updated = repo.append_event(CaseId("case_1"), event)
        assert updated.events == (event,)

    def test_append_call_attempt_increments_count(self, tmp_path: Path) -> None:
        repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
        repo.save(sample_case())
        outcome = CallOutcome(
            result="answered",
            business_outcome="booked",
            booked_slot_id=SlotId("slot_a"),
            started_at=datetime(2026, 5, 11, 12, tzinfo=UTC),
            ended_at=datetime(2026, 5, 11, 12, 1, tzinfo=UTC),
            duration_seconds=60.0,
        )
        attempt = CallAttempt(attempt_number=1, outcome=outcome)
        updated = repo.append_call_attempt(CaseId("case_1"), attempt)
        assert updated.attempt_count == 1
        assert updated.call_attempts == (attempt,)


class TestListing:
    def test_list_by_state(self, tmp_path: Path) -> None:
        repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
        repo.save(sample_case(case_id="case_a"))
        booked = sample_case(case_id="case_b").model_copy(update={"state": CaseState.BOOKED})
        repo.save(booked)
        assert tuple(c.case_id for c in repo.list_by_state(CaseState.BOOKED)) == ("case_b",)

    def test_list_recent_returns_newest_first(self, tmp_path: Path) -> None:
        repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
        a = sample_case(case_id="case_a").model_copy(
            update={"created_at": datetime(2026, 1, 1, tzinfo=UTC)}
        )
        b = sample_case(case_id="case_b").model_copy(
            update={"created_at": datetime(2026, 6, 1, tzinfo=UTC)}
        )
        repo.save(a)
        repo.save(b)
        recent = tuple(repo.list_recent(limit=10))
        assert recent[0].case_id == "case_b"
        assert recent[1].case_id == "case_a"
