"""Side-effect vocabulary the case reducer emits.

The reducer (``decide_next_case_state``) is pure: it consumes the current
``Case`` and an incoming ``CaseSignal`` and returns a ``CaseDecision``
that describes:

- the next ``CaseState`` (may equal the current state for "no transition"),
- a tuple of ``CaseAction`` side-effects the driver must perform,
- a small narrow ``CasePatch`` of legal field updates,
- a short ``reason`` string for audit logs.

Side-effects never run inside the reducer. The Phase 4 ``CaseDriver``
walks the action tuple in order and dispatches each one to the right
adapter (``CallSession``, ``DealerSlotPort``, the timer wheel, the case
repo). This is the classic functional-core / imperative-shell split: all
decisions live in one place and are exhaustively testable; all I/O lives
in the driver and is integration-tested end-to-end.

Every action is a frozen Pydantic model with a unique ``action_type``
discriminator, so the union round-trips through JSON unambiguously and
can be persisted on a ``CaseEvent`` for forensic replay.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from guidepoint.case._models import (
    CaseId,
    Channel,
    EventLevel,
    SlotId,
)
from guidepoint.clock import UtcDatetime


def _frozen_strict() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Call stages — what kind of conversation the CallManager is asked to run.
# ---------------------------------------------------------------------------


class CallStage(StrEnum):
    """Which conversational stage a call belongs to.

    Drives prompt selection (one of two prompts today — ``outreach`` for
    the first reach, ``post_booking`` for everything that happens after
    a confirmed booking) and tool-surface restriction. The reducer picks
    the stage from the current ``CaseState``; the CallManager passes it
    through to the LLM call so the right system prompt and tool subset
    are bound.

    Mapping to prompts:

    - ``OUTREACH``         → ``outreach`` prompt (voice + SMS)
    - ``INITIAL_REMINDER`` → ``post_booking`` prompt, T-24h reminder text
    - ``FINAL_REMINDER``   → ``post_booking`` prompt, day-of reminder text
    - ``FEEDBACK``         → ``post_booking`` prompt, feedback ask
    """

    OUTREACH = "outreach"
    INITIAL_REMINDER = "initial_reminder"
    FINAL_REMINDER = "final_reminder"
    FEEDBACK = "feedback"


# ---------------------------------------------------------------------------
# Individual action variants
# ---------------------------------------------------------------------------


class PlaceCall(BaseModel):
    """Driver must hand off to the channel-appropriate ``CallSession``.

    The driver picks the channel from ``Case.initial_channel`` for the
    first attempt of a stage; reschedule / reminder calls may switch
    channel based on customer preference but Phase 3 always asks for the
    case's recorded initial channel and lets the driver decide overrides.
    """

    model_config = _frozen_strict()

    action_type: Literal["place_call"] = "place_call"
    case_id: CaseId
    stage: CallStage
    attempt_number: int = Field(ge=1)
    # Optional channel override; ``None`` means "use Case.initial_channel".
    channel: Channel | None = None


class RequestDealerSlots(BaseModel):
    """Driver must call ``DealerSlotPort.list_slots`` for this case.

    The response is fed back into the driver as a ``DealerSlotsListed``
    signal, which the reducer consumes on the next pass.
    """

    model_config = _frozen_strict()

    action_type: Literal["request_dealer_slots"] = "request_dealer_slots"
    case_id: CaseId


class RequestDealerConfirmation(BaseModel):
    """Driver must call ``DealerSlotPort.confirm_slot`` for this case.

    Response is fed back as ``DealerConfirmed`` or ``DealerRejected``.
    """

    model_config = _frozen_strict()

    action_type: Literal["request_dealer_confirmation"] = "request_dealer_confirmation"
    case_id: CaseId
    slot_id: SlotId


class ScheduleTimer(BaseModel):
    """Driver must register a one-shot timer for this case.

    When the wall clock reaches ``fire_at``, the driver enqueues either a
    ``InitialReminderDue`` / ``FinalReminderDue`` / ``TimerFired`` signal
    onto this case's queue (the ``name`` selects which). Timer names are
    stable strings so a duplicate ``ScheduleTimer`` for the same name
    replaces the prior one (driver responsibility, not the reducer's).
    """

    model_config = _frozen_strict()

    action_type: Literal["schedule_timer"] = "schedule_timer"
    case_id: CaseId
    name: str = Field(min_length=1)
    fire_at: UtcDatetime


class CancelTimer(BaseModel):
    """Driver must cancel a previously-scheduled per-case timer.

    No-op if no timer with that name exists; the reducer issues this
    defensively when it transitions away from a state that had armed a
    timer (e.g. cancelling the reminder timer when the customer
    cancels at reminder time).
    """

    model_config = _frozen_strict()

    action_type: Literal["cancel_timer"] = "cancel_timer"
    case_id: CaseId
    name: str = Field(min_length=1)


class RecordEvent(BaseModel):
    """Driver must append a ``CaseEvent`` to this case's history.

    The reducer issues this for every non-trivial decision so the audit
    trail captures *why* the case moved. The driver fills in
    ``event_id``, ``correlation_id``, ``timestamp`` and ``attempt_number``
    at write time; the reducer only provides the semantic payload.
    """

    model_config = _frozen_strict()

    action_type: Literal["record_event"] = "record_event"
    case_id: CaseId
    event: str = Field(min_length=1)
    level: EventLevel = "info"
    detail: str = ""


# ---------------------------------------------------------------------------
# The discriminated union — Phase 4 driver dispatches on action_type.
# ---------------------------------------------------------------------------


CaseAction = Annotated[
    PlaceCall
    | RequestDealerSlots
    | RequestDealerConfirmation
    | ScheduleTimer
    | CancelTimer
    | RecordEvent,
    Field(discriminator="action_type"),
]
"""Discriminated union of every side-effect the reducer can emit.

Use ``TypeAdapter(CaseAction)`` to round-trip through JSON; the
``action_type`` literal on each model picks the right concrete class on
deserialization, so persisting an action sequence onto a ``CaseEvent``
for replay is a one-liner.
"""


__all__ = [
    "CallStage",
    "CancelTimer",
    "CaseAction",
    "PlaceCall",
    "RecordEvent",
    "RequestDealerConfirmation",
    "RequestDealerSlots",
    "ScheduleTimer",
]
