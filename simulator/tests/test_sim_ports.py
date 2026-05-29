"""Tests for simulator DealerSlotPort + GeofencePort bindings."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from guidepoint.case import (
    GeofenceEvent,
    OfferedSlot,
    ServiceEvent,
    SlotId,
    Trigger,
    TriggerId,
    create_case_from_trigger,
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
from simulator._sim_ports import SimulatorDealerSlotPort, SimulatorGeofencePort


class _FixedClock:
    def now(self):
        return datetime(2026, 5, 10, 12, 0, tzinfo=UTC)


def _sample_case():
    trig = Trigger(
        id=TriggerId("trig_sim_ports"),
        vehicle_vin=VehicleVin("1C4RJFBG5NC123456"),
        dealer_id=DealerId("d"),
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
    return create_case_from_trigger(
        trigger=trig,
        customer=CustomerRecord(
            id=CustomerId("c"), first_name="Robert", last_name="Jones", phone="+15555550100"
        ),
        dealer=DealerRecord(
            id=DealerId("d"),
            name="Village Jeep",
            phone="5559990000",
            address="1 Main St",
            ride_radius_miles=10,
        ),
        vehicle=VehicleRecord(
            vin=VehicleVin("1C4RJFBG5NC123456"),
            owner_id=CustomerId("c"),
            year=2025,
            make="Jeep",
            model="GC",
            odometer_miles=100,
            current_location=Location(latitude=42.0, longitude=-83.0, description="x"),
        ),
        clock=_FixedClock(),
    )

VIN = VehicleVin("1C4RJFBG5NC123456")
SLOT_B = OfferedSlot(
    id=SlotId("slot_b"),
    starts_at=datetime(2026, 5, 16, 10, 0, tzinfo=UTC),
    display="Thursday 10 AM",
)


@pytest.mark.asyncio
async def test_dealer_port_lists_from_provider() -> None:
    case = _sample_case()
    port = SimulatorDealerSlotPort(slots_for_case=lambda _c: (SLOT_B,))
    slots = await port.list_slots(case=case)
    assert slots == (SLOT_B,)


@pytest.mark.asyncio
async def test_dealer_port_confirms_by_default() -> None:
    case = _sample_case()
    port = SimulatorDealerSlotPort()
    ok = await port.confirm_slot(case=case, slot_id=case.offered_slots[0].id)
    assert ok is True


@pytest.mark.asyncio
async def test_dealer_port_rejects_configured_slots() -> None:
    case = _sample_case()
    slot_id = case.offered_slots[0].id
    port = SimulatorDealerSlotPort(reject_slot_ids=frozenset({slot_id}))
    ok = await port.confirm_slot(case=case, slot_id=slot_id)
    assert ok is False


def test_geofence_edge_triggered_enter_and_exit() -> None:
    port = SimulatorGeofencePort()
    events: list[GeofenceEvent] = []
    port.subscribe(vehicle_vin=VIN, on_event=events.append)

    port.set_at_dealer(vehicle_vin=VIN, at_dealer=True)
    assert len(events) == 1
    assert events[0].kind == "entered"

    # No duplicate while still inside.
    port.set_at_dealer(vehicle_vin=VIN, at_dealer=True)
    assert len(events) == 1

    port.set_at_dealer(vehicle_vin=VIN, at_dealer=False)
    assert len(events) == 2
    assert events[1].kind == "exited"
    assert port.is_at_dealer(vehicle_vin=VIN) is False


def test_geofence_subscription_cancel_stops_events() -> None:
    port = SimulatorGeofencePort()
    events: list[GeofenceEvent] = []
    sub = port.subscribe(vehicle_vin=VIN, on_event=events.append)
    sub.cancel()
    port.set_at_dealer(vehicle_vin=VIN, at_dealer=True)
    assert events == []
