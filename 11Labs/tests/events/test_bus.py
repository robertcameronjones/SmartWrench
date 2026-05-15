"""Tests for the in-process generic EventBus."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from guidepoint.case import CaseEvent, CaseId
from guidepoint.events import build_event_bus


def _event(name: str = "case.created") -> CaseEvent:
    return CaseEvent(
        event_id="evt_test",
        case_id=CaseId("case_x"),
        correlation_id="corr_x",
        timestamp=datetime(2026, 5, 10, tzinfo=UTC),
        event=name,
    )


@pytest.mark.asyncio
async def test_publish_to_no_subscribers_is_noop() -> None:
    bus = build_event_bus(payload_type=CaseEvent)
    await bus.publish(_event())  # must not raise


@pytest.mark.asyncio
async def test_subscriber_receives_published_event() -> None:
    bus = build_event_bus(payload_type=CaseEvent)
    received: list[CaseEvent] = []

    async def consume() -> None:
        async for event in bus.subscribe():
            received.append(event)
            return

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await bus.publish(_event("case.calling"))
    await asyncio.wait_for(consumer, timeout=1.0)
    assert received[0].event == "case.calling"


@pytest.mark.asyncio
async def test_two_subscribers_both_receive() -> None:
    bus = build_event_bus(payload_type=CaseEvent)
    received_a: list[CaseEvent] = []
    received_b: list[CaseEvent] = []

    async def consume(into: list[CaseEvent]) -> None:
        async for event in bus.subscribe():
            into.append(event)
            return

    a = asyncio.create_task(consume(received_a))
    b = asyncio.create_task(consume(received_b))
    await asyncio.sleep(0.01)
    await bus.publish(_event("case.created"))
    await asyncio.wait_for(asyncio.gather(a, b), timeout=1.0)
    assert len(received_a) == 1
    assert len(received_b) == 1
