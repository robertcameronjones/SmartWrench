"""Tests for production port stubs."""

from __future__ import annotations

import pytest

from guidepoint.case import RealDealerSlotPort, RealGeofencePort
from guidepoint.master_data import VehicleVin

from tests.case._helpers import sample_case


@pytest.mark.asyncio
async def test_real_dealer_port_list_raises() -> None:
    port = RealDealerSlotPort()
    with pytest.raises(NotImplementedError, match="list_slots"):
        await port.list_slots(case=sample_case())


@pytest.mark.asyncio
async def test_real_dealer_port_confirm_raises() -> None:
    port = RealDealerSlotPort()
    case = sample_case()
    with pytest.raises(NotImplementedError, match="confirm_slot"):
        await port.confirm_slot(case=case, slot_id=case.offered_slots[0].id)


def test_real_geofence_port_subscribe_raises() -> None:
    port = RealGeofencePort()
    with pytest.raises(NotImplementedError, match="subscribe"):
        port.subscribe(
            vehicle_vin=VehicleVin("1C4RJFBG5NC123456"),
            on_event=lambda _e: None,
        )
