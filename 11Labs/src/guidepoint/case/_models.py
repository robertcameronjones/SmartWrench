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

from enum import StrEnum
from typing import Any, Literal, NewType, final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guidepoint.clock import UtcDatetime
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
    starts_at: UtcDatetime
    display: str = Field(min_length=1)


def lookup_slot_display(
    *,
    offered_slots: tuple[OfferedSlot, ...],
    slot_id: SlotId | None,
) -> str:
    """Return the customer-facing appointment label for ``slot_id``.

    Internal ids like ``slot_a`` are for the state machine and dealer
    port; prompts and the LLM should use this human-readable string
    (``OfferedSlot.display``) instead.
    """

    if slot_id is None:
        return ""
    for slot in offered_slots:
        if slot.id == slot_id:
            return slot.display
    return ""


def _summarize_last_event(events: tuple["CaseEvent", ...]) -> str:
    """Return a short ``"<event> @ <isoZ>: <detail>"`` for the most
    recent event, or ``""`` if none. Surfaced to the LLM via
    ``Case.to_variables()`` so Kate has explicit context about the
    state machine's last action without us replaying full history.
    """

    if not events:
        return ""
    last = events[-1]
    ts = last.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    detail = last.detail.strip().replace("\n", " ")
    if len(detail) > 120:
        detail = detail[:119] + "\u2026"
    if detail:
        return f"{last.event} @ {ts}: {detail}"
    return f"{last.event} @ {ts}"


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
    created_at: UtcDatetime
    fired_at: UtcDatetime | None = None
    error_detail: str = ""
    # Operator who fired this trigger (auth username today). Empty
    # string when no operator identity is available (e.g. monitor task
    # in production). Snapshotted onto the resulting ``Case.user_id``
    # at fire time so the inbound SMS webhook can find the right
    # per-user state (once per-user case repos land).
    user_id: str = ""


# ---------------------------------------------------------------------------
# Case lifecycle states
# ---------------------------------------------------------------------------


class CaseState(StrEnum):
    """Where a case is in its business lifecycle.

    V2 lifecycle enum: trigger → outreach → dealer confirmation →
    initial reminder → final reminder → service event → closed.

    Only states the v2 ``CaseDriver`` + pure reducer actually produce are
    declared. The reachability audit lives in
    ``docs/case-state-jump-table.html``; if you add a member here, add the
    ``next_state=`` branch that produces it in the same change, or it is
    dead weight. Channel-internal lifecycle (dial, opened, conversing)
    lives inside each ``CallManager`` and never appears here.

    Reminder stages are split into ``*_DUE`` (timer armed, nothing sent
    yet) and ``*_SENT`` (Kate has fired the message, awaiting customer
    reply / geofence / EoD). Symmetric across initial (T-24h) and final
    (day-of) so the four customer outcomes — confirm, reschedule,
    cancel, no response — land in the same place at both touchpoints.
    """

    # Non-terminal lifecycle states.
    CREATED = "created"
    CONTACTING_CUSTOMER = "contacting_customer"
    # The customer has picked a slot and it has been confirmed (dealer
    # confirmation is stubbed today). The appointment exists; the case now
    # rests here until the reminder timers fire. Non-terminal.
    BOOKED = "booked"
    INITIAL_REMINDER_DUE = "initial_reminder_due"
    INITIAL_REMINDER_SENT = "initial_reminder_sent"
    RESCHEDULING = "rescheduling"
    FINAL_REMINDER_DUE = "final_reminder_due"
    FINAL_REMINDER_SENT = "final_reminder_sent"
    SHOWED = "showed"

    # Terminal states (case closed).
    DECLINED = "declined"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    NO_SHOW = "no_show"
    RESCHEDULE_FAILED = "reschedule_failed"
    OPTED_OUT = "opted_out"
    COMPLETED = "completed"

    @property
    def is_terminal(self) -> bool:
        """``True`` when the case will not transition further."""
        return self in _TERMINAL_CASE_STATES


_TERMINAL_CASE_STATES: frozenset[CaseState] = frozenset(
    {
        CaseState.DECLINED,
        CaseState.CANCELLED,
        CaseState.ABANDONED,
        CaseState.NO_SHOW,
        CaseState.RESCHEDULE_FAILED,
        CaseState.OPTED_OUT,
        CaseState.COMPLETED,
    }
)


