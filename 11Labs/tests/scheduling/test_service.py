"""Unit tests for the scheduling service.

Tests depend only on the public surface of ``guidepoint.scheduling`` plus the
in-memory ``InMemorySlotProvider`` fake. Production code is exercised entirely
through its Protocol — exactly how downstream consumers will use it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from guidepoint.scheduling import (
    CustomerId,
    DealerId,
    Slot,
    SlotId,
    SlotUnavailableError,
    build_scheduling,
)
from tests.scheduling._fakes import InMemorySlotProvider


def _slot(slot_id: str, dealer: str = "village-jeep", offset_hours: int = 1) -> Slot:
    return Slot(
        id=SlotId(slot_id),
        dealer_id=DealerId(dealer),
        starts_at=datetime(2026, 5, 11, 9, 0, tzinfo=UTC) + timedelta(hours=offset_hours),
        duration_minutes=60,
    )


class TestGetSlots:
    def test_returns_only_slots_for_requested_dealer(self) -> None:
        provider = InMemorySlotProvider(
            slots=(
                _slot("a", dealer="village-jeep"),
                _slot("b", dealer="other-dealer"),
                _slot("c", dealer="village-jeep"),
            ),
        )
        scheduling = build_scheduling(slot_provider=provider)

        result = scheduling.get_slots(dealer_id=DealerId("village-jeep"))

        assert {s.id for s in result} == {"a", "c"}

    def test_returns_empty_tuple_when_no_slots(self) -> None:
        provider = InMemorySlotProvider(slots=())
        scheduling = build_scheduling(slot_provider=provider)

        assert scheduling.get_slots(dealer_id=DealerId("village-jeep")) == ()


class TestBook:
    def test_books_an_open_slot(self) -> None:
        slot = _slot("a")
        provider = InMemorySlotProvider(slots=(slot,))
        scheduling = build_scheduling(slot_provider=provider)

        appt = scheduling.book(
            slot_id=slot.id,
            customer_id=CustomerId("cust-1"),
        )

        assert appt.slot == slot
        assert appt.customer_id == "cust-1"

    def test_booking_removes_slot_from_open_list(self) -> None:
        slot = _slot("a")
        provider = InMemorySlotProvider(slots=(slot,))
        scheduling = build_scheduling(slot_provider=provider)

        scheduling.book(slot_id=slot.id, customer_id=CustomerId("cust-1"))

        assert scheduling.get_slots(dealer_id=slot.dealer_id) == ()

    def test_double_book_raises_slot_unavailable(self) -> None:
        slot = _slot("a")
        provider = InMemorySlotProvider(slots=(slot,))
        scheduling = build_scheduling(slot_provider=provider)

        scheduling.book(slot_id=slot.id, customer_id=CustomerId("cust-1"))

        with pytest.raises(SlotUnavailableError) as excinfo:
            scheduling.book(slot_id=slot.id, customer_id=CustomerId("cust-2"))
        assert excinfo.value.slot_id == slot.id

    def test_unknown_slot_raises_slot_unavailable(self) -> None:
        provider = InMemorySlotProvider(slots=())
        scheduling = build_scheduling(slot_provider=provider)

        with pytest.raises(SlotUnavailableError):
            scheduling.book(
                slot_id=SlotId("does-not-exist"),
                customer_id=CustomerId("cust-1"),
            )
