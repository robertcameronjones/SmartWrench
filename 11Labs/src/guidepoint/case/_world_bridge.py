"""Map world / port events into ``CaseSignal`` values.

Small adapter helpers sit between external bindings (timers,
geofence) and the ``CaseDriver.on_signal`` entry point. Keeping the
mapping here (rather than inside each binding) means:

- Timer and geofence implementations stay dumb (fire a name / emit an
  enum).
- The driver still only ever sees ``CaseSignal`` instances.
- Tests can assert the mapping without standing up a full driver.
"""

from __future__ import annotations

from datetime import datetime

from guidepoint.case._models import CaseId
from guidepoint.case._ports import GeofenceEvent
from guidepoint.case._reducer import (
    TIMER_END_OF_DAY,
    TIMER_FINAL_REMINDER,
    TIMER_INITIAL_REMINDER,
)
from guidepoint.case._signals import (
    CaseSignal,
    EndOfBusinessDayReached,
    FinalReminderDue,
    InitialReminderDue,
    TimerFired,
    VehicleEnteredDealer,
    VehicleExitedDealer,
)
from guidepoint.clock import UtcDatetime


def timer_name_to_case_signal(
    *,
    case_id: CaseId,
    name: str,
    timestamp: datetime,
) -> CaseSignal:
    """Translate a per-case timer name into the ``CaseSignal`` the reducer expects."""

    ts: UtcDatetime = timestamp  # validated upstream via Clock
    if name == TIMER_INITIAL_REMINDER:
        return InitialReminderDue(timestamp=ts, case_id=case_id)
    if name == TIMER_FINAL_REMINDER:
        return FinalReminderDue(timestamp=ts, case_id=case_id)
    if name == TIMER_END_OF_DAY:
        return EndOfBusinessDayReached(timestamp=ts)
    return TimerFired(timestamp=ts, case_id=case_id, name=name)


def geofence_event_to_case_signal(
    *,
    event: GeofenceEvent,
    timestamp: datetime,
) -> CaseSignal:
    """Translate a ``GeofenceEvent`` into a vehicle-targeted ``CaseSignal``."""

    ts: UtcDatetime = timestamp
    if event.kind == "entered":
        return VehicleEnteredDealer(timestamp=ts, vehicle_vin=event.vehicle_vin)
    return VehicleExitedDealer(timestamp=ts, vehicle_vin=event.vehicle_vin)


__all__ = [
    "geofence_event_to_case_signal",
    "timer_name_to_case_signal",
]
