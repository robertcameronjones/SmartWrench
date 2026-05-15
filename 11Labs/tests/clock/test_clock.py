"""Smoke test for the system clock."""

from __future__ import annotations

from datetime import UTC, datetime

from guidepoint.clock import build_system_clock


def test_system_clock_returns_aware_utc_now() -> None:
    clock = build_system_clock()
    before = datetime.now(UTC)
    now = clock.now()
    after = datetime.now(UTC)
    assert now.tzinfo is not None
    assert before <= now <= after
