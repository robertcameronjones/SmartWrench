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


@pytest.mark.asyncio
async def test_subscriber_depths_reports_active_queues() -> None:
    """Each live subscriber appears once in the depth snapshot."""
    bus = build_event_bus(payload_type=CaseEvent)
    assert bus.subscriber_depths() == ()

    async def consume() -> None:
        async for _event in bus.subscribe():
            await asyncio.sleep(10)  # keep the subscription alive

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    depths = bus.subscriber_depths()
    assert len(depths) == 1
    current, max_depth = depths[0]
    assert current == 0
    assert max_depth == 256

    consumer.cancel()


@pytest.mark.asyncio
async def test_slow_consumer_caps_at_queue_max_does_not_block_publisher() -> None:
    """A subscriber that doesn't drain stops receiving past the queue max.

    Verifies the bounded-queue contract: publishes never block, the
    subscriber's queue plateaus at the configured max, and additional
    events are dropped (overflow path is exercised — the structured
    warning is emitted via structlog; behavioral correctness is checked
    here via the queue-depth accessor).
    """
    from guidepoint.events._bus import _QUEUE_MAX

    bus = build_event_bus(payload_type=CaseEvent)

    # Register a subscriber that never reads from its queue.
    async def park() -> None:
        async for _event in bus.subscribe():
            await asyncio.sleep(10)

    consumer = asyncio.create_task(park())
    await asyncio.sleep(0.01)

    # Publish well beyond the queue's max — must not block.
    publish_count = _QUEUE_MAX + 50
    await asyncio.wait_for(
        asyncio.gather(*[bus.publish(_event("case.test")) for _ in range(publish_count)]),
        timeout=2.0,
    )

    # Subscriber's queue is now at (or one below) max — overflow path
    # was exercised. The consumer may have dequeued one event before
    # parking on its inner sleep, so allow a one-event slack.
    depths = bus.subscriber_depths()
    assert len(depths) == 1
    current, max_depth = depths[0]
    assert max_depth == _QUEUE_MAX
    assert current >= _QUEUE_MAX - 1

    consumer.cancel()
