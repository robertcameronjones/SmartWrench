"""Simulator world-control and case-signal route handlers.

Thin glue between the operator UI sliders / buttons and the v2
``CaseDriver``. Keeps ``_routes.py`` from growing further.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Literal, final

import structlog
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from guidepoint.case import (
    BusinessHoursClosed,
    BusinessHoursOpened,
    CaseDriver,
    CaseError,
    CaseId,
    CaseRepository,
    EndOfBusinessDayReached,
    FinalReminderDue,
    InitialReminderDue,
)
from guidepoint.case._ports import GeofenceEvent
from guidepoint.case._world_bridge import geofence_event_to_case_signal
from guidepoint.clock import Clock, UtcDatetime
from guidepoint.master_data import VehicleVin

from simulator._sim_ports import SimulatorGeofencePort

_log = structlog.get_logger(__name__)


async def _drain_case_driver(case_driver: CaseDriver, *, max_iterations: int = 50) -> None:
    """Cooperatively yield until case queues (and IO sub-tasks) are idle."""
    for _ in range(max_iterations):
        await asyncio.sleep(0)
        depths = case_driver.queue_depths()
        if all(d == 0 for d in depths.values()) and case_driver.active_case_count() >= 0:
            # Best-effort: one more tick for PlaceCall IO spawned from the loop.
            await asyncio.sleep(0.01)
            if all(d == 0 for d in case_driver.queue_depths().values()):
                return
    await asyncio.sleep(0.05)

SimCaseSignalType = Literal[
    "initial_reminder_due",
    "final_reminder_due",
    "end_of_business_day_reached",
]


def _frozen_strict() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class WorldBusinessHoursRequest(BaseModel):
    """Toggle the simulator business-hours gate."""

    model_config = _frozen_strict()

    open: bool


class WorldGeofenceRequest(BaseModel):
    """Move the vehicle geofence slider."""

    model_config = _frozen_strict()

    vehicle_vin: str = Field(min_length=1)
    at_dealer: bool


class SimCaseSignalRequest(BaseModel):
    """Fire one lifecycle shortcut from the case control panel."""

    model_config = _frozen_strict()

    signal_type: SimCaseSignalType


class WorldStateResponse(BaseModel):
    """Hydrate the UI sliders on boot / after fire."""

    model_config = _frozen_strict()

    business_hours_open: bool
    at_dealer: bool
    vehicle_vin: str = ""


class QueueHealthResponse(BaseModel):
    """Aggregate queue depths for runtime observability."""

    model_config = _frozen_strict()

    case_driver_queues: dict[str, int]
    case_driver_active_cases: int
    bus_subscriber_depths: dict[str, int]


@final
class SimulatorWorldState:
    """In-memory world gates the UI sliders reflect."""

    def __init__(self) -> None:
        self.business_hours_open: bool = True

    def set_business_hours(self, *, open: bool) -> None:
        self.business_hours_open = open


def build_geofence_forwarder(
    *,
    case_driver: CaseDriver,
    clock: Clock,
) -> Callable[[GeofenceEvent], None]:
    """Return a sync callback suitable for ``GeofencePort.subscribe``."""

    def _forward(event: GeofenceEvent) -> None:
        signal = geofence_event_to_case_signal(
            event=event,
            timestamp=clock.now(),
        )

        async def _dispatch() -> None:
            await case_driver.on_signal(signal)

        _ = asyncio.create_task(_dispatch())

    return _forward


def ensure_geofence_subscription(
    *,
    geofence_port: SimulatorGeofencePort,
    vehicle_vin: VehicleVin,
    on_event: Callable[[GeofenceEvent], None],
    subscribed: set[VehicleVin],
) -> None:
    """Subscribe once per VIN so slider transitions reach the driver."""
    if vehicle_vin in subscribed:
        return
    geofence_port.subscribe(vehicle_vin=vehicle_vin, on_event=on_event)
    subscribed.add(vehicle_vin)


def _build_case_signal(
    *,
    signal_type: SimCaseSignalType,
    case_id: CaseId,
    timestamp: UtcDatetime,
) -> InitialReminderDue | FinalReminderDue | EndOfBusinessDayReached:
    if signal_type == "initial_reminder_due":
        return InitialReminderDue(timestamp=timestamp, case_id=case_id, source="operator")
    if signal_type == "final_reminder_due":
        return FinalReminderDue(timestamp=timestamp, case_id=case_id, source="operator")
    return EndOfBusinessDayReached(timestamp=timestamp, source="operator")


def get_world_state(
    *,
    world: SimulatorWorldState,
    geofence_port: SimulatorGeofencePort,
    vehicle_vin: str,
) -> WorldStateResponse:
    at_dealer = False
    if vehicle_vin:
        at_dealer = geofence_port.is_at_dealer(vehicle_vin=VehicleVin(vehicle_vin))
    return WorldStateResponse(
        business_hours_open=world.business_hours_open,
        at_dealer=at_dealer,
        vehicle_vin=vehicle_vin,
    )


async def put_business_hours(
    *,
    request: Request,
    body: WorldBusinessHoursRequest,
    world: SimulatorWorldState,
    case_driver: CaseDriver,
    clock: Clock,
) -> WorldStateResponse:
    world.set_business_hours(open=body.open)
    signal = (
        BusinessHoursOpened(timestamp=clock.now(), source="operator")
        if body.open
        else BusinessHoursClosed(timestamp=clock.now(), source="operator")
    )
    await case_driver.on_signal(signal)
    geofence: SimulatorGeofencePort = request.app.state.geofence_port
    vin = getattr(request.app.state, "active_vehicle_vin", "")
    return get_world_state(world=world, geofence_port=geofence, vehicle_vin=vin)


def put_geofence(
    *,
    request: Request,
    body: WorldGeofenceRequest,
    geofence_port: SimulatorGeofencePort,
    on_event: Callable[[GeofenceEvent], None],
    subscribed: set[VehicleVin],
) -> WorldStateResponse:
    vin = VehicleVin(body.vehicle_vin)
    ensure_geofence_subscription(
        geofence_port=geofence_port,
        vehicle_vin=vin,
        on_event=on_event,
        subscribed=subscribed,
    )
    geofence_port.set_at_dealer(vehicle_vin=vin, at_dealer=body.at_dealer)
    request.app.state.active_vehicle_vin = body.vehicle_vin
    world: SimulatorWorldState = request.app.state.world_state
    return get_world_state(
        world=world,
        geofence_port=geofence_port,
        vehicle_vin=body.vehicle_vin,
    )


async def post_case_signal(
    *,
    case_id: str,
    body: SimCaseSignalRequest,
    case_driver: CaseDriver,
    clock: Clock,
) -> dict[str, str]:
    signal = _build_case_signal(
        signal_type=body.signal_type,
        case_id=CaseId(case_id),
        timestamp=clock.now(),
    )
    await case_driver.on_signal(signal)
    await _drain_case_driver(case_driver)
    _log.info(
        "simulator.case_signal.sent",
        case_id=case_id,
        signal_type=body.signal_type,
    )
    return {"status": "accepted", "signal_type": body.signal_type}


def get_queue_health(
    *,
    request: Request,
    case_driver: CaseDriver,
) -> QueueHealthResponse:
    bus = request.app.state.event_bus
    depths: dict[str, int] = {}
    if hasattr(bus, "subscriber_depths"):
        raw = bus.subscriber_depths()
        depths = {f"subscriber_{idx}": depth for idx, (depth, _max) in enumerate(raw)}
    return QueueHealthResponse(
        case_driver_queues=dict(case_driver.queue_depths()),
        case_driver_active_cases=case_driver.active_case_count(),
        bus_subscriber_depths=depths,
    )


def make_get_world_state(
    *,
    world: SimulatorWorldState,
    geofence_port: SimulatorGeofencePort,
) -> Callable[[Request], WorldStateResponse]:
    def handler(request: Request) -> WorldStateResponse:
        vin = getattr(request.app.state, "active_vehicle_vin", "")
        return get_world_state(world=world, geofence_port=geofence_port, vehicle_vin=vin)

    return handler


def make_put_business_hours(
    *,
    world: SimulatorWorldState,
    case_driver: CaseDriver,
    clock: Clock,
) -> Callable[..., Coroutine[Any, Any, WorldStateResponse]]:
    async def handler(request: Request, body: WorldBusinessHoursRequest) -> WorldStateResponse:
        return await put_business_hours(
            request=request,
            body=body,
            world=world,
            case_driver=case_driver,
            clock=clock,
        )

    return handler


def make_put_geofence(
    *,
    geofence_port: SimulatorGeofencePort,
    on_event: Callable[[GeofenceEvent], None],
    subscribed: set[VehicleVin],
) -> Callable[..., Coroutine[Any, Any, WorldStateResponse]]:
    async def handler(request: Request, body: WorldGeofenceRequest) -> WorldStateResponse:
        return put_geofence(
            request=request,
            body=body,
            geofence_port=geofence_port,
            on_event=on_event,
            subscribed=subscribed,
        )

    return handler


def make_post_case_signal(
    *,
    case_repo: CaseRepository,
    case_driver: CaseDriver,
    clock: Clock,
) -> Callable[..., Coroutine[Any, Any, dict[str, str]]]:
    async def handler(case_id: str, body: SimCaseSignalRequest) -> dict[str, str]:
        try:
            case_repo.get(CaseId(case_id))
        except CaseError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return await post_case_signal(
            case_id=case_id,
            body=body,
            case_driver=case_driver,
            clock=clock,
        )

    return handler


def make_get_queue_health(
    *,
    case_driver: CaseDriver,
) -> Callable[[Request], QueueHealthResponse]:
    def handler(request: Request) -> QueueHealthResponse:
        return get_queue_health(request=request, case_driver=case_driver)

    return handler


__all__ = [
    "QueueHealthResponse",
    "SimCaseSignalRequest",
    "SimulatorWorldState",
    "WorldBusinessHoursRequest",
    "WorldGeofenceRequest",
    "WorldStateResponse",
    "build_geofence_forwarder",
    "ensure_geofence_subscription",
    "make_get_queue_health",
    "make_get_world_state",
    "make_post_case_signal",
    "make_put_business_hours",
    "make_put_geofence",
]
