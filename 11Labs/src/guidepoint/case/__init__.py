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

from guidepoint.case._call_session import (
    LiveCallSessionSettings,
    build_live_call_session,
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
from guidepoint.case._post_call import ingest_post_call_report
from guidepoint.case._repository import (
    CaseRepository,
    JsonCasePaths,
    build_json_case_repository,
)
from guidepoint.case._trigger_source import (
    JsonTriggerPaths,
    TriggerSource,
    build_json_trigger_source,
)

__all__ = [
    "BusinessOutcome",
    "CallAttempt",
    "CallOutcome",
    "CallResult",
    "CallState",
    "Case",
    "CaseError",
    "CaseEvent",
    "CaseId",
    "CaseManager",
    "CaseNotFoundError",
    "CaseRepository",
    "CaseState",
    "Channel",
    "EventLevel",
    "EventSource",
    "JsonCasePaths",
    "JsonTriggerPaths",
    "LiveCallSessionSettings",
    "OfferedSlot",
    "PostCallReport",
    "RetryPolicy",
    "ServiceEvent",
    "ServiceReasonType",
    "SlotId",
    "TranscriptTurn",
    "Trigger",
    "TriggerForeignKeyError",
    "TriggerId",
    "TriggerNotFoundError",
    "TriggerSource",
    "TriggerSourceKind",
    "TriggerStatus",
    "build_default_case_manager",
    "build_json_case_repository",
    "build_json_trigger_source",
    "build_live_call_session",
    "create_case_from_trigger",
    "ingest_post_call_report",
]
