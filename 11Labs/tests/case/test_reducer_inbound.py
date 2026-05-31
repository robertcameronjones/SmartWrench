"""Reducer tests for the ``InboundSmsReceived`` signal.

The SMS adapter is now a thin transport: every customer reply
arrives here as an :class:`InboundSmsReceived` signal carrying the
raw text. The reducer is the only place that decides what each text
means and what the case should do next.

These tests pin the contract:

- Outreach (and the reschedule-shaped second pass) — a digit reply in
  ``1..N`` books slot N; the ``N+1`` "none of those work" digit and
  the spelled-out ``CANCEL`` keyword close to DECLINED; free text
  emits one ``PlaceCall`` for an LLM-composed answer and stays.
- Reminder stages (initial / final) — the documented 1/2/3 options
  map to the symmetric confirm / reschedule / cancel transitions
  (1 → no state change with audit, 2 → RESCHEDULING + RequestDealerSlots,
  3 → CANCELLED). Free text emits one ``PlaceCall`` for an LLM reply.
- Other states — log + ignore (no transition, no LLM round-trip);
  the audit event is recorded so operators can see drift.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from guidepoint.case import (
    Case,
    CaseState,
    OfferedSlot,
    SlotId,
)
from guidepoint.case._actions import (
    CallStage,
    CancelTimer,
    PlaceCall,
    RecordEvent,
    RequestDealerConfirmation,
    RequestDealerSlots,
)
from guidepoint.case._reducer import decide_next_case_state
from guidepoint.case._signals import InboundSmsReceived

from tests.case._helpers import sample_case

NOW = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
SLOT_A: SlotId = SlotId("slot_a")
SLOT_B: SlotId = SlotId("slot_b")
SLOT_C: SlotId = SlotId("slot_c")


def _case(*, state: CaseState, slots: int = 3) -> Case:
    base = sample_case()
    offered = tuple(
        OfferedSlot(
            id=SlotId(f"slot_{chr(ord('a') + i)}"),
            starts_at=datetime(2026, 5, 12 + i, 13, 30, tzinfo=UTC),
            display=f"Day {i + 1}",
        )
        for i in range(slots)
    )
    return base.model_copy(update={"state": state, "offered_slots": offered, "attempt_count": 1})


def _inbound(case: Case, body: str, sid: str = "SM_in_1") -> InboundSmsReceived:
    return InboundSmsReceived(
        timestamp=NOW,
        case_id=case.case_id,
        from_phone=case.customer.phone,
        body=body,
        message_sid=sid,
    )


# ---------------------------------------------------------------------------
# Outreach digit picks.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        CaseState.CREATED,
        CaseState.CONTACTING_CUSTOMER,
        CaseState.RESCHEDULING,
    ],
)
def test_outreach_digit_books_picked_slot(state: CaseState) -> None:
    case = _case(state=state, slots=3)
    decision = decide_next_case_state(case, _inbound(case, "2"), now=NOW)

    assert decision.next_state == CaseState.BOOKED
    # Must request dealer confirmation for the picked slot AND emit one
    # PlaceCall for the LLM-composed ack the customer expects.
    confs = [a for a in decision.actions if isinstance(a, RequestDealerConfirmation)]
    assert len(confs) == 1
    assert confs[0].slot_id == SLOT_B
    calls = [a for a in decision.actions if isinstance(a, PlaceCall)]
    assert len(calls) == 1
    assert calls[0].stage == CallStage.OUTREACH
    assert decision.patch.booked_slot_id == SLOT_B


def test_outreach_decline_digit_closes_case() -> None:
    case = _case(state=CaseState.CONTACTING_CUSTOMER, slots=3)
    # 4 = "None of those work" (the N+1 slot).
    decision = decide_next_case_state(case, _inbound(case, "4"), now=NOW)

    assert decision.next_state == CaseState.DECLINED
    # Reminder + EoD timers are defensively cancelled so a no-show
    # touchpoint can't fire after the customer declined.
    cancelled = {a.name for a in decision.actions if isinstance(a, CancelTimer)}
    assert cancelled == {"initial_reminder", "final_reminder", "end_of_business_day"}


def test_outreach_cancel_keyword_closes_case() -> None:
    case = _case(state=CaseState.CONTACTING_CUSTOMER, slots=3)
    decision = decide_next_case_state(case, _inbound(case, "Cancel"), now=NOW)

    assert decision.next_state == CaseState.DECLINED


def test_outreach_free_text_triggers_llm_reply_and_stays() -> None:
    case = _case(state=CaseState.CONTACTING_CUSTOMER, slots=3)
    decision = decide_next_case_state(
        case, _inbound(case, "Can I do Friday at 3?"), now=NOW
    )

    assert decision.next_state == CaseState.CONTACTING_CUSTOMER
    calls = [a for a in decision.actions if isinstance(a, PlaceCall)]
    assert len(calls) == 1
    assert calls[0].stage == CallStage.OUTREACH
    events = [a for a in decision.actions if isinstance(a, RecordEvent)]
    assert any(e.event == "case.outreach.free_text" for e in events)


def test_outreach_digit_out_of_range_treated_as_free_text() -> None:
    case = _case(state=CaseState.CONTACTING_CUSTOMER, slots=3)
    # 9 > N+1 → not a valid pick, treat as free text.
    decision = decide_next_case_state(case, _inbound(case, "9"), now=NOW)

    assert decision.next_state == CaseState.CONTACTING_CUSTOMER
    calls = [a for a in decision.actions if isinstance(a, PlaceCall)]
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Reminder stages: initial.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["1", "Yes", "OK", "confirmed"])
def test_initial_reminder_confirm_is_audit_only(reply: str) -> None:
    case = _case(state=CaseState.INITIAL_REMINDER_SENT)
    decision = decide_next_case_state(case, _inbound(case, reply), now=NOW)

    # Confirm at the initial reminder is a no-op (final reminder fires
    # on its own schedule); only an audit RecordEvent should be emitted.
    assert decision.next_state == CaseState.INITIAL_REMINDER_SENT
    assert all(isinstance(a, RecordEvent) for a in decision.actions)


@pytest.mark.parametrize("reply", ["2", "Reschedule", "new time"])
def test_initial_reminder_reschedule_enters_rescheduling(reply: str) -> None:
    case = _case(state=CaseState.INITIAL_REMINDER_SENT)
    decision = decide_next_case_state(case, _inbound(case, reply), now=NOW)

    assert decision.next_state == CaseState.RESCHEDULING
    assert any(isinstance(a, RequestDealerSlots) for a in decision.actions)


@pytest.mark.parametrize("reply", ["3", "Cancel", "no"])
def test_initial_reminder_cancel_closes_case(reply: str) -> None:
    case = _case(state=CaseState.INITIAL_REMINDER_SENT)
    decision = decide_next_case_state(case, _inbound(case, reply), now=NOW)

    assert decision.next_state == CaseState.CANCELLED


def test_initial_reminder_free_text_keeps_state_and_replies() -> None:
    case = _case(state=CaseState.INITIAL_REMINDER_SENT)
    decision = decide_next_case_state(
        case, _inbound(case, "Can you remind me where the dealer is?"), now=NOW
    )

    assert decision.next_state == CaseState.INITIAL_REMINDER_SENT
    calls = [a for a in decision.actions if isinstance(a, PlaceCall)]
    assert len(calls) == 1
    assert calls[0].stage == CallStage.INITIAL_REMINDER


# ---------------------------------------------------------------------------
# Reminder stages: final.
# ---------------------------------------------------------------------------


def test_final_reminder_confirm_holds_for_arrival() -> None:
    case = _case(state=CaseState.FINAL_REMINDER_SENT)
    decision = decide_next_case_state(case, _inbound(case, "1"), now=NOW)

    assert decision.next_state == CaseState.FINAL_REMINDER_SENT


def test_final_reminder_cancel_closes_case() -> None:
    case = _case(state=CaseState.FINAL_REMINDER_SENT)
    decision = decide_next_case_state(case, _inbound(case, "3"), now=NOW)

    assert decision.next_state == CaseState.CANCELLED


def test_final_reminder_free_text_keeps_state_and_replies() -> None:
    case = _case(state=CaseState.FINAL_REMINDER_SENT)
    decision = decide_next_case_state(
        case, _inbound(case, "Running 10 min late, sorry"), now=NOW
    )

    assert decision.next_state == CaseState.FINAL_REMINDER_SENT
    calls = [a for a in decision.actions if isinstance(a, PlaceCall)]
    assert len(calls) == 1
    assert calls[0].stage == CallStage.FINAL_REMINDER


# ---------------------------------------------------------------------------
# Unhandled-but-not-crashing states (log + ignore, no LLM round-trip).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        CaseState.BOOKED,
        CaseState.INITIAL_REMINDER_DUE,
        CaseState.FINAL_REMINDER_DUE,
        CaseState.SHOWED,
    ],
)
def test_inbound_in_unhandled_state_records_audit_only(state: CaseState) -> None:
    case = _case(state=state)
    decision = decide_next_case_state(case, _inbound(case, "hello"), now=NOW)

    assert decision.next_state == state
    # No outbound calls, no dealer round-trips — just an audit event so
    # the operator can see the inbound landed somewhere unexpected.
    assert all(isinstance(a, RecordEvent) for a in decision.actions)
    assert any("inbound" in a.event for a in decision.actions if isinstance(a, RecordEvent))
