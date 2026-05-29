"""Tests for world-event → CaseSignal bridge helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from guidepoint.case import (
    CaseId,
    GeofenceEvent,
    geofence_event_to_case_signal,
    timer_name_to_case_signal,
)
from guidepoint.case._reducer import (
    TIMER_END_OF_DAY,
    TIMER_FINAL_REMINDER,
    TIMER_INITIAL_REMINDER,
)
from guidepoint.case._signals import (
    EndOfBusinessDayReached,
    FinalReminderDue,
    InitialReminderDue,
    TimerFired,
    VehicleEnteredDealer,
    VehicleExitedDealer,
)
from guidepoint.master_data import VehicleVin

NOW = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
VIN = VehicleVin("1C4RJFBG5NC123456")


def test_timer_initial_reminder_maps_to_signal() -> None:
    sig = timer_name_to_case_signal(
        case_id=CaseId("c1"), name=TIMER_INITIAL_REMINDER, timestamp=NOW
    )
    assert isinstance(sig, InitialReminderDue)
    assert sig.case_id == CaseId("c1")


def test_timer_final_reminder_maps_to_signal() -> None:
    sig = timer_name_to_case_signal(
        case_id=CaseId("c1"), name=TIMER_FINAL_REMINDER, timestamp=NOW
    )
    assert isinstance(sig, FinalReminderDue)


def test_timer_end_of_day_maps_to_world_signal() -> None:
    sig = timer_name_to_case_signal(
        case_id=CaseId("c1"), name=TIMER_END_OF_DAY, timestamp=NOW
    )
    assert isinstance(sig, EndOfBusinessDayReached)


def test_unknown_timer_maps_to_timer_fired() -> None:
    sig = timer_name_to_case_signal(
        case_id=CaseId("c1"), name="nudge_silence", timestamp=NOW
    )
    assert isinstance(sig, TimerFired)
    assert sig.name == "nudge_silence"


def test_geofence_entered_maps_to_signal() -> None:
    sig = geofence_event_to_case_signal(
        event=GeofenceEvent(vehicle_vin=VIN, kind="entered"),
        timestamp=NOW,
    )
    assert isinstance(sig, VehicleEnteredDealer)
    assert sig.vehicle_vin == VIN


def test_geofence_exited_maps_to_signal() -> None:
    sig = geofence_event_to_case_signal(
        event=GeofenceEvent(vehicle_vin=VIN, kind="exited"),
        timestamp=NOW,
    )
    assert isinstance(sig, VehicleExitedDealer)
