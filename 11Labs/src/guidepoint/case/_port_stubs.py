"""Production port stubs — fail loudly until real bindings land.

``build_app`` (and future production entrypoints) can wire these
when ``DEALER_PORT=real`` / ``GEOFENCE_PORT=real`` env vars are set,
so the swap point is explicit and greppable. The simulator always uses
``simulator._sim_ports`` instead.
"""

from __future__ import annotations

from typing import final

from guidepoint.case._models import Case, OfferedSlot, SlotId
from guidepoint.case._ports import GeofenceSubscription
from guidepoint.master_data import VehicleVin


@final
class RealDealerSlotPort:
    """Placeholder for the partner dealer-scheduling tool."""

    async def list_slots(self, *, case: Case) -> tuple[OfferedSlot, ...]:
        raise NotImplementedError(
            "RealDealerSlotPort.list_slots is not wired — swap in the "
            "partner dealer-scheduling tool behind DealerSlotPort"
        )

    async def confirm_slot(self, *, case: Case, slot_id: SlotId) -> bool:
        raise NotImplementedError(
            "RealDealerSlotPort.confirm_slot is not wired — swap in the "
            "partner dealer-scheduling tool behind DealerSlotPort"
        )


@final
class _NoopGeofenceSubscription:
    def cancel(self) -> None:
        return


@final
class RealGeofencePort:
    """Placeholder for production telematics."""

    def subscribe(
        self,
        *,
        vehicle_vin: VehicleVin,
        on_event: object,
    ) -> GeofenceSubscription:
        raise NotImplementedError(
            "RealGeofencePort.subscribe is not wired — swap in telematics "
            "behind GeofencePort"
        )
        return _NoopGeofenceSubscription()  # pragma: no cover


__all__ = [
    "RealDealerSlotPort",
    "RealGeofencePort",
]
