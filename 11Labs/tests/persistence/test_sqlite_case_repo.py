"""SQLite CaseRepository round-trip tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from guidepoint.case import (
    CallAttempt,
    CallOutcome,
    CaseId,
    CaseNotFoundError,
    CaseState,
    JsonCasePaths,
    SlotId,
    build_json_case_repository,
)
from guidepoint.persistence import build_case_repository, migrate_cases_json_to_sqlite
from guidepoint.persistence.sqlite import build_sqlite_case_repository
from tests.case._helpers import sample_case


class TestSqliteCaseRepository:
    def test_round_trip(self, tmp_path: Path) -> None:
        repo = build_sqlite_case_repository(db_path=tmp_path / "cases.db")
        case = sample_case()
        repo.save(case)
        assert repo.get(CaseId("case_1")) == case

    def test_get_missing_raises(self, tmp_path: Path) -> None:
        repo = build_sqlite_case_repository(db_path=tmp_path / "cases.db")
        with pytest.raises(CaseNotFoundError):
            repo.get(CaseId("nope"))

    def test_list_active_excludes_terminal(self, tmp_path: Path) -> None:
        repo = build_sqlite_case_repository(db_path=tmp_path / "cases.db")
        repo.save(sample_case(case_id="case_a"))
        repo.save(
            sample_case(case_id="case_b").model_copy(update={"state": CaseState.BOOKED})
        )
        active = tuple(c.case_id for c in repo.list_active())
        assert active == ("case_a",)

    def test_list_by_customer_phone(self, tmp_path: Path) -> None:
        repo = build_sqlite_case_repository(db_path=tmp_path / "cases.db")
        repo.save(sample_case(case_id="case_a"))
        matches = tuple(repo.list_by_customer_phone("+13139095330"))
        assert len(matches) == 1
        assert matches[0].case_id == "case_a"

    def test_append_call_attempt(self, tmp_path: Path) -> None:
        repo = build_sqlite_case_repository(db_path=tmp_path / "cases.db")
        repo.save(sample_case())
        outcome = CallOutcome(
            result="answered",
            business_outcome="booked",
            booked_slot_id=SlotId("slot_a"),
            started_at=datetime(2026, 5, 11, 12, tzinfo=UTC),
            ended_at=datetime(2026, 5, 11, 12, 1, tzinfo=UTC),
            duration_seconds=60.0,
        )
        updated = repo.append_call_attempt(
            CaseId("case_1"),
            CallAttempt(attempt_number=1, outcome=outcome),
        )
        assert updated.attempt_count == 1


class TestBuildCaseRepository:
    def test_json_backend(self, tmp_path: Path) -> None:
        repo = build_case_repository(backend="json", project_root=tmp_path)
        case = sample_case()
        repo.save(case)
        assert repo.get(CaseId("case_1")) == case

    def test_sqlite_backend(self, tmp_path: Path) -> None:
        repo = build_case_repository(backend="sqlite", project_root=tmp_path)
        case = sample_case()
        repo.save(case)
        assert repo.get(CaseId("case_1")) == case


class TestMigrateFromJson:
    def test_copies_json_cases(self, tmp_path: Path) -> None:
        json_paths = JsonCasePaths.for_root(tmp_path)
        json_repo = build_json_case_repository(paths=json_paths)
        json_repo.save(sample_case(case_id="case_migrated"))
        db_path = tmp_path / "data" / "guidepoint.db"
        count = migrate_cases_json_to_sqlite(json_paths=json_paths, db_path=db_path)
        assert count == 1
        sqlite_repo = build_sqlite_case_repository(db_path=db_path)
        assert sqlite_repo.get(CaseId("case_migrated")).case_id == "case_migrated"
