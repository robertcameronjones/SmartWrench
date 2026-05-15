"""Boundary models for the case lifecycle.

A **Trigger** is the stimulus: "go call this customer about that vehicle
for this reason." Triggers live in the cloud DB in production (a
monitor task polls them) or in JSON fixtures in the simulator.

A **Case** is what exists once a trigger has fired. It carries an
immutable snapshot of the master data as it was at fire time, walks a
state machine driven by ``CaseManager``, accumulates one or more
``CallAttempt`` records, and ends in a terminal outcome.

A **CallAttempt** records what one phone call to ElevenLabs produced.
A **CallOutcome** is the small struct returned by ``CallSession`` to
``CaseManager`` — it's the only thing the case-state machine learns
about a call.

A **CaseEvent** is one line in the case's audit log. The Case Manager
appends them as the lifecycle unfolds. Every external interaction
(initiated call, agent message, tool call, hangup) lands here too,
mirrored from ElevenLabs by ``CallSession``.

Per ADR 0006 the snapshot fields on ``Case`` are full record copies
rather than foreign-key references. The case is an audit object: pulling
it up six months later must show exactly what Kate was told, even if
the underlying customer record has since been edited.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, NewType, final

from pydantic import BaseModel, ConfigDict, Field

from guidepoint.master_data import (
    CustomerRecord,
    DealerId,
    DealerRecord,
    VehicleRecord,
    VehicleVin,
)

# Phantom-typed ids for case-domain entities.
CaseId = NewType("CaseId", str)
TriggerId = NewType("TriggerId", str)
SlotId = NewType("SlotId", str)

ServiceReasonType = Literal["dtc", "recall", "maintenance"]
Channel = Literal["voice", "sms"]
TriggerSourceKind = Literal["telematics", "operator", "batch"]
TriggerStatus = Literal["pending", "fired", "failed", "cancelled"]


def _frozen_strict() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Service event + offered slots (carried on Trigger, snapshotted on Case)
# ---------------------------------------------------------------------------


class ServiceEvent(BaseModel):
    """The reason for the call.

    Two text fields, two audiences:

    - ``summary`` is **for the customer**. A short, plain-English label
      Kate can drop into a sentence ("oil change", "transmission
      recall"). No jargon, no codes.
    - ``narrative`` is **for Kate** — internal context so she
      understands *why* she's calling. Kate uses it to answer questions
      naturally and route appropriately, but **does not read it to the
      customer and does not troubleshoot from it**. Guardrails in the
      system prompt enforce this; ``narrative`` exists only to keep
      Kate informed.
    """

    model_config = _frozen_strict()

    type: ServiceReasonType
    summary: str = Field(min_length=1)
    narrative: str = ""


class OfferedSlot(BaseModel):
    """One bookable appointment slot offered to the customer in this call.

    Slots are pre-fetched into the trigger so Kate can read them without
    round-tripping to the dealer DMS mid-conversation.
    """

    model_config = _frozen_strict()

    id: SlotId
    starts_at: datetime
    display: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


class Trigger(BaseModel):
    """A stimulus that, when fired, creates a Case.

    In production these rows live in the cloud DB and a monitor task
    polls them. In simulator mode they live as JSON fixtures the
    operator can fire by hand. Either way they reach ``CaseManager``
    through the same ``TriggerSource`` Protocol.

    Triggers do **not** carry a phone number. The number to dial is
    always ``Customer.phone``, resolved through the vehicle → owner
    chain at fire time. One customer, one phone, one source of truth
    (per the SPOT discussion of 2026-05-10).
    """

    model_config = _frozen_strict()

    id: TriggerId
    vehicle_vin: VehicleVin
    dealer_id: DealerId
    service_event: ServiceEvent
    channel_preference: Channel
    offered_slots: tuple[OfferedSlot, ...] = ()
    source: TriggerSourceKind = "operator"
    status: TriggerStatus = "pending"
    created_at: datetime
    fired_at: datetime | None = None
    error_detail: str = ""


# ---------------------------------------------------------------------------
# Case lifecycle states
# ---------------------------------------------------------------------------


class CaseState(StrEnum):
    """Where a case is in its business lifecycle.

    Five non-terminal states (top of enum) and four terminal (bottom).
    Only ``CaseManager`` writes to ``Case.state``.
    """

    CREATED = "created"
    READY_TO_CALL = "ready_to_call"
    CALLING = "calling"
    BETWEEN_ATTEMPTS = "between_attempts"
    BOOKED = "booked"
    DECLINED = "declined"
    UNREACHABLE = "unreachable"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """``True`` when the case will not transition further."""
        return self in _TERMINAL_CASE_STATES


_TERMINAL_CASE_STATES: frozenset[CaseState] = frozenset(
    {
        CaseState.BOOKED,
        CaseState.DECLINED,
        CaseState.UNREACHABLE,
        CaseState.ESCALATED,
        CaseState.CANCELLED,
    }
)


class CallState(StrEnum):
    """Live state of one in-flight call attempt.

    Read-only mirror of what ElevenLabs is doing — we observe it, we
    do not drive it. Updated by ``CallSession`` while a call is active.
    """

    DIALING = "dialing"
    RINGING = "ringing"
    CONNECTED = "connected"
    IN_CONVERSATION = "in_conversation"
    ENDED = "ended"


# ---------------------------------------------------------------------------
# Call outcome (reported back to CaseManager when an attempt ends)
# ---------------------------------------------------------------------------

CallResult = Literal["answered", "no_answer", "busy", "connection_failed", "error"]
BusinessOutcome = Literal["booked", "declined", "transferred", "inconclusive"]


class CallOutcome(BaseModel):
    """What one phone call attempt produced.

    Returned by ``CallSession.place`` to ``CaseManager``. The case-state
    machine reads ``result`` and ``business_outcome`` to decide the
    next state. Everything else is for the audit trail.
    """

    model_config = _frozen_strict()

    result: CallResult
    business_outcome: BusinessOutcome | None = None
    booked_slot_id: SlotId | None = None
    elevenlabs_conversation_id: str = ""
    started_at: datetime
    ended_at: datetime
    duration_seconds: float = Field(ge=0.0)
    transcript: str = ""
    recording_url: str = ""
    error_detail: str = ""


# ---------------------------------------------------------------------------
# Per-attempt record (one entry on Case.call_attempts)
# ---------------------------------------------------------------------------


class CallAttempt(BaseModel):
    """One row on ``Case.call_attempts`` describing one call placed."""

    model_config = _frozen_strict()

    attempt_number: int = Field(ge=1)
    outcome: CallOutcome


# ---------------------------------------------------------------------------
# Post-call report (cleanup-after-the-party payload)
# ---------------------------------------------------------------------------

PostCallStatus = Literal["done", "failed"]
TranscriptRole = Literal["agent", "user"]


class TranscriptTurn(BaseModel):
    """One side of one exchange in the call.

    Mirrors the per-turn shape inside ElevenLabs's
    ``post_call_transcription`` webhook payload: who spoke, what they
    said, and when it happened relative to call start. We do not see
    these turns live (per ADR 0006 we use the native Twilio
    integration); we receive them in bulk after the call ends.
    """

    model_config = _frozen_strict()

    role: TranscriptRole
    message: str = Field(min_length=1)
    time_in_call_seconds: float = Field(ge=0.0)


class PostCallReport(BaseModel):
    """What ElevenLabs sends after the conversation ends.

    Boundary model for the ``post_call_transcription`` webhook,
    narrowed to the fields ``CallSession`` actually needs to construct
    a ``CallOutcome``. The stub synthesizes one of these from its
    canned script; the future ``_LiveCallSession`` constructs one from
    the verified webhook body. Either way, the cleanup path through
    ``case._post_call.ingest_post_call_report`` is identical.

    ``business_outcome`` and ``booked_slot_id`` are populated by the
    inference step on the live side (we read them out of the
    conversation analysis or our own tool-call audit log). The stub
    fills them deterministically.
    """

    model_config = _frozen_strict()

    elevenlabs_conversation_id: str = Field(min_length=1)
    status: PostCallStatus
    started_at: datetime
    ended_at: datetime
    duration_seconds: float = Field(ge=0.0)
    transcript: tuple[TranscriptTurn, ...] = ()
    business_outcome: BusinessOutcome = "inconclusive"
    booked_slot_id: SlotId | None = None
    recording_url: str = ""
    error_detail: str = ""


# ---------------------------------------------------------------------------
# Case event log entry
# ---------------------------------------------------------------------------

EventLevel = Literal["info", "warn", "error", "debug"]
EventSource = Literal["system", "elevenlabs", "tool_webhook", "operator"]


class CaseEvent(BaseModel):
    """One line in a case's audit log.

    Persisted on the ``Case`` and published to the in-process
    ``EventBus`` for any live UI to render. Carries our correlation_id
    on every line so log scraping after the fact is cheap.
    """

    model_config = _frozen_strict()

    event_id: str = Field(min_length=1)
    case_id: CaseId
    correlation_id: str = Field(min_length=1)
    attempt_number: int | None = None
    timestamp: datetime
    source: EventSource = "system"
    level: EventLevel = "info"
    event: str = Field(min_length=1)
    detail: str = ""


# ---------------------------------------------------------------------------
# Case
# ---------------------------------------------------------------------------


class Case(BaseModel):
    """The durable record of one customer interaction.

    Created when a trigger fires; lives until it reaches a terminal
    ``state``. Carries an immutable snapshot of the master data captured
    at fire time so the case is replayable and auditable independent of
    later edits to the underlying records.

    ``to_variables()`` flattens the snapshot into the string dict
    ElevenLabs expects as ``dynamic_variables``. The system prompt's
    ``{{placeholders}}`` resolve from this dict — see
    ``guidepoint.agent._variable_audit`` for the cross-check.
    """

    model_config = _frozen_strict()

    case_id: CaseId
    trigger_id: TriggerId
    correlation_id: str = Field(min_length=1)

    # Snapshotted master data (frozen copies, not FK references).
    customer: CustomerRecord
    dealer: DealerRecord
    vehicle: VehicleRecord
    service_event: ServiceEvent
    offered_slots: tuple[OfferedSlot, ...] = ()

    # State machine.
    state: CaseState = CaseState.CREATED
    attempt_count: int = Field(ge=0, default=0)
    next_attempt_at: datetime | None = None

    # History.
    call_attempts: tuple[CallAttempt, ...] = ()
    events: tuple[CaseEvent, ...] = ()

    # Outcome (only set in terminal states).
    outcome_detail: str = ""
    booked_slot_id: SlotId | None = None

    # Timestamps.
    created_at: datetime
    closed_at: datetime | None = None

    def to_variables(self) -> dict[str, str]:
        """Flatten the case snapshot into the dict ElevenLabs receives.

        Keys here MUST match the ``{{placeholder}}`` names inside
        ``config/system-prompt.md``. The variable audit module reads
        ``Case.variable_keys()`` to enforce that contract.
        """
        return {
            "channel": "voice",
            "case_id": self.case_id,
            "trigger_id": self.trigger_id,
            "customer_id": self.customer.id,
            "customer_first_name": self.customer.first_name,
            "customer_last_name": self.customer.last_name,
            "customer_full_name": self.customer.full_name,
            "customer_phone": self.customer.phone,
            "customer_opt_status": self.customer.opt_status,
            "customer_preferred_channel": self.customer.preferred_channel,
            "customer_timezone": self.customer.timezone,
            "dealer_id": self.dealer.id,
            "dealer_name": self.dealer.name,
            "dealer_phone": self.dealer.phone,
            "dealer_address": self.dealer.address,
            "ride_radius_miles": str(self.dealer.ride_radius_miles),
            "vehicle_year": str(self.vehicle.year),
            "vehicle_make": self.vehicle.make,
            "vehicle_model": self.vehicle.model,
            "vehicle_vin": self.vehicle.vin,
            "vehicle_odometer_miles": str(self.vehicle.odometer_miles),
            "vehicle_location_lat": f"{self.vehicle.current_location.latitude:.6f}",
            "vehicle_location_lon": f"{self.vehicle.current_location.longitude:.6f}",
            "vehicle_location_description": self.vehicle.current_location.description,
            "service_reason_type": self.service_event.type,
            "service_reason_summary": self.service_event.summary,
            "service_reason_narrative": self.service_event.narrative,
            "slot_count": str(len(self.offered_slots)),
            "slot_options": "; ".join(s.display for s in self.offered_slots),
        }

    @classmethod
    def variable_keys(cls) -> frozenset[str]:
        """Return the set of variable keys ``to_variables`` produces.

        Used by the agent's variable audit to cross-check the system
        prompt without needing a real fixture. Adding a field to
        ``to_variables`` automatically expands this set.
        """
        return frozenset(_audit_sample_case().to_variables().keys())


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CaseError(Exception):
    """Base class for all expected case-domain failures."""


@final
class CaseNotFoundError(CaseError):
    """No case with the requested id."""

    def __init__(self, case_id: CaseId) -> None:
        super().__init__(f"Case {case_id!r} not found")
        self.case_id = case_id


@final
class TriggerNotFoundError(CaseError):
    """No trigger with the requested id."""

    def __init__(self, trigger_id: TriggerId) -> None:
        super().__init__(f"Trigger {trigger_id!r} not found")
        self.trigger_id = trigger_id


@final
class TriggerForeignKeyError(CaseError):
    """A trigger references a master-data id that doesn't resolve.

    Raised by ``create_case_from_trigger`` when the vehicle, customer,
    or dealer the trigger points at can't be loaded. Typed so the
    monitor task can mark the trigger ``status='failed'`` and move on.
    """

    def __init__(self, trigger_id: TriggerId, missing: str) -> None:
        super().__init__(f"Trigger {trigger_id!r} references missing {missing}")
        self.trigger_id = trigger_id
        self.missing = missing


# ---------------------------------------------------------------------------
# Internal: synthetic Case used only by Case.variable_keys()
# ---------------------------------------------------------------------------


def _audit_sample_case() -> Case:
    """Synthetic Case whose only purpose is to enumerate variable keys.

    Values satisfy every constraint but are never sent to ElevenLabs.
    Lives in this file (not in the audit module) so the sample stays
    pinned to the schema.
    """
    return Case.model_validate(
        {
            "case_id": "case_sample",
            "trigger_id": "trig_sample",
            "correlation_id": "sample",
            "customer": {
                "id": "c",
                "first_name": "Sample",
                "last_name": "Customer",
                "phone": "5550000",
            },
            "dealer": {
                "id": "d",
                "name": "Sample Dealer",
                "phone": "5550000",
                "address": "1 Main St",
                "ride_radius_miles": 10,
            },
            "vehicle": {
                "vin": "1C4RJFBG5NC000000",
                "owner_id": "c",
                "year": 2025,
                "make": "Sample",
                "model": "Sample",
                "odometer_miles": 0,
                "current_location": {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "description": "sample",
                },
            },
            "service_event": {"type": "maintenance", "summary": "sample"},
            "offered_slots": [],
            "state": "created",
            "attempt_count": 0,
            "created_at": "2026-01-01T00:00:00Z",
        }
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
    "CaseNotFoundError",
    "CaseState",
    "Channel",
    "EventLevel",
    "EventSource",
    "OfferedSlot",
    "PostCallReport",
    "PostCallStatus",
    "ServiceEvent",
    "ServiceReasonType",
    "SlotId",
    "TranscriptRole",
    "TranscriptTurn",
    "Trigger",
    "TriggerForeignKeyError",
    "TriggerId",
    "TriggerNotFoundError",
    "TriggerSourceKind",
    "TriggerStatus",
]
