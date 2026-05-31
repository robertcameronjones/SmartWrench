"""Case-level event vocabulary.

These are the **only** signals that cross into the ``CaseManager`` /
``CaseDriver``. Channel-internal events (LLM turn complete, twilio sid
received, ElevenLabs status update, customer turn text, STOP keyword)
stay inside the per-channel ``CallManager``\\s and never appear here.

The discipline that keeps this list small: if a signal name contains
``twilio``, ``elevenlabs``, ``llm``, or ``turn``, it belongs in a
channel reducer, not on this union.

Targeting
~~~~~~~~~

Every signal carries enough information for the ``CaseDriver`` to
route it to the right case(s):

- **Direct targeting** (``case_id`` present) — the driver enqueues
  onto that one case's per-case signal queue.
- **Indirect targeting** (``vehicle_vin`` for geofence,
  ``customer_phone`` for opt-out) — the driver resolves to the
  matching case(s) via the case repository.
- **World gates** (no targeting field) — the driver fans out to every
  case currently awaiting that gate.

The Phase 4 ``CaseDriver`` is responsible for the routing; this module
only defines the data shapes.

Discriminator
~~~~~~~~~~~~~

Every signal has a unique ``signal_type`` Literal. The discriminated
union (``CaseSignal``) is annotated with ``Field(discriminator=...)``
so Pydantic deserialization picks the right concrete model from JSON
without ambiguity.
"""

from __future__ import annotations

from guidepoint.clock import UtcDatetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from guidepoint.case._models import (
    CallOutcome,
    CaseId,
    EventSource,
    OfferedSlot,
    SlotId,
)
from guidepoint.master_data import VehicleVin


def _frozen_strict() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Case-targeted signals (carry case_id directly)
# ---------------------------------------------------------------------------


class CallEnded(BaseModel):
    """A ``CallManager`` finished and is reporting its terminal outcome.

    The only way per-channel state crosses into the CaseManager. The
    ``CallOutcome`` carries the rolled-up business decision (booked /
    declined / confirmed / cancelled / opted-out / incomplete) plus
    enough audit detail to reconstruct what happened.
    """

    model_config = _frozen_strict()

    signal_type: Literal["call_ended"] = "call_ended"
    timestamp: UtcDatetime
    source: EventSource = "system"
    case_id: CaseId
    outcome: CallOutcome


class DealerSlotsListed(BaseModel):
    """``DealerSlotPort.list_slots`` returned for this case."""

    model_config = _frozen_strict()

    signal_type: Literal["dealer_slots_listed"] = "dealer_slots_listed"
    timestamp: UtcDatetime
    source: EventSource = "dealer"
    case_id: CaseId
    slots: tuple[OfferedSlot, ...]


class DealerConfirmed(BaseModel):
    """Dealer confirmed the proposed booking for this case."""

    model_config = _frozen_strict()

    signal_type: Literal["dealer_confirmed"] = "dealer_confirmed"
    timestamp: UtcDatetime
    source: EventSource = "dealer"
    case_id: CaseId
    slot_id: SlotId


class DealerRejected(BaseModel):
    """Dealer could not honor the proposed booking; reducer re-proposes."""

    model_config = _frozen_strict()

    signal_type: Literal["dealer_rejected"] = "dealer_rejected"
    timestamp: UtcDatetime
    source: EventSource = "dealer"
    case_id: CaseId
    slot_id: SlotId
    reason: str = ""


class InitialReminderDue(BaseModel):
    """T-24h timer fired — initial reminder is due to be sent.

    Drives ``INITIAL_REMINDER_DUE → INITIAL_REMINDER_SENT`` and queues
    ``PlaceCall(INITIAL_REMINDER)``.
    """

    model_config = _frozen_strict()

    signal_type: Literal["initial_reminder_due"] = "initial_reminder_due"
    timestamp: UtcDatetime
    source: EventSource = "clock"
    case_id: CaseId


class FinalReminderDue(BaseModel):
    """Day-of (slot-2h) timer fired — final reminder is due to be sent.

    Transitions any still-open post-confirm state
    (``INITIAL_REMINDER_DUE``, ``INITIAL_REMINDER_SENT``, or
    ``FINAL_REMINDER_DUE``) to ``FINAL_REMINDER_SENT`` and queues
    ``PlaceCall(FINAL_REMINDER)``.
    """

    model_config = _frozen_strict()

    signal_type: Literal["final_reminder_due"] = "final_reminder_due"
    timestamp: UtcDatetime
    source: EventSource = "clock"
    case_id: CaseId


class TimerFired(BaseModel):
    """A named per-case timer fired (silence nudges, etc.)."""

    model_config = _frozen_strict()

    signal_type: Literal["timer_fired"] = "timer_fired"
    timestamp: UtcDatetime
    source: EventSource = "clock"
    case_id: CaseId
    name: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Indirectly-targeted signals (driver resolves to case via vehicle / phone)
# ---------------------------------------------------------------------------


