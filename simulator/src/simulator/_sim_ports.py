"""Simulator bindings for ``DealerSlotPort`` and ``GeofencePort``.

These are the **only** simulator modules that implement the case
driver's world-facing Protocols. Production swaps in real bindings
via env var at ``build_app`` time; the driver never changes.

Dealer
~~~~~~
``SimulatorDealerSlotPort`` reads slot lists from a caller-supplied
provider (typically the per-user ``SlotsRepository.list()``) and
always confirms unless the slot id is in an optional reject set
(useful for testing the dealer-rejection → reschedule path).

Geofence
~~~~~~~~
``SimulatorGeofencePort`` backs the "at dealer / not at dealer"
slider. Call ``set_at_dealer(vin, True/False)`` from a route handler;
subscribed handlers receive ``GeofenceEvent`` values that
``world_bridge.geofence_event_to_case_signal`` translates into
``CaseSignal`` instances for the driver.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import final

import structlog

from guidepoint.case import Case, OfferedSlot, SlotId
from guidepoint.case._ports import GeofenceEvent, GeofenceSubscription
from guidepoint.master_data import VehicleVin

_log = structlog.get_logger(__name__)

SlotsProvider = Callable[[Case], tuple[OfferedSlot, ...]]


@final
class SimulatorDealerSlotPort:
    """Canned dealer port — list from provider, confirm by default."""

    def __init__(
        self,
        *,
        slots_for_case: SlotsProvider | None = None,
        reject_slot_ids: frozenset[SlotId] = frozenset(),
    ) -> None:
        self._slots_for_case = slots_for_case or (lambda case: case.offered_slots)
        self._reject_slot_ids = reject_slot_ids

    async def list_slots(self, *, case: Case) -> tuple[OfferedSlot, ...]:
        slots = self._slots_for_case(case)
        _log.info(
            "simulator.dealer_port.list_slots",
            case_id=case.case_id,
            dealer_id=case.dealer.id,
            count=len(slots),
        )
        return slots

    async def confirm_slot(self, *, case: Case, slot_id: SlotId) -> bool:
        ok = slot_id not in self._reject_slot_ids
        _log.info(
            "simulator.dealer_port.confirm_slot",
            case_id=case.case_id,
            slot_id=slot_id,
            confirmed=ok,
        )
        return ok


@final
class _GeofenceSubscription:
    """One vehicle's callback list entry — cancel removes the handler."""

    def __init__(
        self,
        *,
        port: SimulatorGeofencePort,
        vehicle_vin: VehicleVin,
        on_event: Callable[[GeofenceEvent], None],
    ) -> None:
        self._port = port
        self._vehicle_vin = vehicle_vin
        self._on_event = on_event
        self._active = True

    def cancel(self) -> None:
        if not self._active:
            return
        self._active = False
        self._port._unsubscribe(self._vehicle_vin, self._on_event)  # noqa: SLF001


@final
class SimulatorGeofencePort:
    """In-memory geofence for the operator slider.

    Tracks whether each ``vehicle_vin`` is currently inside the dealer
    geofence. Edge-triggered: ``set_at_dealer(True)`` fires ``entered``
    only on a false→true transition; ``set_at_dealer(False)`` fires
    ``exited`` only on true→false.
    """

    def __init__(self) -> None:
        self._at_dealer: dict[VehicleVin, bool] = {}
        self._handlers: dict[VehicleVin, list[Callable[[GeofenceEvent], None]]] = {}

    def subscribe(
        self,
        *,
        vehicle_vin: VehicleVin,
        on_event: Callable[[GeofenceEvent], None],
    ) -> GeofenceSubscription:
        self._handlers.setdefault(vehicle_vin, []).append(on_event)
        _log.debug("simulator.geofence.subscribed", vehicle_vin=vehicle_vin)
        return _GeofenceSubscription(
            port=self, vehicle_vin=vehicle_vin, on_event=on_event
        )

    def set_at_dealer(self, *, vehicle_vin: VehicleVin, at_dealer: bool) -> None:
        """Simulator slider entry point."""

        prev = self._at_dealer.get(vehicle_vin, False)
        if prev == at_dealer:
            _log.debug(
                "simulator.geofence.no_transition",
                vehicle_vin=vehicle_vin,
                at_dealer=at_dealer,
            )
            return
        self._at_dealer[vehicle_vin] = at_dealer
        kind = "entered" if at_dealer else "exited"
        event = GeofenceEvent(vehicle_vin=vehicle_vin, kind=kind)
        _log.info(
            "simulator.geofence.transition",
            vehicle_vin=vehicle_vin,
            kind=kind,
        )
        for handler in list(self._handlers.get(vehicle_vin, ())):
            handler(event)

    def is_at_dealer(self, *, vehicle_vin: VehicleVin) -> bool:
        """Read the current slider position (for UI hydration)."""

        return self._at_dealer.get(vehicle_vin, False)

    def _unsubscribe(
        self,
        vehicle_vin: VehicleVin,
        on_event: Callable[[GeofenceEvent], None],
    ) -> None:
        handlers = self._handlers.get(vehicle_vin)
        if not handlers:
            return
        try:
            handlers.remove(on_event)
        except ValueError:
            return
        if not handlers:
            self._handlers.pop(vehicle_vin, None)


__all__ = [
    "SimulatorDealerSlotPort",
    "SimulatorGeofencePort",
    "SlotsProvider",
]
