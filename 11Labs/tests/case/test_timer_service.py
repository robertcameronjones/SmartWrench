"""Tests for ``InMemoryTimerService``."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from guidepoint.case import CaseId, InMemoryTimerService
from tests.case._helpers import FixedClock


@pytest.mark.asyncio
async def test_timer_fires_after_delay() -> None:
    clock = FixedClock(instant=datetime(2026, 5, 10, 12, 0, tzinfo=UTC))
    fired: list[tuple[CaseId, str]] = []

    async def _fire(case_id: CaseId, name: str) -> None:
        fired.append((case_id, name))

    svc = InMemoryTimerService(clock=clock, fire=_fire)
    case_id = CaseId("case_timer_1")
    svc.schedule(
        case_id=case_id,
        name="initial_reminder",
        fire_at=clock.now() + timedelta(milliseconds=30),
    )
    assert svc.pending() == ((case_id, "initial_reminder"),)
    await asyncio.sleep(0.05)
    assert fired == [(case_id, "initial_reminder")]
    assert svc.pending() == ()


@pytest.mark.asyncio
async def test_timer_cancel_prevents_fire() -> None:
    clock = FixedClock(instant=datetime(2026, 5, 10, 12, 0, tzinfo=UTC))
    fired: list[tuple[CaseId, str]] = []

    async def _fire(case_id: CaseId, name: str) -> None:
        fired.append((case_id, name))

    svc = InMemoryTimerService(clock=clock, fire=_fire)
    case_id = CaseId("case_timer_2")
    svc.schedule(
        case_id=case_id,
        name="final_reminder",
        fire_at=clock.now() + timedelta(seconds=1),
    )
    svc.cancel(case_id=case_id, name="final_reminder")
    await asyncio.sleep(0.05)
    assert fired == []
    assert svc.pending() == ()


@pytest.mark.asyncio
async def test_schedule_replaces_same_name() -> None:
    clock = FixedClock(instant=datetime(2026, 5, 10, 12, 0, tzinfo=UTC))
    fired: list[str] = []

    async def _fire(_case_id: CaseId, name: str) -> None:
        fired.append(name)

    svc = InMemoryTimerService(clock=clock, fire=_fire)
    case_id = CaseId("case_timer_3")
    svc.schedule(
        case_id=case_id,
        name="initial_reminder",
        fire_at=clock.now() + timedelta(seconds=5),
    )
    svc.schedule(
        case_id=case_id,
        name="initial_reminder",
        fire_at=clock.now() + timedelta(milliseconds=20),
    )
    await asyncio.sleep(0.05)
    assert fired == ["initial_reminder"]
    assert len(svc.pending()) == 0
