"""Shared test helpers for the case-domain suite."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import final

from guidepoint.case import (
    CallOutcome,
    Case,
    CaseId,
    CaseState,
    OfferedSlot,
    ServiceEvent,
    SlotId,
    Trigger,
    TriggerId,
)
from guidepoint.master_data import (
    CustomerId,
    CustomerRecord,
    DealerId,
    DealerRecord,
    Location,
    VehicleRecord,
    VehicleVin,
)


@final
class FixedClock:
    def __init__(self, *, instant: datetime | None = None) -> None:
        self._instant = instant or datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._instant


def sample_customer(*, customer_id: str = "c") -> CustomerRecord:
    return CustomerRecord(
        id=CustomerId(customer_id),
        first_name="Robert",
        last_name="Jones",
        phone="+13139095330",
    )


def sample_dealer(*, dealer_id: str = "d") -> DealerRecord:
    return DealerRecord(
        id=DealerId(dealer_id),
        name="Village Jeep",
        phone="5559990000",
        address="1 Main St",
        ride_radius_miles=10,
    )


def sample_vehicle(*, vin: str = "1C4RJFBG5NC123456", owner: str = "c") -> VehicleRecord:
    return VehicleRecord(
        vin=VehicleVin(vin),
        owner_id=CustomerId(owner),
        year=2025,
        make="Jeep",
        model="GC",
        odometer_miles=100,
        current_location=Location(latitude=42.0, longitude=-83.0, description="here"),
    )


def sample_trigger(
    *,
    trigger_id: str = "trig_1",
    vin: str = "1C4RJFBG5NC123456",
    dealer_id: str = "d",
) -> Trigger:
    return Trigger(
        id=TriggerId(trigger_id),
        vehicle_vin=VehicleVin(vin),
        dealer_id=DealerId(dealer_id),
        service_event=ServiceEvent(type="maintenance", summary="oil change"),
        channel_preference="voice",
        offered_slots=(
            OfferedSlot(
                id=SlotId("slot_a"),
                starts_at=datetime(2026, 5, 12, 13, 30, tzinfo=UTC),
                display="Tuesday 8:30 AM",
            ),
        ),
        created_at=datetime(2026, 5, 10, tzinfo=UTC),
    )


def sample_case(*, case_id: str = "case_1") -> Case:
    return Case(
        case_id=CaseId(case_id),
        trigger_id=TriggerId("trig_1"),
        correlation_id="corr_test",
        customer=sample_customer(),
        dealer=sample_dealer(),
        vehicle=sample_vehicle(),
        service_event=ServiceEvent(type="maintenance", summary="oil change"),
        offered_slots=(
            OfferedSlot(
                id=SlotId("slot_a"),
                starts_at=datetime(2026, 5, 12, 13, 30, tzinfo=UTC),
                display="Tuesday 8:30 AM",
            ),
        ),
        state=CaseState.CREATED,
        attempt_count=0,
        created_at=datetime(2026, 5, 10, tzinfo=UTC),
    )


@final
class FakeBookedCallSession:
    """Test fake — returns a deterministic ``booked`` outcome.

    Stand-in for ``CallSession`` in tests that exercise the case
    manager / route layer without placing real ElevenLabs calls.
    Returns the canonical ``slot_a`` booking that ``sample_trigger``
    offers, so manager-state assertions are stable.
    """

    async def place(self, case: Case) -> CallOutcome:
        booked = case.offered_slots[0].id if case.offered_slots else SlotId("slot_a")
        now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
        return CallOutcome(
            result="answered",
            business_outcome="booked",
            booked_slot_id=booked,
            elevenlabs_conversation_id="fake_conv_test",
            started_at=now,
            ended_at=now,
            duration_seconds=0.0,
            transcript="(fake test outcome)",
        )


__all__ = [
    "FakeBookedCallSession",
    "FixedClock",
    "sample_case",
    "sample_customer",
    "sample_dealer",
    "sample_trigger",
    "sample_vehicle",
]
