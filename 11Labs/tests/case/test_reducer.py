"""Exhaustive tests for ``decide_next_case_state``.

The reducer is a pure function with a giant state x signal cross
product. These tests enforce three things:

1. **Totality** — every (CaseState, CaseSignal) combination produces a
   decision without raising. Combinations that aren't meaningful return
   a no-op decision with a ``reason`` starting ``"ignored:"``.

2. **Happy-path transitions** — the lifecycle paths the user signed off
   on (trigger → outreach → confirmation → reminder → day-of → showed →
   feedback → completed, plus every terminal branch) produce the
   expected next state + actions + patch.

3. **Universal opt-out** — ``CustomerOptedOut`` closes any non-terminal
   case to ``OPTED_OUT``.

Helper builders live at module top so the per-test code reads as
narrowly as possible. Each builder accepts only the fields a test is
varying; sensible UTC defaults handle the rest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from guidepoint.case import (
    CallOutcome,
    Case,
    CaseId,
    CaseState,
    OfferedSlot,
    ServiceEvent,
    SlotId,
    TriggerId,
)
from guidepoint.case._actions import (
    CallStage,
    CancelTimer,
    PlaceCall,
    RecordEvent,
    RequestDealerConfirmation,
    RequestDealerSlots,
    ScheduleTimer,
)
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
from guidepoint.case._signals import (
    BusinessHoursClosed,
    BusinessHoursOpened,
    CallEnded,
    CaseCreated,
    CustomerOptedIn,
    CustomerOptedOut,
    DealerConfirmed,
    DealerRejected,
    DealerSlotsListed,
    EndOfBusinessDayReached,
    FinalReminderDue,
    InboundSmsReceived,
    InitialReminderDue,
    TimerFired,
    VehicleEnteredDealer,
    VehicleExitedDealer,
)

from tests.case._helpers import sample_case

# ---------------------------------------------------------------------------
# Time anchors — every test pins these so behaviour is reproducible.
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
SLOT_FAR = datetime(2026, 5, 15, 13, 30, tzinfo=UTC)  # 5 days out (> initial lead)
SLOT_NEAR = NOW + timedelta(hours=6)  # < initial lead
SLOT_A: SlotId = SlotId("slot_a")
SLOT_B: SlotId = SlotId("slot_b")


# ---------------------------------------------------------------------------
# Case + signal builders
# ---------------------------------------------------------------------------


def _case(
    *,
    state: CaseState = CaseState.CREATED,
    attempt_count: int = 0,
    reschedule_count: int = 0,
    slot_starts_at: datetime = SLOT_FAR,
    booked_slot_id: SlotId | None = None,
) -> Case:
    base = sample_case()
    return base.model_copy(
        update={
            "state": state,
            "attempt_count": attempt_count,
            "reschedule_count": reschedule_count,
            "offered_slots": (
                OfferedSlot(id=SLOT_A, starts_at=slot_starts_at, display="A"),
            ),
            "booked_slot_id": booked_slot_id,
        }
    )


def _outcome(
    *,
    business_outcome: str | None = None,
    booked_slot_id: SlotId | None = None,
    result: str = "answered",
) -> CallOutcome:
    return CallOutcome(
        result=result,  # type: ignore[arg-type]
        business_outcome=business_outcome,  # type: ignore[arg-type]
        booked_slot_id=booked_slot_id,
        elevenlabs_conversation_id="conv",
        started_at=NOW - timedelta(minutes=2),
        ended_at=NOW,
        duration_seconds=120.0,
        transcript="t",
    )


def _call_ended(
    case: Case,
    *,
    business_outcome: str | None,
    booked_slot_id: SlotId | None = None,
    result: str = "answered",
) -> CallEnded:
    return CallEnded(
        timestamp=NOW,
        case_id=case.case_id,
        outcome=_outcome(
            business_outcome=business_outcome,
            booked_slot_id=booked_slot_id,
            result=result,
        ),
    )


# All non-terminal states the reducer reasons about.
NON_TERMINAL_STATES: tuple[CaseState, ...] = tuple(
    s for s in CaseState if not s.is_terminal
)
TERMINAL_STATES: tuple[CaseState, ...] = tuple(s for s in CaseState if s.is_terminal)


# Every "world" + "indirect" signal we want to fire at every state when
# proving totality. ``CallEnded`` is excluded from this list because it
# requires an outcome and is exhaustively covered in its own block.
def _every_simple_signal(case: Case) -> tuple[Any, ...]:
    return (
        CaseCreated(timestamp=NOW, case_id=case.case_id),
        BusinessHoursOpened(timestamp=NOW),
        BusinessHoursClosed(timestamp=NOW),
        EndOfBusinessDayReached(timestamp=NOW),
        DealerSlotsListed(timestamp=NOW, case_id=case.case_id, slots=()),
        DealerConfirmed(timestamp=NOW, case_id=case.case_id, slot_id=SLOT_A),
        DealerRejected(timestamp=NOW, case_id=case.case_id, slot_id=SLOT_A, reason="x"),
        InitialReminderDue(timestamp=NOW, case_id=case.case_id),
        FinalReminderDue(timestamp=NOW, case_id=case.case_id),
        InboundSmsReceived(
            timestamp=NOW,
            case_id=case.case_id,
            from_phone=case.customer.phone,
            body="hello",
            message_sid="SM_test",
        ),
        TimerFired(timestamp=NOW, case_id=case.case_id, name="something"),
        VehicleEnteredDealer(timestamp=NOW, vehicle_vin=case.vehicle.vin),
        VehicleExitedDealer(timestamp=NOW, vehicle_vin=case.vehicle.vin),
        CustomerOptedOut(timestamp=NOW, customer_phone=case.customer.phone),
        CustomerOptedIn(timestamp=NOW, customer_phone=case.customer.phone),
    )


# ---------------------------------------------------------------------------
# Totality: every (state, signal) combination produces a CaseDecision.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", list(CaseState))
def test_every_state_handles_every_simple_signal(state: CaseState) -> None:
    case = _case(state=state)
    for signal in _every_simple_signal(case):
        decision = decide_next_case_state(case, signal, now=NOW)
        assert isinstance(decision, CaseDecision)
        assert decision.reason  # non-empty by Field(min_length=1)


@pytest.mark.parametrize("state", list(CaseState))
@pytest.mark.parametrize(
    "business_outcome",
    [
        None,
        "booked",
        "declined",
        "confirmed",
        "rescheduled",
        "cancelled",
        "feedback",
        "opted_out",
        "inconclusive",
    ],
)
def test_every_state_handles_every_call_ended(
    state: CaseState, business_outcome: str | None
) -> None:
    case = _case(state=state)
    signal = _call_ended(
        case,
        business_outcome=business_outcome,
        booked_slot_id=SLOT_A if business_outcome in {"booked", "rescheduled"} else None,
    )
    decision = decide_next_case_state(case, signal, now=NOW)
    assert isinstance(decision, CaseDecision)
    assert decision.reason


# ---------------------------------------------------------------------------
# Terminal cases ignore everything (except is no-op).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", list(TERMINAL_STATES))
def test_terminal_states_ignore_all_signals(state: CaseState) -> None:
    case = _case(state=state)
    for signal in _every_simple_signal(case):
        decision = decide_next_case_state(case, signal, now=NOW)
        assert decision.next_state == state
        assert decision.actions == ()
        assert decision.reason.startswith("ignored:")


# ---------------------------------------------------------------------------
# Opt-out is universal across non-terminal states.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", list(NON_TERMINAL_STATES))
def test_opt_out_closes_any_non_terminal_case(state: CaseState) -> None:
    case = _case(state=state)
    signal = CustomerOptedOut(timestamp=NOW, customer_phone=case.customer.phone)
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.OPTED_OUT
    assert decision.reason == "opt_out"
    assert any(isinstance(a, RecordEvent) for a in decision.actions)
    # All three named timers are defensively cancelled.
    cancelled = {a.name for a in decision.actions if isinstance(a, CancelTimer)}
    assert cancelled == {
        TIMER_INITIAL_REMINDER,
        TIMER_FINAL_REMINDER,
        TIMER_END_OF_DAY,
    }


def test_opt_in_does_not_move_case() -> None:
    case = _case(state=CaseState.CONTACTING_CUSTOMER)
    signal = CustomerOptedIn(timestamp=NOW, customer_phone=case.customer.phone)
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.CONTACTING_CUSTOMER
    assert decision.is_noop
    assert decision.reason == "ignored:opt_in_does_not_revive_case"


# ---------------------------------------------------------------------------
# Happy-path: CREATED -> CONTACTING via CaseCreated lifecycle signal.
# ---------------------------------------------------------------------------


def test_case_created_starts_outreach_from_created() -> None:
    case = _case(state=CaseState.CREATED)
    decision = decide_next_case_state(
        case,
        CaseCreated(timestamp=NOW, case_id=case.case_id),
        now=NOW,
    )
    assert decision.next_state == CaseState.CONTACTING_CUSTOMER
    assert decision.patch == CasePatch(increment_attempt_count=True)
    assert decision.reason == "case_created"
    calls = [a for a in decision.actions if isinstance(a, PlaceCall)]
    assert len(calls) == 1
    assert calls[0].stage == CallStage.OUTREACH
    assert calls[0].attempt_number == 1


def test_case_created_irrelevant_after_outreach_started() -> None:
    case = _case(state=CaseState.CONTACTING_CUSTOMER, attempt_count=1)
    decision = decide_next_case_state(
        case,
        CaseCreated(timestamp=NOW, case_id=case.case_id),
        now=NOW,
    )
    assert decision.is_noop
    assert decision.reason.startswith("ignored:case_created_irrelevant")


def test_business_hours_signals_are_ignored_by_reducer() -> None:
    """The reducer has no opinion on hours — the queue worker owns that gate."""
    case = _case(state=CaseState.CREATED)
    for signal in (
        BusinessHoursOpened(timestamp=NOW),
        BusinessHoursClosed(timestamp=NOW),
    ):
        decision = decide_next_case_state(case, signal, now=NOW)
        assert decision.is_noop, signal
        assert "hours_handled_by_outbound_queue" in decision.reason, signal


# ---------------------------------------------------------------------------
# Outreach call outcomes.
# ---------------------------------------------------------------------------


def test_outreach_booked_routes_to_dealer_confirmation() -> None:
    case = _case(state=CaseState.CONTACTING_CUSTOMER, attempt_count=1)
    signal = _call_ended(case, business_outcome="booked", booked_slot_id=SLOT_A)
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.CONFIRMING_WITH_DEALER
    assert decision.patch.booked_slot_id == SLOT_A
    confirms = [
        a for a in decision.actions if isinstance(a, RequestDealerConfirmation)
    ]
    assert confirms == [
        RequestDealerConfirmation(case_id=case.case_id, slot_id=SLOT_A)
    ]


def test_outreach_booked_without_slot_is_ignored() -> None:
    case = _case(state=CaseState.CONTACTING_CUSTOMER, attempt_count=1)
    signal = _call_ended(case, business_outcome="booked", booked_slot_id=None)
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.is_noop
    assert decision.reason == "ignored:booked_without_slot_id"


def test_outreach_declined_terminates() -> None:
    case = _case(state=CaseState.CONTACTING_CUSTOMER, attempt_count=1)
    signal = _call_ended(case, business_outcome="declined")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.DECLINED
    assert decision.reason == "outreach_declined"


def test_outreach_incomplete_first_attempt_retries() -> None:
    case = _case(state=CaseState.CONTACTING_CUSTOMER, attempt_count=1)
    signal = _call_ended(case, business_outcome="inconclusive", result="no_answer")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.CONTACTING_CUSTOMER
    assert decision.patch == CasePatch(increment_attempt_count=True)
    calls = [a for a in decision.actions if isinstance(a, PlaceCall)]
    assert len(calls) == 1
    assert calls[0].attempt_number == 2
    assert decision.reason == "incomplete_retry"


def test_outreach_incomplete_second_attempt_abandons() -> None:
    case = _case(state=CaseState.CONTACTING_CUSTOMER, attempt_count=MAX_CALL_ATTEMPTS)
    signal = _call_ended(case, business_outcome="inconclusive", result="no_answer")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.ABANDONED
    assert decision.reason == "outreach_abandoned"


def test_outreach_opt_out_via_call_closes() -> None:
    case = _case(state=CaseState.CONTACTING_CUSTOMER, attempt_count=1)
    signal = _call_ended(case, business_outcome="opted_out")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.OPTED_OUT


# ---------------------------------------------------------------------------
# Dealer confirmation.
# ---------------------------------------------------------------------------


def test_dealer_confirmed_far_slot_arms_both_reminders() -> None:
    """Far slot: case lands in ``INITIAL_REMINDER_DUE`` and BOTH timers
    are armed up front. Arming the final reminder at confirm time means
    silence on the initial reminder still drives the day-of nudge."""
    case = _case(
        state=CaseState.CONFIRMING_WITH_DEALER,
        slot_starts_at=SLOT_FAR,
        booked_slot_id=SLOT_A,
    )
    signal = DealerConfirmed(timestamp=NOW, case_id=case.case_id, slot_id=SLOT_A)
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.INITIAL_REMINDER_DUE
    timers = {a.name: a.fire_at for a in decision.actions if isinstance(a, ScheduleTimer)}
    assert timers == {
        TIMER_INITIAL_REMINDER: SLOT_FAR - INITIAL_REMINDER_LEAD,
        TIMER_FINAL_REMINDER: SLOT_FAR - FINAL_REMINDER_LEAD,
    }


def test_dealer_confirmed_near_slot_skips_initial_reminder() -> None:
    """Near slot (< 24h): skip the initial reminder entirely. Only the
    final-reminder timer is armed; case lands in ``FINAL_REMINDER_DUE``."""
    case = _case(
        state=CaseState.CONFIRMING_WITH_DEALER,
        slot_starts_at=SLOT_NEAR,
        booked_slot_id=SLOT_A,
    )
    signal = DealerConfirmed(timestamp=NOW, case_id=case.case_id, slot_id=SLOT_A)
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.FINAL_REMINDER_DUE
    timers = [a for a in decision.actions if isinstance(a, ScheduleTimer)]
    assert len(timers) == 1
    assert timers[0].name == TIMER_FINAL_REMINDER
    assert timers[0].fire_at == SLOT_NEAR - FINAL_REMINDER_LEAD


def test_dealer_confirmed_unknown_slot_is_ignored() -> None:
    case = _case(state=CaseState.CONFIRMING_WITH_DEALER)
    signal = DealerConfirmed(timestamp=NOW, case_id=case.case_id, slot_id=SlotId("nope"))
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.is_noop
    assert decision.reason == "ignored:dealer_confirmed_unknown_slot"


def test_dealer_rejected_kicks_to_rescheduling() -> None:
    case = _case(state=CaseState.CONFIRMING_WITH_DEALER, booked_slot_id=SLOT_A)
    signal = DealerRejected(
        timestamp=NOW, case_id=case.case_id, slot_id=SLOT_A, reason="no tech"
    )
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.RESCHEDULING
    assert any(isinstance(a, RequestDealerSlots) for a in decision.actions)


# ---------------------------------------------------------------------------
# Initial reminder stage (T-24h).
# ---------------------------------------------------------------------------


def test_initial_reminder_due_places_call() -> None:
    case = _case(state=CaseState.INITIAL_REMINDER_DUE, attempt_count=1)
    decision = decide_next_case_state(
        case, InitialReminderDue(timestamp=NOW, case_id=case.case_id), now=NOW
    )
    assert decision.next_state == CaseState.INITIAL_REMINDER_SENT
    calls = [a for a in decision.actions if isinstance(a, PlaceCall)]
    assert len(calls) == 1
    assert calls[0].stage == CallStage.INITIAL_REMINDER


def test_initial_reminder_confirmed_is_a_no_op() -> None:
    """Customer confirms at the initial reminder → no state change.
    The final-reminder timer is already armed from dealer-confirm and
    fires on its own schedule; we never anticipate the day-of touchpoint
    by changing state here.
    """
    case = _case(state=CaseState.INITIAL_REMINDER_SENT, attempt_count=2)
    signal = _call_ended(case, business_outcome="confirmed")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.INITIAL_REMINDER_SENT
    # Confirm did not cancel the final-reminder timer.
    cancels = {a.name for a in decision.actions if isinstance(a, CancelTimer)}
    assert TIMER_FINAL_REMINDER not in cancels
    assert decision.reason == "initial_reminder_confirmed"


def test_initial_reminder_cancelled_terminates_and_clears_final_timer() -> None:
    case = _case(state=CaseState.INITIAL_REMINDER_SENT, attempt_count=2)
    signal = _call_ended(case, business_outcome="cancelled")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.CANCELLED
    cancels = {a.name for a in decision.actions if isinstance(a, CancelTimer)}
    assert TIMER_FINAL_REMINDER in cancels


def test_initial_reminder_rescheduled_enters_rescheduling() -> None:
    case = _case(
        state=CaseState.INITIAL_REMINDER_SENT, attempt_count=2, reschedule_count=0
    )
    signal = _call_ended(case, business_outcome="rescheduled")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.RESCHEDULING
    assert decision.patch.increment_reschedule_count is True
    assert any(isinstance(a, RequestDealerSlots) for a in decision.actions)
    cancels = {a.name for a in decision.actions if isinstance(a, CancelTimer)}
    assert TIMER_FINAL_REMINDER in cancels


def test_initial_reminder_silence_is_a_no_op_after_retry() -> None:
    """No response to the initial reminder is a no-op. The final
    reminder timer was armed at dealer-confirm and will fire on its own
    schedule on the day of the appointment; the case stays in
    ``INITIAL_REMINDER_SENT`` until then.
    """
    case = _case(
        state=CaseState.INITIAL_REMINDER_SENT, attempt_count=MAX_CALL_ATTEMPTS
    )
    signal = _call_ended(case, business_outcome="inconclusive", result="no_answer")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.INITIAL_REMINDER_SENT
    assert decision.reason == "ignored:initial_reminder_incomplete_awaiting_final"
    cancels = {a.name for a in decision.actions if isinstance(a, CancelTimer)}
    assert TIMER_FINAL_REMINDER not in cancels


# ---------------------------------------------------------------------------
# Rescheduling — dealer hands over a new slot list, customer picks one.
# ---------------------------------------------------------------------------


def test_rescheduling_dealer_slots_listed_triggers_new_outreach() -> None:
    case = _case(state=CaseState.RESCHEDULING, attempt_count=2)
    new_slot = OfferedSlot(id=SLOT_B, starts_at=SLOT_FAR, display="B")
    decision = decide_next_case_state(
        case,
        DealerSlotsListed(timestamp=NOW, case_id=case.case_id, slots=(new_slot,)),
        now=NOW,
    )
    assert decision.next_state == CaseState.CONTACTING_CUSTOMER
    assert decision.patch.increment_attempt_count is True


def test_rescheduling_booked_returns_to_dealer_confirmation() -> None:
    case = _case(state=CaseState.RESCHEDULING, attempt_count=3)
    signal = _call_ended(case, business_outcome="booked", booked_slot_id=SLOT_A)
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.CONFIRMING_WITH_DEALER
    assert decision.patch.booked_slot_id == SLOT_A


def test_rescheduling_cancelled_terminates() -> None:
    case = _case(state=CaseState.RESCHEDULING, attempt_count=3)
    signal = _call_ended(case, business_outcome="cancelled")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.CANCELLED


# ---------------------------------------------------------------------------
# Final reminder stage (day-of) + geofence.
# ---------------------------------------------------------------------------


def test_final_reminder_due_from_final_reminder_due_places_touchpoint() -> None:
    case = _case(state=CaseState.FINAL_REMINDER_DUE, attempt_count=2)
    decision = decide_next_case_state(
        case, FinalReminderDue(timestamp=NOW, case_id=case.case_id), now=NOW
    )
    assert decision.next_state == CaseState.FINAL_REMINDER_SENT
    calls = [a for a in decision.actions if isinstance(a, PlaceCall)]
    assert calls[0].stage == CallStage.FINAL_REMINDER


def test_final_reminder_due_from_initial_reminder_due_also_advances() -> None:
    """Defensive: if the final-reminder timer fires before the initial
    reminder has been placed (rare clock skew), the case still
    advances to ``FINAL_REMINDER_SENT``."""
    case = _case(state=CaseState.INITIAL_REMINDER_DUE, attempt_count=1)
    decision = decide_next_case_state(
        case, FinalReminderDue(timestamp=NOW, case_id=case.case_id), now=NOW
    )
    assert decision.next_state == CaseState.FINAL_REMINDER_SENT


def test_final_reminder_due_from_initial_reminder_sent_also_advances() -> None:
    """Defensive: final-reminder timer fires while the initial reminder
    conversation is still open. Day-of still gets sent."""
    case = _case(state=CaseState.INITIAL_REMINDER_SENT, attempt_count=2)
    decision = decide_next_case_state(
        case, FinalReminderDue(timestamp=NOW, case_id=case.case_id), now=NOW
    )
    assert decision.next_state == CaseState.FINAL_REMINDER_SENT


def test_final_reminder_confirmed_holds_state_for_arrival() -> None:
    """Customer reconfirms at the final reminder → no state change;
    wait on the geofence (or EoD) to drive the terminal outcome."""
    case = _case(state=CaseState.FINAL_REMINDER_SENT, attempt_count=3)
    signal = _call_ended(case, business_outcome="confirmed")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.FINAL_REMINDER_SENT


def test_final_reminder_cancelled_terminates() -> None:
    case = _case(state=CaseState.FINAL_REMINDER_SENT, attempt_count=3)
    signal = _call_ended(case, business_outcome="cancelled")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.CANCELLED


def test_final_reminder_rescheduled_enters_rescheduling() -> None:
    case = _case(state=CaseState.FINAL_REMINDER_SENT, attempt_count=3, reschedule_count=0)
    signal = _call_ended(case, business_outcome="rescheduled")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.RESCHEDULING
    assert any(isinstance(a, RequestDealerSlots) for a in decision.actions)


def test_final_reminder_rescheduled_second_time_fails() -> None:
    case = _case(
        state=CaseState.FINAL_REMINDER_SENT,
        attempt_count=3,
        reschedule_count=MAX_RESCHEDULES,
    )
    signal = _call_ended(case, business_outcome="rescheduled")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.RESCHEDULE_FAILED


def test_vehicle_entered_dealer_advances_final_reminder_sent_to_showed() -> None:
    case = _case(state=CaseState.FINAL_REMINDER_SENT)
    decision = decide_next_case_state(
        case,
        VehicleEnteredDealer(timestamp=NOW, vehicle_vin=case.vehicle.vin),
        now=NOW,
    )
    assert decision.next_state == CaseState.SHOWED
    cancels = {a.name for a in decision.actions if isinstance(a, CancelTimer)}
    assert TIMER_END_OF_DAY in cancels


def test_vehicle_entered_dealer_from_final_reminder_due_also_shows() -> None:
    """Defensive: vehicle arrives before the day-of touchpoint has fired."""
    case = _case(state=CaseState.FINAL_REMINDER_DUE)
    decision = decide_next_case_state(
        case,
        VehicleEnteredDealer(timestamp=NOW, vehicle_vin=case.vehicle.vin),
        now=NOW,
    )
    assert decision.next_state == CaseState.SHOWED


def test_vehicle_exited_dealer_completes_case_no_feedback_sms() -> None:
    """Per the dictated state machine: SHOWED + geofence-out → terminal.

    No feedback touchpoint. The customer is not asked to fill out a
    survey by the core workflow. The audit trail gets both the raw
    geofence event AND an explicit ``case.closed.service_event_complete``
    marker so operators can see the case ended cleanly without
    parsing geofence detail strings.
    """

    case = _case(state=CaseState.SHOWED, attempt_count=3)
    decision = decide_next_case_state(
        case,
        VehicleExitedDealer(timestamp=NOW, vehicle_vin=case.vehicle.vin),
        now=NOW,
    )
    assert decision.next_state == CaseState.COMPLETED
    calls = [a for a in decision.actions if isinstance(a, PlaceCall)]
    assert calls == []
    record_events = [a for a in decision.actions if isinstance(a, RecordEvent)]
    event_names = {a.event for a in record_events}
    assert "case.geofence.exited" in event_names
    assert "case.closed.service_event_complete" in event_names


def test_end_of_day_in_final_reminder_sent_marks_no_show() -> None:
    case = _case(state=CaseState.FINAL_REMINDER_SENT)
    decision = decide_next_case_state(
        case, EndOfBusinessDayReached(timestamp=NOW), now=NOW
    )
    assert decision.next_state == CaseState.NO_SHOW


def test_end_of_day_irrelevant_outside_final_reminder_sent() -> None:
    case = _case(state=CaseState.FINAL_REMINDER_DUE)
    decision = decide_next_case_state(
        case, EndOfBusinessDayReached(timestamp=NOW), now=NOW
    )
    assert decision.is_noop


# ---------------------------------------------------------------------------
# Feedback stage.
# ---------------------------------------------------------------------------


def test_feedback_captured_completes_case() -> None:
    case = _case(state=CaseState.AWAITING_FEEDBACK, attempt_count=4)
    signal = _call_ended(case, business_outcome="feedback")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.COMPLETED


def test_feedback_silence_after_retry_completes_case() -> None:
    case = _case(state=CaseState.AWAITING_FEEDBACK, attempt_count=MAX_CALL_ATTEMPTS)
    signal = _call_ended(case, business_outcome="inconclusive", result="no_answer")
    decision = decide_next_case_state(case, signal, now=NOW)
    assert decision.next_state == CaseState.COMPLETED
    assert decision.reason == "feedback_silence_completes"


# ---------------------------------------------------------------------------
# Decision struct invariants.
# ---------------------------------------------------------------------------


def test_decision_is_frozen_and_json_round_trippable() -> None:
    case = _case(state=CaseState.CONTACTING_CUSTOMER, attempt_count=1)
    signal = _call_ended(case, business_outcome="declined")
    decision = decide_next_case_state(case, signal, now=NOW)
    # Frozen.
    with pytest.raises(Exception):
        decision.next_state = CaseState.FINAL_REMINDER_DUE  # type: ignore[misc]
    # JSON round trip.
    restored = CaseDecision.model_validate_json(decision.model_dump_json())
    assert restored == decision