# Legacy state values mapped to their current equivalents. Applied by the
# ``Case`` model validator so older persisted cases still load.
#
# v1 outreach: READY_TO_CALL / CALLING / BETWEEN_ATTEMPTS all collapse
# into CONTACTING_CUSTOMER (the active-outreach state). UNREACHABLE and
# ESCALATED both meant "we gave up reaching the customer" — that's
# ABANDONED in v2's vocabulary.
#
# v2 post-booking: the original four reminder states (awaiting_reminder,
# reminded, confirmed, day_of) conflated "timer armed" with "message
# sent". They are renamed to the symmetric *_DUE / *_SENT pair so each
# touchpoint has the same four customer outcomes (confirm / reschedule /
# cancel / no response).
#
# Renames / retired states: slot_proposed / slot_picked were outreach
# sub-states the v2 reducer never produced (outreach collapses into
# contacting_customer); awaiting_feedback was a feedback touchpoint that
# was never wired (showed → completed on geofence-out). The old
# confirming_with_dealer state was renamed to booked — the customer has
# picked a slot and it is confirmed, so the case is booked while it waits
# for the reminder timers. (The v1 terminal "booked" is gone; "booked" is
# now this live non-terminal value.) Mapped here only so any fossil
# persisted rows still load instead of raising.
_LEGACY_STATE_MIGRATIONS: dict[str, str] = {
    "ready_to_call": "contacting_customer",
    "calling": "contacting_customer",
    "between_attempts": "contacting_customer",
    "unreachable": "abandoned",
    "escalated": "abandoned",
    "awaiting_reminder": "initial_reminder_due",
    "reminded": "initial_reminder_sent",
    "confirmed": "final_reminder_due",
    "day_of": "final_reminder_sent",
    "slot_proposed": "contacting_customer",
    "slot_picked": "contacting_customer",
    "awaiting_feedback": "showed",
    "confirming_with_dealer": "booked",
}


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

# v2 BusinessOutcome — what one call produced at the *business* layer.
#
# Decided values (customer made a choice that closes or advances the case):
#   - booked        — outreach: customer accepted, slot picked
#   - declined      — outreach: customer declined service
#   - confirmed     — reminder: customer reconfirmed existing booking
#   - rescheduled   — reminder: customer wants a new slot (drives RESCHEDULING)
#   - cancelled     — reminder: customer cancelled the booking
#   - feedback      — feedback: customer left feedback (or explicit decline)
#   - opted_out     — any stage: STOP keyword detected (SMS) / explicit opt-out (voice)
#
# Incomplete value (call ended with no business decision; reducer applies
# the one-retry, then yield policy):
#   - inconclusive  — no decision reached this call
#
# Deprecated v1 value retained for legacy JSON readback (mapped to ABANDONED
# by the v1 manager loop; never emitted by v2 CallManagers):
#   - transferred
BusinessOutcome = Literal[
    "booked",
    "declined",
    "confirmed",
    "rescheduled",
    "cancelled",
    "feedback",
    "opted_out",
    "inconclusive",
    "transferred",
]

# Set of business outcomes that count as "Decided" per the v2 retry policy.
# Everything else (None / "inconclusive" / connection-level failure) is
# "Incomplete" and the reducer applies the one-retry, then yield rule.
DECIDED_OUTCOMES: frozenset[str] = frozenset(
    {
        "booked",
        "declined",
        "confirmed",
        "rescheduled",
        "cancelled",
        "feedback",
        "opted_out",
    }
)


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
    booked_slot_display: str = ""
    elevenlabs_conversation_id: str = ""
    started_at: UtcDatetime
    ended_at: UtcDatetime
    duration_seconds: float = Field(ge=0.0)
    transcript: str = ""
    recording_url: str = ""
    error_detail: str = ""

    @property
    def is_decided(self) -> bool:
        """``True`` when this call produced a customer decision.

        The v2 retry policy uses this: ``Decided`` outcomes advance the
        case (book / decline / confirm / reschedule / cancel / feedback /
        opt-out). Anything else (``False``) is ``Incomplete`` and the
        reducer applies one retry, then yields.
        """
        return (
            self.business_outcome is not None
            and self.business_outcome in DECIDED_OUTCOMES
        )


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
    started_at: UtcDatetime
    ended_at: UtcDatetime
    duration_seconds: float = Field(ge=0.0)
    transcript: tuple[TranscriptTurn, ...] = ()
    business_outcome: BusinessOutcome = "inconclusive"
    booked_slot_id: SlotId | None = None
    booked_slot_display: str = ""
    recording_url: str = ""
    error_detail: str = ""


# ---------------------------------------------------------------------------
# Case event log entry
# ---------------------------------------------------------------------------

