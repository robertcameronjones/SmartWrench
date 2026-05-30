"""Pure case-state reducer (Phase 3).

``decide_next_case_state(case, signal, *, now)`` is the **only** place in
the codebase that decides what happens next to a case. It is:

- **Pure** — no I/O, no logging, no randomness. Given the same inputs it
  returns the same ``CaseDecision`` byte-for-byte. ``now`` is passed in
  explicitly rather than read from a clock for the same reason.
- **Total** — every (``CaseState``, ``CaseSignal``) combination produces
  a decision. Most combinations are "ignored at this stage"; those return
  a decision with ``next_state == case.state`` and ``actions=()``.
  Tests assert that the reducer never raises across the full cross-product.
- **Effect-free** — the reducer describes what the driver should do via
  the ``CaseAction`` discriminated union; it never executes anything.

Anything that needs to look at the wall clock (e.g. "is the slot more
than 24h away?") receives ``now`` as a parameter so the test suite can
freeze time and the production driver passes ``clock.now()`` once at the
top of each tick.

The driver (Phase 4) is the imperative shell: it reads the decision,
applies the actions in order, persists the resulting case, emits the
recorded events, and feeds any returned-by-action signals back into the
queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, final

from pydantic import BaseModel, ConfigDict, Field

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
from guidepoint.case._models import (
    Case,
    CaseState,
    SlotId,
    lookup_slot_display,
)
from guidepoint.case._signals import (
    BusinessHoursClosed,
    BusinessHoursOpened,
    CallEnded,
    CaseCreated,
    CaseSignal,
    CustomerOptedIn,
    CustomerOptedOut,
    DealerConfirmed,
    DealerRejected,
    OutboundDispatched,
    DealerSlotsListed,
    EndOfBusinessDayReached,
    FinalReminderDue,
    InboundSmsReceived,
    InitialReminderDue,
    TimerFired,
    VehicleEnteredDealer,
    VehicleExitedDealer,
)
from guidepoint.clock import UtcDatetime

# ---------------------------------------------------------------------------
# Timer + policy constants — surfaced here so tests can reference them.
# ---------------------------------------------------------------------------

#: Per-call retry budget for *incomplete* outcomes. The reducer issues
#: one retry (attempt 2) on the first ``inconclusive`` call ending; a
#: second ``inconclusive`` yields the stage-appropriate terminal /
#: hold state. ``Decided`` outcomes never retry — the customer chose.
MAX_CALL_ATTEMPTS: int = 2

#: Initial reminder fires at booked_slot - 24h. If a booking is less
#: than this away when it is confirmed, the initial reminder stage is
#: skipped and the case jumps straight to FINAL_REMINDER_DUE (per the
#: user policy: "no rapid double-tap").
INITIAL_REMINDER_LEAD: timedelta = timedelta(hours=24)

#: Final (day-of) reminder fires at booked_slot - 2h. Inside business
#: hours only; the driver's clock is responsible for not firing this
#: overnight.
FINAL_REMINDER_LEAD: timedelta = timedelta(hours=2)

#: Reschedule one-shot limit. A second swing failing → RESCHEDULE_FAILED.
MAX_RESCHEDULES: int = 1

# Named timers the reducer arms / cancels. Centralised so the driver
# (Phase 4) and the simulator scaffolding can reference the same strings.
TIMER_INITIAL_REMINDER: str = "initial_reminder"
TIMER_FINAL_REMINDER: str = "final_reminder"
TIMER_END_OF_DAY: str = "end_of_business_day"


# ---------------------------------------------------------------------------
# CasePatch — the narrow set of field updates the reducer is allowed to
# request. The driver applies these to the persisted case after the
# transition. Using a small explicit record (rather than letting the
# reducer return a full Case) keeps the contract obvious and prevents
# accidental mutation of audit fields.
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class CasePatch:
    """Narrow set of legal field updates the reducer can request.

    Every field defaults to ``None`` meaning "do not touch". The driver
    applies only the non-``None`` entries. ``increment_reschedule_count``
    is the one boolean: ``True`` means "++ the counter" (the reducer
    never sets an absolute count).
    """

    booked_slot_id: SlotId | None = None
    booked_slot_display: str | None = None
    next_attempt_at: UtcDatetime | None = None
    context_notes: str | None = None
    increment_reschedule_count: bool = False
    increment_attempt_count: bool = False


# Sentinel for "no field updates this tick". Frozen + slotted so a single
# module-level instance is safe to share across decisions.
_NO_PATCH: CasePatch = CasePatch()


# ---------------------------------------------------------------------------
# CaseDecision — the reducer's return type. Frozen Pydantic so we get
# JSON round-trip + structural equality for tests for free.
# ---------------------------------------------------------------------------


def _decision_config() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)


class CaseDecision(BaseModel):
    """One reducer output: where the case goes and what the driver must do.

    A "no-op" decision (signal arrived but doesn't apply at this state)
    is encoded with ``next_state == case.state``, ``actions=()``,
    ``patch=CasePatch()``, and a ``reason`` starting with ``"ignored:"``.
    The driver detects no-ops cheaply by checking the action tuple.
    """

    model_config = _decision_config()

    next_state: CaseState
    actions: tuple[CaseAction, ...] = ()
    patch: CasePatch = Field(default=_NO_PATCH)
    reason: str = Field(min_length=1)

    @property
    def is_noop(self) -> bool:
        """``True`` when the decision changes nothing."""
        return not self.actions and self.patch == _NO_PATCH


# ---------------------------------------------------------------------------
# Small helpers used inside the reducer's pattern match. Kept private.
# ---------------------------------------------------------------------------


def _stage_for_state(state: CaseState) -> CallStage | None:
    """Map a case state to the call stage used for *new* outbound calls
    from that state. Returns ``None`` when the state has no associated
    outbound call (e.g. terminal, or "waiting on the clock")."""

    if state in (
        CaseState.CREATED,
        CaseState.CONTACTING_CUSTOMER,
        CaseState.SLOT_PROPOSED,
        CaseState.SLOT_PICKED,
        CaseState.RESCHEDULING,
    ):
        return CallStage.OUTREACH
    if state in (CaseState.INITIAL_REMINDER_DUE, CaseState.INITIAL_REMINDER_SENT):
        return CallStage.INITIAL_REMINDER
    if state in (CaseState.FINAL_REMINDER_DUE, CaseState.FINAL_REMINDER_SENT):
        return CallStage.FINAL_REMINDER
    if state in (CaseState.SHOWED, CaseState.AWAITING_FEEDBACK):
        return CallStage.FEEDBACK
    return None


def _ignored(case: Case, why: str) -> CaseDecision:
    """Build a no-op decision: same state, no actions, with a reason."""

    return CaseDecision(next_state=case.state, reason=f"ignored:{why}")


def _opt_out_decision(case: Case) -> CaseDecision:
    """Close any non-terminal case via the opt-out path."""

    if case.state.is_terminal:
        return _ignored(case, "already_terminal")
    return CaseDecision(
        next_state=CaseState.OPTED_OUT,
        actions=(
            CancelTimer(case_id=case.case_id, name=TIMER_INITIAL_REMINDER),
            CancelTimer(case_id=case.case_id, name=TIMER_FINAL_REMINDER),
            CancelTimer(case_id=case.case_id, name=TIMER_END_OF_DAY),
            RecordEvent(
                case_id=case.case_id,
                event="case.closed.opted_out",
                level="warn",
                detail="customer opted out of SMS",
            ),
        ),
        reason="opt_out",
    )


def _next_attempt_number(case: Case) -> int:
    """Attempt number the next outbound call should carry."""

    return case.attempt_count + 1


def _initial_reminder_lead_satisfied(now: datetime, booked: datetime) -> bool:
    """``True`` when the booked slot is at least ``INITIAL_REMINDER_LEAD``
    away.

    Determines whether a freshly-confirmed booking still has room for
    the initial (T-24h) reminder stage, or whether the case should skip
    straight to ``FINAL_REMINDER_DUE``.
    """

    return (booked - now) >= INITIAL_REMINDER_LEAD


# ---------------------------------------------------------------------------
# The reducer itself. One giant pattern match. Each top-level branch is
# keyed on the signal type; per-signal we then key on the case state.
# Keep the order stable so adding a new signal type produces a single
# diff hunk.
# ---------------------------------------------------------------------------


def decide_next_case_state(
    case: Case,
    signal: CaseSignal,
    *,
    now: datetime,
) -> CaseDecision:
    """Compute the case's next state + actions from one signal.

    Pure. Every state/signal combination is handled — invalid pairs
    return a no-op decision with a ``reason`` starting ``"ignored:"``
    rather than raising. The Phase 4 driver treats ``is_noop`` decisions
    as "log + skip persistence".

    Args:
        case: Current case snapshot. Not mutated.
        signal: The incoming case-level signal.
        now: Wall clock at the start of this driver tick. Passed in so
            tests can pin time.

    Returns:
        A ``CaseDecision`` describing the next state, the side-effects
        the driver must perform, and a narrow patch of legal field
        updates.
    """

    # Terminal cases ignore everything except opt-in (informational; no
    # state change). This keeps the dispatch table below much smaller.
    if case.state.is_terminal:
        return _ignored(case, "terminal")

    # Opt-out is universal: any non-terminal case closes on this signal,
    # regardless of which state it was in or how it was targeted.
    if isinstance(signal, CustomerOptedOut):
        return _opt_out_decision(case)

    # Opt-in is informational at the case level — the driver flips
    # Customer.sms_consent, but the case itself doesn't move. Future
    # cases against this customer can use SMS again.
    if isinstance(signal, CustomerOptedIn):
        return _ignored(case, "opt_in_does_not_revive_case")

    # ---- Lifecycle ------------------------------------------------------

    if isinstance(signal, CaseCreated):
        return _on_case_created(case)

    # ---- World gates: business hours + end-of-day -----------------------
    #
    # The state-machine has *no opinion* on hours. The outbound queue
    # worker is the single owner of the hours-gating policy: it reads
    # the world boolean directly and holds messages when closed. The
    # reducer therefore swallows hours-opened/closed signals — they
    # exist in the signal union for other listeners but never drive a
    # case transition.

    if isinstance(signal, BusinessHoursOpened):
        return _ignored(case, "hours_handled_by_outbound_queue")

    if isinstance(signal, BusinessHoursClosed):
        return _ignored(case, "hours_handled_by_outbound_queue")

    if isinstance(signal, EndOfBusinessDayReached):
        return _on_end_of_business_day(case)

    # ---- Outbound queue result (audit-only push from worker) -------------
    #
    # The single "I sent it" report from the queue. The state machine
    # does not transition on it — case transitions continue to be driven
    # by ``CallEnded`` and the geofence / reminder signals — but the
    # operator audit trail wants to see exactly when each message left
    # with its real Twilio MessageSid. Blocked / failed dispatches are
    # worker-level structlog warnings only; the case has its own paths
    # (``CustomerOptedOut``, session timeout → ``CallEnded(inconclusive)``)
    # for those.

    if isinstance(signal, OutboundDispatched):
        return _on_outbound_dispatched(case, signal)

    # ---- Per-case targeted signals ---------------------------------------

    if isinstance(signal, CallEnded):
        return _on_call_ended(case, signal, now=now)

    if isinstance(signal, InboundSmsReceived):
        return _on_inbound_sms_received(case, signal)

    if isinstance(signal, DealerSlotsListed):
        return _on_dealer_slots_listed(case, signal)

    if isinstance(signal, DealerConfirmed):
        return _on_dealer_confirmed(case, signal, now=now)

    if isinstance(signal, DealerRejected):
        return _on_dealer_rejected(case, signal)

    if isinstance(signal, InitialReminderDue):
        return _on_initial_reminder_due(case)

    if isinstance(signal, FinalReminderDue):
        return _on_final_reminder_due(case)

    if isinstance(signal, TimerFired):
        return _on_timer_fired(case, signal)

    # ---- Geofence (indirect targeting handled by driver) -----------------

    if isinstance(signal, VehicleEnteredDealer):
        return _on_vehicle_entered(case)

    if isinstance(signal, VehicleExitedDealer):
        return _on_vehicle_exited(case)

    # Unreachable under the discriminated union, but keeps mypy/pyright
    # happy without a `# type: ignore`.
    return _ignored(case, "unknown_signal")


# ---------------------------------------------------------------------------
# Per-signal handlers. Each one is a small, focused function so the
# (state, signal) cells in the test grid map 1:1 to readable branches.
# ---------------------------------------------------------------------------


def _on_outbound_dispatched(
    case: Case, signal: OutboundDispatched
) -> CaseDecision:
    """A queued outbound message just left for Twilio. Audit only."""

    return CaseDecision(
        next_state=case.state,
        actions=(
            RecordEvent(
                case_id=case.case_id,
                event="case.outbound.dispatched",
                detail=(
                    f"to={signal.to_phone} sid={signal.twilio_sid} "
                    f"item={signal.item_id}"
                ),
            ),
        ),
        reason="outbound_dispatched",
    )


def _on_case_created(case: Case) -> CaseDecision:
    """Lifecycle kickoff: a new case enters and outreach begins immediately.

    The state-machine fires the outreach send unconditionally; the
    outbound queue worker decides whether the message actually leaves
    now (hours open) or sits until later (hours closed). This keeps
    the reducer free of any wall-clock awareness — the "queue holds
    when closed, drains when open" property is owned by the queue,
    not by case state transitions.
    """

    if case.state != CaseState.CREATED:
        return _ignored(case, f"case_created_irrelevant_at_{case.state.value}")
    return CaseDecision(
        next_state=CaseState.CONTACTING_CUSTOMER,
        actions=(
            PlaceCall(
                case_id=case.case_id,
                stage=CallStage.OUTREACH,
                attempt_number=_next_attempt_number(case),
            ),
            RecordEvent(
                case_id=case.case_id,
                event="case.outreach.started",
                detail="new case — placing initial outreach call",
            ),
        ),
        patch=CasePatch(increment_attempt_count=True),
        reason="case_created",
    )


def _on_end_of_business_day(case: Case) -> CaseDecision:
    """A case in ``FINAL_REMINDER_SENT`` with no geofence-in becomes
    ``NO_SHOW`` at end of business day."""

    if case.state != CaseState.FINAL_REMINDER_SENT:
        return _ignored(case, f"eod_irrelevant_at_{case.state.value}")
    return CaseDecision(
        next_state=CaseState.NO_SHOW,
        actions=(
            CancelTimer(case_id=case.case_id, name=TIMER_END_OF_DAY),
            RecordEvent(
                case_id=case.case_id,
                event="case.closed.no_show",
                level="warn",
                detail="end of business day with no geofence entry",
            ),
        ),
        reason="no_show_at_eod",
    )


def _on_call_ended(case: Case, signal: CallEnded, *, now: datetime) -> CaseDecision:
    """Roll up a CallEnded into a case transition.

    The behaviour splits on (a) the stage the case was in when the call
    was placed (read from ``case.state``) and (b) whether the outcome
    was ``Decided`` or ``Incomplete``.
    """

    outcome = signal.outcome
    stage = _stage_for_state(case.state)
    if stage is None:
        return _ignored(case, f"call_ended_no_active_stage_at_{case.state.value}")

    # Decided outcomes route by business_outcome.
    if outcome.is_decided:
        return _on_decided_outcome(case, signal, now=now)

    # Incomplete outcome: one retry, then yield.
    return _on_incomplete_outcome(case, signal, stage=stage)


def _on_decided_outcome(
    case: Case, signal: CallEnded, *, now: datetime
) -> CaseDecision:
    """Map every Decided business_outcome to a transition."""

    outcome = signal.outcome
    bo = outcome.business_outcome

    # Opt-out via call (SMS keyword path). Same as the CustomerOptedOut
    # signal — terminate the case via the universal opt-out path.
    if bo == "opted_out":
        return _opt_out_decision(case)

    # Outreach stage decisions ------------------------------------------------

    if case.state in (
        CaseState.CREATED,
        CaseState.CONTACTING_CUSTOMER,
        CaseState.SLOT_PROPOSED,
        CaseState.SLOT_PICKED,
    ):
        if bo == "booked":
            slot_id = outcome.booked_slot_id
            if slot_id is None:
                return _ignored(case, "booked_without_slot_id")
            return CaseDecision(
                next_state=CaseState.CONFIRMING_WITH_DEALER,
                actions=(
                    RequestDealerConfirmation(case_id=case.case_id, slot_id=slot_id),
                    RecordEvent(
                        case_id=case.case_id,
                        event="case.slot.picked",
                        detail=f"customer picked slot {slot_id}; confirming with dealer",
                    ),
                ),
                patch=CasePatch(
                    booked_slot_id=slot_id,
                    booked_slot_display=_resolve_booked_slot_display(
                        case, slot_id, from_outcome=outcome.booked_slot_display
                    ),
                ),
                reason="outreach_booked",
            )
        if bo == "declined":
            return CaseDecision(
                next_state=CaseState.DECLINED,
                actions=(
                    RecordEvent(
                        case_id=case.case_id,
                        event="case.closed.declined",
                        detail="customer declined service during outreach",
                    ),
                ),
                reason="outreach_declined",
            )
        return _ignored(case, f"outreach_unexpected_outcome:{bo}")

    # Reminder stage decisions ---------------------------------------------
    # ``INITIAL_REMINDER_SENT`` and ``FINAL_REMINDER_SENT`` share the
    # same customer-outcome vocabulary (confirm / reschedule / cancel /
    # silence). The handler is parameterised by stage so the outgoing
    # transitions stay symmetric.

    if case.state == CaseState.INITIAL_REMINDER_SENT:
        decision = _post_booking_touchpoint_outcome(case, bo=bo, stage="initial")
        if decision is not None:
            return decision
        return _ignored(case, f"post_booking_unexpected_outcome:{bo}")

    if case.state == CaseState.FINAL_REMINDER_SENT:
        decision = _post_booking_touchpoint_outcome(case, bo=bo, stage="final")
        if decision is not None:
            return decision
        return _ignored(case, f"post_booking_unexpected_outcome:{bo}")

    # Rescheduling — outreach-shaped re-pick of a slot ----------------------

    if case.state == CaseState.RESCHEDULING:
        if bo == "booked":
            slot_id = outcome.booked_slot_id
            if slot_id is None:
                return _ignored(case, "rescheduling_booked_without_slot_id")
            return CaseDecision(
                next_state=CaseState.CONFIRMING_WITH_DEALER,
                actions=(
                    RequestDealerConfirmation(case_id=case.case_id, slot_id=slot_id),
                    RecordEvent(
                        case_id=case.case_id,
                        event="case.reschedule.picked",
                        detail=f"customer picked replacement slot {slot_id}",
                    ),
                ),
                patch=CasePatch(
                    booked_slot_id=slot_id,
                    booked_slot_display=_resolve_booked_slot_display(
                        case, slot_id, from_outcome=outcome.booked_slot_display
                    ),
                ),
                reason="rescheduling_booked",
            )
        if bo == "cancelled":
            return CaseDecision(
                next_state=CaseState.CANCELLED,
                actions=(
                    RecordEvent(
                        case_id=case.case_id,
                        event="case.closed.cancelled",
                        detail="customer cancelled during reschedule",
                    ),
                ),
                reason="rescheduling_cancelled",
            )
        return _ignored(case, f"rescheduling_unexpected_outcome:{bo}")

    # Feedback stage --------------------------------------------------------

    if case.state in (CaseState.SHOWED, CaseState.AWAITING_FEEDBACK):
        if bo == "feedback":
            return CaseDecision(
                next_state=CaseState.COMPLETED,
                actions=(
                    RecordEvent(
                        case_id=case.case_id,
                        event="case.closed.completed",
                        detail="feedback captured",
                    ),
                ),
                reason="feedback_captured",
            )
        return _ignored(case, f"feedback_unexpected_outcome:{bo}")

    # Anything else (CONFIRMING_WITH_DEALER, INITIAL_REMINDER_DUE etc with
    # a Decided call result) is a sequencing bug — we shouldn't be calling
    # there. Don't crash, just ignore + audit-by-reason.
    return _ignored(case, f"decided_outcome_unexpected_at_{case.state.value}")


def _on_incomplete_outcome(
    case: Case, signal: CallEnded, *, stage: CallStage
) -> CaseDecision:
    """One retry, then yield. Yield meaning depends on stage."""

    attempts = case.attempt_count
    if attempts < MAX_CALL_ATTEMPTS:
        # Retry: same state, place another call. Driver will respect any
        # business-hours guard. Attempt count is incremented via patch so
        # the next call's number is correct.
        return CaseDecision(
            next_state=case.state,
            actions=(
                PlaceCall(
                    case_id=case.case_id,
                    stage=stage,
                    attempt_number=attempts + 1,
                ),
                RecordEvent(
                    case_id=case.case_id,
                    event="case.call.retry",
                    detail=f"incomplete call; retrying ({attempts + 1}/{MAX_CALL_ATTEMPTS})",
                ),
            ),
            patch=CasePatch(increment_attempt_count=True),
            reason="incomplete_retry",
        )

    # Budget exhausted. Yield by stage:
    # - outreach          → ABANDONED
    # - initial reminder  → no-op (case stays in INITIAL_REMINDER_SENT;
    #                       the final-reminder timer was armed at
    #                       dealer-confirm and fires on its own schedule)
    # - final reminder    → no-op (case stays in FINAL_REMINDER_SENT;
    #                       the geofence + EoD still decide the terminal
    #                       outcome)
    # - feedback          → COMPLETED (silence after service is fine)
    if stage == CallStage.OUTREACH:
        return CaseDecision(
            next_state=CaseState.ABANDONED,
            actions=(
                RecordEvent(
                    case_id=case.case_id,
                    event="case.closed.abandoned",
                    level="warn",
                    detail="outreach exhausted retry budget",
                ),
            ),
            reason="outreach_abandoned",
        )
    if stage == CallStage.INITIAL_REMINDER:
        # Silence at the initial reminder is a no-op. The final-reminder
        # timer was armed at dealer-confirm and fires on its own
        # schedule; the case stays in INITIAL_REMINDER_SENT until then.
        return _ignored(case, "initial_reminder_incomplete_awaiting_final")
    if stage == CallStage.FINAL_REMINDER:
        return _ignored(case, "final_reminder_incomplete_geofence_will_decide")
    if stage == CallStage.FEEDBACK:
        return CaseDecision(
            next_state=CaseState.COMPLETED,
            actions=(
                RecordEvent(
                    case_id=case.case_id,
                    event="case.closed.completed",
                    detail="feedback skipped; service event complete",
                ),
            ),
            reason="feedback_silence_completes",
        )
    return _ignored(case, "incomplete_unknown_stage")


# ---------------------------------------------------------------------------
# Inbound SMS classification + reducer handler.
#
# Every inbound text from the customer arrives here. The reducer maps
# (state, body) to a decision:
#
# - Reminder stages (initial / final) — recognise the three documented
#   options confirm / reschedule / cancel and route them through the
#   existing post-booking touchpoint logic. Anything else (including
#   free text) keeps the case in the same state and triggers a
#   composed LLM reply via a fresh ``PlaceCall`` action.
# - Outreach / propose / picked / rescheduling — recognise a numbered
#   digit pick against ``case.offered_slots``. The Nth+1 option is
#   "none of those work" → DECLINED. Free text triggers a composed
#   LLM reply.
# - Anywhere else (confirming-with-dealer, awaiting-reminders,
#   showed, awaiting-feedback) — log + ignore.
# ---------------------------------------------------------------------------

# Customer reply vocabularies for the two post-booking touchpoints. Aligned
# with the numbered options in ``prompt-post-booking.md``: 1 Confirm,
# 2 Reschedule, 3 Cancel. Kept here in the reducer (rather than the SMS
# adapter) so the state machine fully owns the inbound-→-decision mapping.
_INBOUND_CONFIRMED: frozenset[str] = frozenset(
    {"1", "CONFIRMED", "CONFIRM", "YES", "Y", "OK", "OKAY", "YEP", "YUP"}
)
_INBOUND_RESCHEDULE: frozenset[str] = frozenset(
    {"2", "RESCHEDULE", "RESCHED", "RESCHEDULED", "RESCHEDULING", "NEW TIME"}
)
_INBOUND_CANCEL: frozenset[str] = frozenset(
    {"3", "CANCEL", "CANCELLED", "CANCELED", "NO", "N"}
)
_OUTREACH_CANCEL: frozenset[str] = frozenset({"CANCEL", "CANCELLED", "CANCELED"})

_OUTREACH_STATES: frozenset[CaseState] = frozenset(
    {
        CaseState.CREATED,
        CaseState.CONTACTING_CUSTOMER,
        CaseState.SLOT_PROPOSED,
        CaseState.SLOT_PICKED,
        CaseState.RESCHEDULING,
    }
)


def _normalize_inbound(body: str) -> str:
    """Strip + uppercase one SMS body for vocabulary matching."""

    return body.strip().upper()


def _classify_post_booking_reply(normalized: str) -> str | None:
    """Map a normalized inbound body to a post-booking outcome verb.

    Returns one of ``"confirmed"`` / ``"rescheduled"`` / ``"cancelled"``
    when the text matches one of the documented numbered options, or
    ``None`` for free text / ambiguity (in which case the reducer
    falls through to an LLM-composed reply).
    """

    if normalized in _INBOUND_CONFIRMED:
        return "confirmed"
    if normalized in _INBOUND_RESCHEDULE:
        return "rescheduled"
    if normalized in _INBOUND_CANCEL:
        return "cancelled"
    return None


_OutreachDigitKind = Literal["book", "decline", "none"]


def _interpret_outreach_digit(body: str, *, case: Case) -> tuple[_OutreachDigitKind, SlotId | None]:
    """Map a customer's digit-only reply to a slot pick during outreach.

    The SMS opener presents ``offered_slots`` numbered ``1..N`` with
    ``N+1 = "None of those work"``. A plain-digit reply IS the
    customer's decision; the LLM's prose acknowledgement of it does
    not need to be parsed.

    - ``"1".."N"`` → ``("book", SlotId)`` for that index.
    - ``"N+1"``   → ``("decline", None)`` (none of those work).
    - anything else (multi-char, non-digit) → ``("none", None)``.
    """

    cleaned = body.strip()
    if not cleaned.isdigit():
        return ("none", None)
    idx = int(cleaned) - 1
    n = len(case.offered_slots)
    if 0 <= idx < n:
        return ("book", case.offered_slots[idx].id)
    if idx == n:
        return ("decline", None)
    return ("none", None)


def _llm_reply_action(case: Case, *, stage: CallStage) -> PlaceCall:
    """Build the :class:`PlaceCall` action that asks the driver to
    compose-and-send one LLM-driven reply for the current case.

    No attempt-count increment: this is a turn within the existing
    conversation, not a fresh dial attempt. The attempt counter only
    moves on stage transitions and explicit retries.
    """

    return PlaceCall(
        case_id=case.case_id,
        stage=stage,
        attempt_number=max(case.attempt_count, 1),
    )


def _on_inbound_sms_received(case: Case, signal: InboundSmsReceived) -> CaseDecision:
    """Route one inbound SMS body into the appropriate state transition.

    The driver has already appended the customer's turn to the SMS
    history store by the time this runs; the LLM reply (when one is
    needed) will see it on its next ``HistoryStore.load``.
    """

    normalized = _normalize_inbound(signal.body)

    # ---- Post-booking reminder stages ------------------------------------

    if case.state == CaseState.INITIAL_REMINDER_SENT:
        bo = _classify_post_booking_reply(normalized)
        if bo is not None:
            decision = _post_booking_touchpoint_outcome(case, bo=bo, stage="initial")
            if decision is not None:
                return decision
        return CaseDecision(
            next_state=case.state,
            actions=(
                _llm_reply_action(case, stage=CallStage.INITIAL_REMINDER),
                RecordEvent(
                    case_id=case.case_id,
                    event="case.initial_reminder.free_text",
                    detail=_audit_summary(signal),
                ),
            ),
            reason="initial_reminder_free_text_reply",
        )

    if case.state == CaseState.FINAL_REMINDER_SENT:
        bo = _classify_post_booking_reply(normalized)
        if bo is not None:
            decision = _post_booking_touchpoint_outcome(case, bo=bo, stage="final")
            if decision is not None:
                return decision
        return CaseDecision(
            next_state=case.state,
            actions=(
                _llm_reply_action(case, stage=CallStage.FINAL_REMINDER),
                RecordEvent(
                    case_id=case.case_id,
                    event="case.final_reminder.free_text",
                    detail=_audit_summary(signal),
                ),
            ),
            reason="final_reminder_free_text_reply",
        )

    # ---- Outreach (or rescheduling, which is outreach-shaped) -------------

    if case.state in _OUTREACH_STATES:
        kind, picked = _interpret_outreach_digit(signal.body, case=case)
        if kind == "book" and picked is not None:
            display = _resolve_booked_slot_display(case, picked)
            return CaseDecision(
                next_state=CaseState.CONFIRMING_WITH_DEALER,
                actions=(
                    RequestDealerConfirmation(case_id=case.case_id, slot_id=picked),
                    _llm_reply_action(case, stage=CallStage.OUTREACH),
                    RecordEvent(
                        case_id=case.case_id,
                        event="case.slot.picked",
                        detail=(
                            f"customer picked slot {picked} via SMS; "
                            "confirming with dealer"
                        ),
                    ),
                ),
                patch=CasePatch(
                    booked_slot_id=picked,
                    booked_slot_display=display or None,
                ),
                reason="outreach_booked_via_inbound",
            )
        if kind == "decline":
            return CaseDecision(
                next_state=CaseState.DECLINED,
                actions=(
                    CancelTimer(
                        case_id=case.case_id, name=TIMER_INITIAL_REMINDER
                    ),
                    CancelTimer(case_id=case.case_id, name=TIMER_FINAL_REMINDER),
                    CancelTimer(case_id=case.case_id, name=TIMER_END_OF_DAY),
                    RecordEvent(
                        case_id=case.case_id,
                        event="case.closed.declined",
                        detail="customer picked none-of-those-work option via SMS",
                    ),
                ),
                reason="outreach_declined_via_inbound",
            )
        # Outreach hard-cancel keyword (CANCEL spelled out, not a digit).
        if normalized in _OUTREACH_CANCEL:
            return CaseDecision(
                next_state=CaseState.DECLINED,
                actions=(
                    CancelTimer(
                        case_id=case.case_id, name=TIMER_INITIAL_REMINDER
                    ),
                    CancelTimer(case_id=case.case_id, name=TIMER_FINAL_REMINDER),
                    CancelTimer(case_id=case.case_id, name=TIMER_END_OF_DAY),
                    RecordEvent(
                        case_id=case.case_id,
                        event="case.closed.declined",
                        detail=f"customer cancelled during outreach: {normalized}",
                    ),
                ),
                reason="outreach_cancel_keyword",
            )
        # Free text: ask the LLM to keep the conversation going.
        return CaseDecision(
            next_state=case.state,
            actions=(
                _llm_reply_action(case, stage=CallStage.OUTREACH),
                RecordEvent(
                    case_id=case.case_id,
                    event="case.outreach.free_text",
                    detail=_audit_summary(signal),
                ),
            ),
            reason="outreach_free_text_reply",
        )

    # ---- Anywhere else (CONFIRMING_WITH_DEALER, reminder-DUE windows,
    # SHOWED, AWAITING_FEEDBACK) — log + ignore. We don't crash, and we
    # don't trigger a new LLM round-trip; the customer may text us
    # mid-flight while we're awaiting an external event.

    return CaseDecision(
        next_state=case.state,
        actions=(
            RecordEvent(
                case_id=case.case_id,
                event="case.inbound.unhandled",
                detail=(
                    f"inbound SMS arrived in {case.state.value} — "
                    f"{_audit_summary(signal)}"
                ),
            ),
        ),
        reason=f"inbound_unhandled_at_{case.state.value}",
    )


def _audit_summary(signal: InboundSmsReceived, *, limit: int = 120) -> str:
    """Render an inbound signal as a one-line audit detail string."""

    clipped = signal.body.strip().replace("\n", " ")
    if len(clipped) > limit:
        clipped = clipped[: limit - 1] + "\u2026"
    return f"from={signal.from_phone} sid={signal.message_sid} body={clipped!r}"


def _on_dealer_slots_listed(case: Case, signal: DealerSlotsListed) -> CaseDecision:
    """Dealer returned slot list — used during RESCHEDULING to drive the
    second outreach call.

    During the *initial* outreach the slots come from the trigger
    (operator pre-selected), not from a port round-trip; that branch is
    a no-op at the case level.
    """

    if case.state != CaseState.RESCHEDULING:
        return _ignored(case, f"slots_listed_irrelevant_at_{case.state.value}")
    # Fire a fresh outreach-shaped call carrying the new slots. The
    # CallManager picks them up from the case (the driver passes the
    # case snapshot into PlaceCall.start).
    return CaseDecision(
        next_state=CaseState.CONTACTING_CUSTOMER,
        actions=(
            PlaceCall(
                case_id=case.case_id,
                stage=CallStage.OUTREACH,
                attempt_number=_next_attempt_number(case),
            ),
            RecordEvent(
                case_id=case.case_id,
                event="case.reschedule.slots_listed",
                detail=f"received {len(signal.slots)} replacement slots",
            ),
        ),
        patch=CasePatch(increment_attempt_count=True),
        reason="rescheduling_slots_received",
    )


def _on_dealer_confirmed(
    case: Case, signal: DealerConfirmed, *, now: datetime
) -> CaseDecision:
    """Dealer confirmed the slot. Arm the reminder timer(s) and land
    the case in the appropriate ``*_DUE`` state.

    Two paths:

    - **Far slot** (≥ 24h away): land in ``INITIAL_REMINDER_DUE`` and
      arm BOTH ``TIMER_INITIAL_REMINDER`` (T-24h) AND
      ``TIMER_FINAL_REMINDER`` (slot-2h) up front. Arming both means
      the final reminder fires on its own schedule regardless of how
      the customer reacted (or didn't react) to the initial reminder.
      The state stays in ``INITIAL_REMINDER_SENT`` between the two
      touchpoints — no anticipatory state transition is needed.
    - **Near slot** (< 24h away): skip the initial reminder, land in
      ``FINAL_REMINDER_DUE`` and arm only ``TIMER_FINAL_REMINDER``.
    """

    if case.state != CaseState.CONFIRMING_WITH_DEALER:
        return _ignored(case, f"dealer_confirmed_irrelevant_at_{case.state.value}")

    # Find the slot's start time. The reducer needs the slot timestamp
    # to decide which reminder path to take and when to arm timers.
    slot_starts_at = _lookup_slot_start(case, signal.slot_id)
    if slot_starts_at is None:
        # Defensive: dealer confirmed something we don't know about.
        return _ignored(case, "dealer_confirmed_unknown_slot")

    confirmed_display = _resolve_booked_slot_display(case, signal.slot_id)
    confirm_patch = CasePatch(
        booked_slot_id=signal.slot_id,
        booked_slot_display=confirmed_display or None,
    )
    final_at = slot_starts_at - FINAL_REMINDER_LEAD

    if _initial_reminder_lead_satisfied(now, slot_starts_at):
        initial_at = slot_starts_at - INITIAL_REMINDER_LEAD
        return CaseDecision(
            next_state=CaseState.INITIAL_REMINDER_DUE,
            actions=(
                ScheduleTimer(
                    case_id=case.case_id,
                    name=TIMER_INITIAL_REMINDER,
                    fire_at=initial_at,
                ),
                ScheduleTimer(
                    case_id=case.case_id,
                    name=TIMER_FINAL_REMINDER,
                    fire_at=final_at,
                ),
                RecordEvent(
                    case_id=case.case_id,
                    event="case.booking.confirmed",
                    detail=(
                        f"dealer confirmed; initial reminder armed for "
                        f"{initial_at.isoformat()}, final reminder armed for "
                        f"{final_at.isoformat()}"
                    ),
                ),
            ),
            patch=confirm_patch,
            reason="confirmed_awaiting_initial_reminder",
        )

    # Less than INITIAL_REMINDER_LEAD away — skip initial reminder.
    return CaseDecision(
        next_state=CaseState.FINAL_REMINDER_DUE,
        actions=(
            ScheduleTimer(
                case_id=case.case_id,
                name=TIMER_FINAL_REMINDER,
                fire_at=final_at,
            ),
            RecordEvent(
                case_id=case.case_id,
                event="case.booking.confirmed_no_initial_reminder",
                detail=(
                    "dealer confirmed; slot is under initial-reminder lead, "
                    f"skipping initial reminder; final reminder armed for "
                    f"{final_at.isoformat()}"
                ),
            ),
        ),
        patch=confirm_patch,
        reason="confirmed_skip_initial_reminder",
    )


def _on_dealer_rejected(case: Case, signal: DealerRejected) -> CaseDecision:
    """Dealer rejected the proposed slot — re-propose to the customer."""

    if case.state != CaseState.CONFIRMING_WITH_DEALER:
        return _ignored(case, f"dealer_rejected_irrelevant_at_{case.state.value}")
    return CaseDecision(
        next_state=CaseState.RESCHEDULING,
        actions=(
            RequestDealerSlots(case_id=case.case_id),
            RecordEvent(
                case_id=case.case_id,
                event="case.dealer.rejected",
                level="warn",
                detail=f"dealer rejected slot {signal.slot_id}: {signal.reason or 'no reason'}",
            ),
        ),
        reason="dealer_rejected",
    )


def _on_initial_reminder_due(case: Case) -> CaseDecision:
    """T-24h timer fired — send the initial reminder."""

    if case.state != CaseState.INITIAL_REMINDER_DUE:
        return _ignored(
            case, f"initial_reminder_due_irrelevant_at_{case.state.value}"
        )
    return CaseDecision(
        next_state=CaseState.INITIAL_REMINDER_SENT,
        actions=(
            PlaceCall(
                case_id=case.case_id,
                stage=CallStage.INITIAL_REMINDER,
                attempt_number=_next_attempt_number(case),
            ),
            RecordEvent(
                case_id=case.case_id,
                event="case.initial_reminder.sending",
                detail="placing initial reminder (T-24h)",
            ),
        ),
        patch=CasePatch(increment_attempt_count=True),
        reason="initial_reminder_due",
    )


def _on_final_reminder_due(case: Case) -> CaseDecision:
    """Slot-2h timer fired — send the final (day-of) reminder.

    Allowed sources:

    - ``FINAL_REMINDER_DUE`` — the normal path: customer either
      confirmed the initial reminder, was silent through it, or the
      booking was inside the initial-reminder lead and we skipped
      straight here at dealer-confirm time.
    - ``INITIAL_REMINDER_DUE`` / ``INITIAL_REMINDER_SENT`` — defensive:
      both timers are armed up front at dealer-confirm; if the final
      timer fires before the initial conversation has resolved, we
      still advance to the day-of touchpoint.
    """

    if case.state not in (
        CaseState.FINAL_REMINDER_DUE,
        CaseState.INITIAL_REMINDER_DUE,
        CaseState.INITIAL_REMINDER_SENT,
    ):
        return _ignored(case, f"final_reminder_due_irrelevant_at_{case.state.value}")
    return CaseDecision(
        next_state=CaseState.FINAL_REMINDER_SENT,
        actions=(
            PlaceCall(
                case_id=case.case_id,
                stage=CallStage.FINAL_REMINDER,
                attempt_number=_next_attempt_number(case),
            ),
            RecordEvent(
                case_id=case.case_id,
                event="case.final_reminder.sending",
                detail="placing final reminder (day-of)",
            ),
        ),
        patch=CasePatch(increment_attempt_count=True),
        reason="final_reminder_due",
    )


def _on_timer_fired(case: Case, signal: TimerFired) -> CaseDecision:
    """Generic named timer fired (silence nudges, etc.).

    Phase 3 doesn't define any silence nudges yet; the handler exists so
    the dispatch is total and Phase 4 can wire nudges in without
    reshuffling the reducer.
    """

    return _ignored(case, f"timer_unhandled:{signal.name}")


def _on_vehicle_entered(case: Case) -> CaseDecision:
    """Vehicle crossed into the dealer geofence.

    Normally fired from ``FINAL_REMINDER_SENT``. Defensively accepted
    from ``FINAL_REMINDER_DUE`` so an early arrival (vehicle pulls in
    before our slot-2h day-of nudge has fired) still advances to
    ``SHOWED`` rather than stranding the case.
    """

    if case.state not in (
        CaseState.FINAL_REMINDER_SENT,
        CaseState.FINAL_REMINDER_DUE,
    ):
        return _ignored(case, f"geofence_in_irrelevant_at_{case.state.value}")
    return CaseDecision(
        next_state=CaseState.SHOWED,
        actions=(
            CancelTimer(case_id=case.case_id, name=TIMER_FINAL_REMINDER),
            CancelTimer(case_id=case.case_id, name=TIMER_END_OF_DAY),
            RecordEvent(
                case_id=case.case_id,
                event="case.geofence.entered",
                detail="vehicle entered dealer geofence",
            ),
        ),
        reason="vehicle_arrived",
    )


def _on_vehicle_exited(case: Case) -> CaseDecision:
    """Vehicle crossed out of the dealer geofence — service event complete.

    Per the dictated state machine: SHOWED + geofence-out → terminal.
    No feedback touchpoint; the customer is not pestered for a survey
    by the core workflow.

    Two log events are emitted so the audit trail is unambiguous:

    - ``case.geofence.exited`` — the physical-world signal (mirror of
      ``case.geofence.entered`` on the way in).
    - ``case.closed.service_event_complete`` — the terminal marker that
      lets operators / log scrapers see "this case ended cleanly via the
      happy path" without parsing geofence detail strings.
    """

    if case.state != CaseState.SHOWED:
        return _ignored(case, f"geofence_out_irrelevant_at_{case.state.value}")
    return CaseDecision(
        next_state=CaseState.COMPLETED,
        actions=(
            CancelTimer(case_id=case.case_id, name=TIMER_END_OF_DAY),
            RecordEvent(
                case_id=case.case_id,
                event="case.geofence.exited",
                detail="vehicle exited dealer geofence",
            ),
            RecordEvent(
                case_id=case.case_id,
                event="case.closed.service_event_complete",
                detail="service event complete; case closed via geofence-out",
            ),
        ),
        reason="service_event_complete",
    )


def _lookup_slot_start(case: Case, slot_id: SlotId) -> UtcDatetime | None:
    """Find a slot's start time on the case.

    Searches both the originally-offered slots (from the trigger) and
    any later additions. Returns ``None`` if the id isn't on the case;
    the caller treats that as "ignore + audit".
    """

    for slot in case.offered_slots:
        if slot.id == slot_id:
            return slot.starts_at
    return None


def _resolve_booked_slot_display(
    case: Case,
    slot_id: SlotId,
    *,
    from_outcome: str = "",
) -> str:
    """Pick the customer-facing appointment label for a booking patch."""

    if from_outcome.strip():
        return from_outcome.strip()
    if case.booked_slot_display.strip():
        return case.booked_slot_display.strip()
    return lookup_slot_display(offered_slots=case.offered_slots, slot_id=slot_id)


_PostBookingStage = Literal["initial", "final"]


def _post_booking_touchpoint_outcome(
    case: Case, *, bo: str | None, stage: _PostBookingStage
) -> CaseDecision | None:
    """Map confirmed / cancelled / rescheduled from either reminder stage.

    Symmetric across the two reminder touchpoints:

    - ``initial`` reminder (``INITIAL_REMINDER_SENT``) — confirm is a
      no-op. The case sits in ``INITIAL_REMINDER_SENT`` until the
      final-reminder timer fires on the day of the appointment.
    - ``final`` reminder (``FINAL_REMINDER_SENT``) — confirm is a
      no-op. The case sits in ``FINAL_REMINDER_SENT`` awaiting the
      geofence or end-of-business-day.

    Silence (the ``inconclusive`` business outcome) is handled by
    ``_on_incomplete_outcome``; at both reminder stages it is also a
    no-op for the same reason — the next touchpoint (final reminder or
    geofence/EoD) fires on its own schedule and decides the outcome.

    Cancel and reschedule outcomes are identical at both stages: cancel
    closes the case after defensively cancelling the final-reminder
    timer; reschedule routes through one-shot rescheduling.
    """

    if bo == "confirmed":
        if stage == "initial":
            return CaseDecision(
                next_state=CaseState.INITIAL_REMINDER_SENT,
                actions=(
                    RecordEvent(
                        case_id=case.case_id,
                        event="case.initial_reminder.confirmed",
                        detail=(
                            "customer confirmed at initial reminder; awaiting "
                            "final reminder on day of appointment"
                        ),
                    ),
                ),
                reason="initial_reminder_confirmed",
            )
        # Final reminder confirmation: no state change; wait for
        # geofence / EoD to drive the terminal outcome.
        return CaseDecision(
            next_state=CaseState.FINAL_REMINDER_SENT,
            actions=(
                RecordEvent(
                    case_id=case.case_id,
                    event="case.final_reminder.confirmed",
                    detail="customer confirmed at final reminder; awaiting arrival",
                ),
            ),
            reason="final_reminder_confirmed",
        )
    if bo == "cancelled":
        return CaseDecision(
            next_state=CaseState.CANCELLED,
            actions=(
                CancelTimer(case_id=case.case_id, name=TIMER_FINAL_REMINDER),
                CancelTimer(case_id=case.case_id, name=TIMER_END_OF_DAY),
                RecordEvent(
                    case_id=case.case_id,
                    event="case.closed.cancelled",
                    detail=f"customer cancelled at {stage}_reminder",
                ),
            ),
            reason=f"{stage}_reminder_cancelled",
        )
    if bo == "rescheduled":
        if case.reschedule_count >= MAX_RESCHEDULES:
            return CaseDecision(
                next_state=CaseState.RESCHEDULE_FAILED,
                actions=(
                    CancelTimer(case_id=case.case_id, name=TIMER_FINAL_REMINDER),
                    CancelTimer(case_id=case.case_id, name=TIMER_END_OF_DAY),
                    RecordEvent(
                        case_id=case.case_id,
                        event="case.closed.reschedule_failed",
                        level="warn",
                        detail=(
                            "customer requested a second reschedule at "
                            f"{stage}_reminder"
                        ),
                    ),
                ),
                reason="reschedule_one_shot_exhausted",
            )
        return CaseDecision(
            next_state=CaseState.RESCHEDULING,
            actions=(
                CancelTimer(case_id=case.case_id, name=TIMER_FINAL_REMINDER),
                CancelTimer(case_id=case.case_id, name=TIMER_END_OF_DAY),
                RequestDealerSlots(case_id=case.case_id),
                RecordEvent(
                    case_id=case.case_id,
                    event=f"case.{stage}_reminder.rescheduling",
                    detail="customer wants a new slot",
                ),
            ),
            patch=CasePatch(increment_reschedule_count=True),
            reason=f"{stage}_reminder_rescheduling",
        )
    return None


__all__ = [
    "FINAL_REMINDER_LEAD",
    "INITIAL_REMINDER_LEAD",
    "MAX_CALL_ATTEMPTS",
    "MAX_RESCHEDULES",
    "TIMER_END_OF_DAY",
    "TIMER_FINAL_REMINDER",
    "TIMER_INITIAL_REMINDER",
    "CaseDecision",
    "CasePatch",
    "decide_next_case_state",
]