class VehicleEnteredDealer(BaseModel):
    """Vehicle crossed into the dealer geofence.

    Driver resolves to the case(s) for ``vehicle_vin`` that are
    currently in ``FINAL_REMINDER_SENT`` (transition to ``SHOWED``).
    """

    model_config = _frozen_strict()

    signal_type: Literal["vehicle_entered_dealer"] = "vehicle_entered_dealer"
    timestamp: UtcDatetime
    source: EventSource = "geofence"
    vehicle_vin: VehicleVin


class VehicleExitedDealer(BaseModel):
    """Vehicle crossed out of the dealer geofence.

    The inferred meaning: service event complete, customer left. Driver
    resolves to the case(s) for ``vehicle_vin`` currently in ``SHOWED``
    (transition to ``COMPLETED``).
    """

    model_config = _frozen_strict()

    signal_type: Literal["vehicle_exited_dealer"] = "vehicle_exited_dealer"
    timestamp: UtcDatetime
    source: EventSource = "geofence"
    vehicle_vin: VehicleVin


class InboundSmsReceived(BaseModel):
    """One inbound SMS just arrived for an active case.

    The adapter resolved ``from_phone -> case_id`` via the routing
    store and republished the raw text body onto the case driver's
    signal queue. From here on the reducer owns the interpretation:
    digit picks at outreach, confirm / reschedule / cancel at the
    reminder stages, and free-text replies that need an LLM-composed
    answer.

    Opt-out (``STOP`` / ``UNSUBSCRIBE``) is **not** routed through
    this signal — the webhook handler short-circuits to
    :class:`CustomerOptedOut` directly so consent updates always run
    even if no case is active.
    """

    model_config = _frozen_strict()

    signal_type: Literal["inbound_sms_received"] = "inbound_sms_received"
    timestamp: UtcDatetime
    source: EventSource = "sms"
    case_id: CaseId
    from_phone: str = Field(min_length=1)
    body: str
    message_sid: str = ""


class CustomerOptedOut(BaseModel):
    """Customer sent STOP / UNSUBSCRIBE / similar.

    Driver flips ``Customer.sms_consent = False`` and resolves to any
    active case(s) for the customer with this ``phone`` so they can
    transition to ``OPTED_OUT``.
    """

    model_config = _frozen_strict()

    signal_type: Literal["customer_opted_out"] = "customer_opted_out"
    timestamp: UtcDatetime
    source: EventSource = "sms"
    customer_phone: str = Field(min_length=1)


class CustomerOptedIn(BaseModel):
    """Customer sent START / UNSTOP. Re-enables outbound SMS.

    Does not revive a previously-closed case; only re-enables consent
    for future cases against this customer.
    """

    model_config = _frozen_strict()

    signal_type: Literal["customer_opted_in"] = "customer_opted_in"
    timestamp: UtcDatetime
    source: EventSource = "sms"
    customer_phone: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Lifecycle signals (case-targeted, fired by the driver itself)
# ---------------------------------------------------------------------------


class CaseCreated(BaseModel):
    """The driver just created a new case and is ready to start outreach.

    Emitted exactly once per case, immediately after the case row is
    persisted, from :meth:`CaseDriver.fire`. The reducer consumes this
    in the ``CREATED`` state and transitions to ``CONTACTING_CUSTOMER``
    while emitting the initial ``PlaceCall`` action.

    This is a **lifecycle** event, not a world event — it doesn't say
    anything about wall-clock hours. The send-side business-hours gate
    lives entirely in the outbound queue worker, which holds messages
    until the simulator/dealer's hours boolean flips open. The
    state-machine fires sends whenever its model says to and lets the
    queue decide when they actually leave.
    """

    model_config = _frozen_strict()

    signal_type: Literal["case_created"] = "case_created"
    timestamp: UtcDatetime
    source: EventSource = "system"
    case_id: CaseId


# ---------------------------------------------------------------------------
# Outbound-queue result signal (worker → state machine, push not pull)
# ---------------------------------------------------------------------------


class OutboundDispatched(BaseModel):
    """The outbound queue worker just sent one message to Twilio.

    The single "I sent it" report from the queue back into the state
    machine's existing signal queue. The reducer audits the moment
    via :class:`RecordEvent` but does not change state — case
    transitions continue to be driven by :class:`CallEnded` and the
    geofence / reminder signals.

    Block / fail outcomes inside the queue are **not** signalled back:

    - **Consent refusal** at the worker is a race guard (STOP arrived
      between enqueue and dispatch). The case state machine's own
      :class:`CustomerOptedOut` handling already terminates the case;
      the dropped queue item needs no separate event.
    - **Retry exhaustion** (Twilio genuinely down) is handled by the
      session's own inactivity timeout, which eventually fires
      ``CallEnded(inconclusive)`` and lets the case move on.

    Both are logged at warn level by the worker for operator
    visibility, but neither flows back into the case as a signal.
    """

    model_config = _frozen_strict()

    signal_type: Literal["outbound_dispatched"] = "outbound_dispatched"
    timestamp: UtcDatetime
    source: EventSource = "system"
    case_id: CaseId
    item_id: str = Field(min_length=1)
    twilio_sid: str = Field(min_length=1)
    to_phone: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# World-gate signals (driver fans out to every awaiting case)
