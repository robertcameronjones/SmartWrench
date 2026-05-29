"""Case lifecycle — public surface.

The case module owns the business state machine for one customer
interaction: trigger → case → call attempt(s) → terminal outcome. It is
SPOT to ElevenLabs (every call is initiated through ``CaseManager``)
and SPOT to the case audit trail (every event lives on a ``Case``).

``CallSession`` is intentionally **not** exported. It is an
implementation detail of the manager. Outside callers can place a call
only by asking the ``CaseManager`` to fire a trigger.

See ``docs/adr/0006-case-lifecycle-and-normalized-fixtures.md`` for the
architectural rationale.

Typical use::

    from pathlib import Path
    from guidepoint.case import build_default_case_manager, build_json_case_repository, ...
    from guidepoint.master_data import build_json_master_data_repository, JsonFilePaths
    ...
    manager = build_default_case_manager(...)
    case = await manager.fire(trigger)
"""

from guidepoint.case._actions import (
    CallStage,
    CancelTimer,
    CaseAction,
    PlaceCall,
    RecordEvent,
    RequestDealerConfirmation,
    RequestDealerSlots,
    ScheduleTimer,
)
from guidepoint.case._call_session import (
    LiveCallSessionSettings,
    build_live_call_session,
    build_voice_call_manager,
)
from guidepoint.case._driver import (
    DEFAULT_CASE_QUEUE_SIZE,
    CaseDriver,
)
from guidepoint.case._factory import create_case_from_trigger
from guidepoint.case._manager import (
    CaseManager,
    RetryPolicy,
    build_default_case_manager,
)
from guidepoint.case._models import (
    BusinessOutcome,
    CallAttempt,
    CallOutcome,
    CallResult,
    CallState,
    Case,
    CaseError,
    CaseEvent,
    CaseId,
    CaseNotFoundError,
    CaseState,
    Channel,
    EventLevel,
    EventSource,
    OfferedSlot,
    lookup_slot_display,
    PostCallReport,
    ServiceEvent,
    ServiceReasonType,
    SlotId,
    TranscriptTurn,
    Trigger,
    TriggerForeignKeyError,
    TriggerId,
    TriggerNotFoundError,
    TriggerSourceKind,
    TriggerStatus,
)
from guidepoint.case._port_stubs import RealDealerSlotPort, RealGeofencePort
from guidepoint.case._ports import (
    CallManager,
    DealerSlotPort,
    GeofenceEvent,
    GeofenceEventKind,
    GeofencePort,
    GeofenceSubscription,
    TimerService,
)
from guidepoint.case._timer_service import InMemoryTimerService
from guidepoint.case._post_call import ingest_post_call_report
from guidepoint.case._reducer import (
    FINAL_REMINDER_LEAD,
    INITIAL_REMINDER_LEAD,
    MAX_CALL_ATTEMPTS,
    MAX_RESCHEDULES,
    TIMER_END_OF_DAY,
    TIMER_FINAL_REMINDER,
    TIMER_INITIAL_REMINDER,
    CaseDecision,
    CasePatch,
    decide_next_case_state,
)
from guidepoint.case._repository import (
    CaseRepository,
    JsonCasePaths,
    build_json_case_repository,
)
from guidepoint.case._signals import (
    BusinessHoursClosed,
    BusinessHoursOpened,
    CallEnded,
    CaseSignal,
    CustomerOptedIn,
    CustomerOptedOut,
    DealerConfirmed,
    DealerRejected,
    DealerSlotsListed,
    EndOfBusinessDayReached,
    FinalReminderDue,
    InitialReminderDue,
    TimerFired,
    VehicleEnteredDealer,
    VehicleExitedDealer,
    is_case_targeted,
    is_customer_targeted,
    is_vehicle_targeted,
    is_world_signal,
)
from guidepoint.case._world_bridge import (
    geofence_event_to_case_signal,
    timer_name_to_case_signal,
)
from guidepoint.case._trigger_source import (
    JsonTriggerPaths,
    TriggerSource,
    build_json_trigger_source,
)

__all__ = [
    "DEFAULT_CASE_QUEUE_SIZE",
    "FINAL_REMINDER_LEAD",
    "INITIAL_REMINDER_LEAD",
    "MAX_CALL_ATTEMPTS",
    "MAX_RESCHEDULES",
    "TIMER_END_OF_DAY",
    "TIMER_FINAL_REMINDER",
    "TIMER_INITIAL_REMINDER",
    "BusinessHoursClosed",
    "BusinessHoursOpened",
    "BusinessOutcome",
    "CallAttempt",
    "CallEnded",
    "CallManager",
    "CallOutcome",
    "CallResult",
    "CallStage",
    "CallState",
    "CancelTimer",
    "Case",
    "CaseAction",
    "CaseDecision",
    "CaseDriver",
    "CaseError",
    "CaseEvent",
    "CaseId",
    "CaseManager",
    "CaseNotFoundError",
    "CasePatch",
    "CaseRepository",
    "CaseSignal",
    "CaseState",
    "Channel",
    "CustomerOptedIn",
    "CustomerOptedOut",
    "DealerConfirmed",
    "DealerRejected",
    "DealerSlotPort",
    "DealerSlotsListed",
    "EndOfBusinessDayReached",
    "FinalReminderDue",
    "GeofenceEvent",
    "GeofenceEventKind",
    "GeofencePort",
    "GeofenceSubscription",
    "InMemoryTimerService",
    "InitialReminderDue",
    "EventSource",
    "JsonCasePaths",
    "JsonTriggerPaths",
    "LiveCallSessionSettings",
    "OfferedSlot",
    "lookup_slot_display",
    "PlaceCall",
    "PostCallReport",
    "RealDealerSlotPort",
    "RealGeofencePort",
    "RecordEvent",
    "RequestDealerConfirmation",
    "RequestDealerSlots",
    "RetryPolicy",
    "ScheduleTimer",
    "ServiceEvent",
    "ServiceReasonType",
    "SlotId",
    "TimerFired",
    "TimerService",
    "TranscriptTurn",
    "Trigger",
    "TriggerForeignKeyError",
    "TriggerId",
    "TriggerNotFoundError",
    "TriggerSource",
    "TriggerSourceKind",
    "TriggerStatus",
    "VehicleEnteredDealer",
    "VehicleExitedDealer",
    "timer_name_to_case_signal",
    "build_default_case_manager",
    "build_json_case_repository",
    "build_json_trigger_source",
    "build_live_call_session",
    "build_voice_call_manager",
    "create_case_from_trigger",
    "decide_next_case_state",
    "geofence_event_to_case_signal",
    "ingest_post_call_report",
    "is_case_targeted",
    "is_customer_targeted",
    "is_vehicle_targeted",
    "is_world_signal",
]
