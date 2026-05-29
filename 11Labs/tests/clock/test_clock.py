"""Smoke test for the system clock + UtcDatetime discipline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from guidepoint.clock import UtcDatetime, build_system_clock


def test_system_clock_returns_aware_utc_now() -> None:
    clock = build_system_clock()
    before = datetime.now(UTC)
    now = clock.now()
    after = datetime.now(UTC)
    assert now.tzinfo is not None
    assert before <= now <= after


# ---------------------------------------------------------------------------
# UtcDatetime discipline: tz-aware in, normalized to UTC out; naive rejected.
# ---------------------------------------------------------------------------


class _M(BaseModel):
    """Throwaway model that just exercises the UtcDatetime validator."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    at: UtcDatetime


def test_utcdatetime_accepts_utc_aware_unchanged() -> None:
    src = datetime(2026, 5, 11, 12, 30, tzinfo=UTC)
    assert _M(at=src).at == src


def test_utcdatetime_normalizes_non_utc_aware_to_utc() -> None:
    eastern = timezone(timedelta(hours=-4))
    src = datetime(2026, 5, 11, 9, 0, tzinfo=eastern)
    out = _M(at=src).at
    assert out.tzinfo is UTC
    assert out == datetime(2026, 5, 11, 13, 0, tzinfo=UTC)


def test_utcdatetime_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _M(at=datetime(2026, 5, 11, 12, 30))  # noqa: DTZ001 — testing rejection
    assert "naive datetime not allowed" in str(excinfo.value)


def test_utcdatetime_round_trips_through_json() -> None:
    src = datetime(2026, 5, 11, 12, 30, tzinfo=UTC)
    payload = _M(at=src).model_dump_json()
    restored = _M.model_validate_json(payload)
    assert restored.at == src
    assert restored.at.tzinfo is UTC