EventLevel = Literal["info", "warn", "error", "debug"]
EventSource = Literal[
    "system",
    "elevenlabs",
    "sms",
    "tool_webhook",
    "operator",
    # v2 case-signal sources — added in Phase 2 alongside CaseSignal.
    "geofence",  # vehicle entered/exited dealer
    "dealer",  # dealer slot port responses
    "clock",  # scheduled timers (reminder, day-of, EoD)
    "world",  # global gates (business hours)
]


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
    timestamp: UtcDatetime
    source: EventSource = "system"
    level: EventLevel = "info"
    event: str = Field(min_length=1)
    detail: str = ""
    # Case state at the moment this event was recorded (i.e. *after* any
    # transition the originating decision applied). Optional so audit
    # rows persisted before this field existed still load. The driver
    # always populates it for live events.
    state: CaseState | None = None


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

    # Channel the case's **initial outreach** was made on. Snapshotted
    # from ``Trigger.channel_preference`` at fire time. Downstream
    # stages (reminder, day-of, feedback) always use SMS regardless of
    # this value — see the v2 lifecycle doc. Renamed from ``channel``
    # in v2 to make the per-stage channel semantics explicit; the model
    # validator below accepts the legacy ``channel`` field for
    # backward-compat with v1 JSON cases.
    initial_channel: Channel = "voice"

    # Operator who fired the trigger that created this case. Empty
    # string when no operator identity exists (production monitor task)
    # or for cases serialized before this field was introduced. Used
    # today by the inbound SMS webhook for audit logging; future
    # per-user case repos will route on this.
    user_id: str = ""

    # State machine.
    state: CaseState = CaseState.CREATED
    attempt_count: int = Field(ge=0, default=0)
    next_attempt_at: UtcDatetime | None = None

    # Count of reschedule attempts consumed at the reminder stage.
    # Policy is one-shot: ``reschedule_count >= 1`` plus a failed
    # second swing yields the ``RESCHEDULE_FAILED`` terminal. Set by
    # the Phase 3 reducer; defaults to zero for v1 cases that predate
    # the field.
    reschedule_count: int = Field(ge=0, default=0)

    # Short, human-readable notes captured by the OUTREACH CallManager
    # at end of call (e.g. "customer prefers mornings; mentioned
    # they're traveling next week"). The post-booking SMS prompt
    # exposes this as a dynamic variable so reminder / day-of /
    # feedback conversations feel continuous with the original
    # outreach. Cap at ~200 chars to keep the prompt focused.
    context_notes: str = Field(default="", max_length=200)

    # History.
    call_attempts: tuple[CallAttempt, ...] = ()
    events: tuple[CaseEvent, ...] = ()

    # Outcome (only set in terminal states).
    outcome_detail: str = ""
    booked_slot_id: SlotId | None = None
    # Customer-facing appointment time (e.g. "Tuesday, May 12, 2026 -
    # 8:30 AM"). Set when the customer picks a slot; fed to post-
    # booking prompts via ``to_variables()`` as ``booked_slot_display``.
    # Not the internal ``SlotId`` — the LLM never sees ``slot_a``.
    booked_slot_display: str = ""

    # Timestamps.
    created_at: UtcDatetime
    closed_at: UtcDatetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, values: Any) -> Any:
        """Accept v1-shaped JSON: legacy ``channel`` field and old states.

        The case fixtures and any persisted case JSON files written
        under v1 use ``channel`` as the field name and may carry state
        values that were retired in v2 (``ready_to_call``, ``calling``,
        ``between_attempts``, ``unreachable``, ``escalated``). This
        validator normalizes them into the v2 shape before pydantic's
        type validation runs, so downstream code only ever sees v2
        field names and v2 state values.

        Skips silently for non-dict inputs (e.g. ``Case.model_validate``
        passing through an existing instance).
        """
        if not isinstance(values, dict):
            return values

        if "channel" in values and "initial_channel" not in values:
            values["initial_channel"] = values.pop("channel")

        raw_state = values.get("state")
        if isinstance(raw_state, str) and raw_state in _LEGACY_STATE_MIGRATIONS:
            values["state"] = _LEGACY_STATE_MIGRATIONS[raw_state]

        return values

    def to_variables(self) -> dict[str, str]:
        """Flatten the case snapshot into the dict ElevenLabs receives.

        Keys here MUST match the ``{{placeholder}}`` names inside
        ``config/system-prompt.md``. The variable audit module reads
        ``Case.variable_keys()`` to enforce that contract.
        """
        return {
            "channel": self.initial_channel,
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
            "booked_slot_display": self.booked_slot_display
            or lookup_slot_display(
                offered_slots=self.offered_slots, slot_id=self.booked_slot_id
            ),
            "context_notes": self.context_notes,
            # Hand the LLM the case state machine's view of where we are.
            # Without this Kate cannot tell the outreach stage from a
            # reminder run or a day-of run — she only sees prompt + history
            # and ends up repeating prior turns.
            "case_state": self.state.value,
            "attempt_count": str(self.attempt_count),
            "last_event_summary": _summarize_last_event(self.events),
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
            "initial_channel": "voice",
            "state": "created",
            "attempt_count": 0,
            "reschedule_count": 0,
            "booked_slot_display": "",
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
    "lookup_slot_display",
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