#
# ``BusinessHoursOpened`` and ``BusinessHoursClosed`` are kept in the
# signal vocabulary for forward compatibility (channel adapters or
# external consumers may want to observe hours transitions) but the
# state-machine reducer treats them as no-ops. The outbound queue
# worker — not the reducer — is the single owner of the hours-gating
# policy.
# ---------------------------------------------------------------------------


class BusinessHoursOpened(BaseModel):
    """Operator hours just opened.

    No-op for the case reducer; the outbound queue worker reads the
    business-hours boolean directly. Kept in the vocabulary so other
    listeners can react if they want to.
    """

    model_config = _frozen_strict()

    signal_type: Literal["business_hours_opened"] = "business_hours_opened"
    timestamp: UtcDatetime
    source: EventSource = "world"


class BusinessHoursClosed(BaseModel):
    """Operator hours just closed.

    No-op for the case reducer; see :class:`BusinessHoursOpened`.
    """

    model_config = _frozen_strict()

    signal_type: Literal["business_hours_closed"] = "business_hours_closed"
    timestamp: UtcDatetime
    source: EventSource = "world"


class EndOfBusinessDayReached(BaseModel):
    """End-of-business-day. Cases in ``FINAL_REMINDER_SENT`` with no
    geofence-in transition to ``NO_SHOW`` per the lifecycle."""

    model_config = _frozen_strict()

    signal_type: Literal["end_of_business_day_reached"] = "end_of_business_day_reached"
    timestamp: UtcDatetime
    source: EventSource = "clock"


# ---------------------------------------------------------------------------
# The discriminated union — Phase 3 reducer + Phase 4 driver consume this
# ---------------------------------------------------------------------------


CaseSignal = Annotated[
    CallEnded
    | CaseCreated
    | DealerSlotsListed
    | DealerConfirmed
    | DealerRejected
    | InboundSmsReceived
    | InitialReminderDue
    | FinalReminderDue
    | TimerFired
    | VehicleEnteredDealer
    | VehicleExitedDealer
    | CustomerOptedOut
    | CustomerOptedIn
    | BusinessHoursOpened
    | BusinessHoursClosed
    | EndOfBusinessDayReached
    | OutboundDispatched,
    Field(discriminator="signal_type"),
]
"""Discriminated union of every signal the ``CaseManager`` accepts.

Use Pydantic's ``TypeAdapter(CaseSignal)`` to round-trip JSON without
ambiguity; the ``signal_type`` literal on each model picks the right
concrete class on deserialization.
"""


# Convenience classifiers for the driver. These are deliberately simple
# tuples of type objects rather than runtime sets, so an exhaustive
# ``isinstance`` check works under strict typing and so adding a new
# signal type produces a typecheck error at every dispatch site that
# needs to update.

_CASE_TARGETED_TYPES: tuple[type[BaseModel], ...] = (
    CallEnded,
    CaseCreated,
    DealerSlotsListed,
    DealerConfirmed,
    DealerRejected,
    InboundSmsReceived,
    InitialReminderDue,
    FinalReminderDue,
    TimerFired,
    OutboundDispatched,
)

_VEHICLE_TARGETED_TYPES: tuple[type[BaseModel], ...] = (
    VehicleEnteredDealer,
    VehicleExitedDealer,
)

_CUSTOMER_TARGETED_TYPES: tuple[type[BaseModel], ...] = (
    CustomerOptedOut,
    CustomerOptedIn,
)

_WORLD_TARGETED_TYPES: tuple[type[BaseModel], ...] = (
    BusinessHoursOpened,
    BusinessHoursClosed,
    EndOfBusinessDayReached,
)


def is_case_targeted(signal: BaseModel) -> bool:
    """``True`` if ``signal`` carries a ``case_id`` that picks one case."""
    return isinstance(signal, _CASE_TARGETED_TYPES)


def is_vehicle_targeted(signal: BaseModel) -> bool:
    """``True`` if ``signal`` carries a ``vehicle_vin`` the driver resolves."""
    return isinstance(signal, _VEHICLE_TARGETED_TYPES)


def is_customer_targeted(signal: BaseModel) -> bool:
    """``True`` if ``signal`` carries a ``customer_phone`` the driver resolves."""
    return isinstance(signal, _CUSTOMER_TARGETED_TYPES)


def is_world_signal(signal: BaseModel) -> bool:
    """``True`` if ``signal`` has no targeting field — driver fans out."""
    return isinstance(signal, _WORLD_TARGETED_TYPES)


__all__ = [
    "BusinessHoursClosed",
    "BusinessHoursOpened",
    "CallEnded",
    "CaseCreated",
    "CaseSignal",
    "CustomerOptedIn",
    "CustomerOptedOut",
    "DealerConfirmed",
    "DealerRejected",
    "DealerSlotsListed",
    "EndOfBusinessDayReached",
    "FinalReminderDue",
    "InboundSmsReceived",
    "InitialReminderDue",
    "OutboundDispatched",
    "TimerFired",
    "VehicleEnteredDealer",
    "VehicleExitedDealer",
    "is_case_targeted",
    "is_customer_targeted",
    "is_vehicle_targeted",
    "is_world_signal",
]
