"""Case Manager — orchestrates one trigger → one case → one+ call attempts.

The case manager is the **only** public entry point for placing calls
to ElevenLabs (per ADR 0006). It loads master data through the
``MasterDataRepository``, builds a ``Case`` via ``create_case_from_trigger``,
persists it through ``CaseRepository``, delegates the actual call to
``CallSession``, and walks the case through its terminal state based on
the ``CallOutcome`` returned.

The retry policy is encapsulated in ``RetryPolicy`` (today: single-shot,
no retries). The state-transition decision is a **pure function**
(``_decide_terminal_state``) so it can be property-tested independently
of the I/O.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Protocol, final

import structlog

from guidepoint.case._call_session import CallSession
from guidepoint.case._factory import create_case_from_trigger
from guidepoint.case._models import (
    CallAttempt,
    CallOutcome,
    Case,
    CaseEvent,
    CaseId,
    CaseState,
    Trigger,
    TriggerForeignKeyError,
)
from guidepoint.case._repository import CaseRepository
from guidepoint.case._trigger_source import TriggerSource
from guidepoint.clock import Clock
from guidepoint.events import EventBus
from guidepoint.master_data import (
    MasterDataRepository,
)

_CaseEventBus = EventBus[CaseEvent]

_log = structlog.get_logger(__name__)


@final
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How aggressively the case manager retries failed call attempts.

    Default: single-shot. One call, then the case lands in a terminal
    state regardless of why it didn't succeed. Confirmed by the human
    operator's policy as of ADR 0006.
    """

    max_attempts: int = 1


class CaseManager(Protocol):
    """SPOT to ElevenLabs. Owns the case state machine."""

    async def fire(self, trigger: Trigger) -> Case:
        """Create a case from the trigger, place the first call, return the case.

        Pipeline:

        1. Load customer / dealer / vehicle from master data.
        2. ``create_case_from_trigger`` → new Case in state CREATED.
        3. Persist + publish ``case.created``.
        4. Transition to CALLING; persist + publish ``case.calling``.
        5. ``call_session.place(case)`` → returns ``CallOutcome``.
        6. Append the attempt to the case.
        7. Decide terminal state from outcome + retry policy.
        8. Persist outcome + publish terminal event.
        9. Mark trigger as fired in the trigger source.

        Returns the Case in its (potentially terminal) end state.
        """
        ...

    async def cancel(self, case_id: CaseId, *, reason: str) -> Case:
        """External cancel: dealer pulled the case, operator override, etc."""
        ...


def build_default_case_manager(
    *,
    master_data: MasterDataRepository,
    case_repo: CaseRepository,
    trigger_source: TriggerSource,
    call_session: CallSession,
    bus: _CaseEventBus,
    clock: Clock,
    retry_policy: RetryPolicy | None = None,
) -> CaseManager:
    """Construct the default ``CaseManager`` with all dependencies injected."""
    return _DefaultCaseManager(
        master_data=master_data,
        case_repo=case_repo,
        trigger_source=trigger_source,
        call_session=call_session,
        bus=bus,
        clock=clock,
        retry_policy=retry_policy or RetryPolicy(),
    )


@final
class _DefaultCaseManager:
    """Single implementation of ``CaseManager``."""

    def __init__(
        self,
        *,
        master_data: MasterDataRepository,
        case_repo: CaseRepository,
        trigger_source: TriggerSource,
        call_session: CallSession,
        bus: _CaseEventBus,
        clock: Clock,
        retry_policy: RetryPolicy,
    ) -> None:
        self._master_data = master_data
        self._case_repo = case_repo
        self._trigger_source = trigger_source
        self._call_session = call_session
        self._bus = bus
        self._clock = clock
        self._retry_policy = retry_policy

    async def fire(self, trigger: Trigger) -> Case:
        case = self._build_case(trigger)
        self._case_repo.save(case)
        await self._emit(case, "case.created", f"trigger={trigger.id}")

        case = self._case_repo.update_state(case.case_id, new_state=CaseState.CALLING)
        await self._emit(case, "case.calling", f"attempt {case.attempt_count + 1}")

        outcome = await self._call_session.place(case)
        case = self._case_repo.append_call_attempt(
            case.case_id,
            CallAttempt(attempt_number=case.attempt_count + 1, outcome=outcome),
        )

        terminal_state, detail = _decide_terminal_state(
            outcome=outcome,
            retry_policy=self._retry_policy,
            current_attempt=case.attempt_count,
        )
        case = self._case_repo.update_outcome(
            case.case_id,
            new_state=terminal_state,
            outcome_detail=detail,
            booked_slot_id=outcome.booked_slot_id,
            closed_at=self._clock.now(),
        )
        await self._emit(case, f"case.{terminal_state.value}", detail)

        self._trigger_source.mark_fired(trigger.id, case_id=case.case_id)
        _log.info(
            "case.fire.complete",
            case_id=case.case_id,
            trigger_id=trigger.id,
            terminal_state=terminal_state.value,
            correlation_id=case.correlation_id,
        )
        return case

    async def cancel(self, case_id: CaseId, *, reason: str) -> Case:
        case = self._case_repo.update_outcome(
            case_id,
            new_state=CaseState.CANCELLED,
            outcome_detail=reason,
            booked_slot_id=None,
            closed_at=self._clock.now(),
        )
        await self._emit(case, "case.cancelled", reason)
        return case

    def _build_case(self, trigger: Trigger) -> Case:
        try:
            vehicle = self._master_data.get_vehicle(trigger.vehicle_vin)
            customer = self._master_data.get_customer(vehicle.owner_id)
            dealer = self._master_data.get_dealer(trigger.dealer_id)
        except Exception as exc:
            self._trigger_source.mark_failed(trigger.id, error_detail=str(exc))
            raise
        try:
            return create_case_from_trigger(
                trigger=trigger,
                customer=customer,
                dealer=dealer,
                vehicle=vehicle,
                clock=self._clock,
            )
        except TriggerForeignKeyError as exc:
            self._trigger_source.mark_failed(trigger.id, error_detail=str(exc))
            raise

    async def _emit(self, case: Case, event_name: str, detail: str) -> None:
        event = CaseEvent(
            event_id=f"evt_{secrets.token_hex(6)}",
            case_id=case.case_id,
            correlation_id=case.correlation_id,
            attempt_number=None,
            timestamp=self._clock.now(),
            source="system",
            level="info",
            event=event_name,
            detail=detail,
        )
        self._case_repo.append_event(case.case_id, event)
        await self._bus.publish(event)


_BUSINESS_TO_STATE: dict[str, tuple[CaseState, str]] = {
    "declined": (CaseState.DECLINED, "customer declined"),
    "transferred": (CaseState.ESCALATED, "transferred to human"),
}

_RESULT_TO_DETAIL: dict[str, str] = {
    "no_answer": "no answer",
    "busy": "line busy",
    "connection_failed": "connection failed",
}


def _decide_terminal_state(
    *,
    outcome: CallOutcome,
    retry_policy: RetryPolicy,
    current_attempt: int,
) -> tuple[CaseState, str]:
    """Pure: map a CallOutcome + retry policy to a terminal CaseState.

    Single-shot policy (max_attempts=1) bypasses retries entirely:
    every non-business outcome lands in ``UNREACHABLE``. When we add
    multi-attempt policies, this function gains a ``BETWEEN_ATTEMPTS``
    branch — and only this function changes.
    """
    if outcome.business_outcome == "booked":
        slot = outcome.booked_slot_id or "(unknown slot)"
        return CaseState.BOOKED, f"booked slot {slot}"
    if outcome.business_outcome in _BUSINESS_TO_STATE:
        return _BUSINESS_TO_STATE[outcome.business_outcome]
    if current_attempt < retry_policy.max_attempts:  # pragma: no cover (multi-attempt unused)
        return CaseState.BETWEEN_ATTEMPTS, "scheduling retry"
    if outcome.result == "error":
        return CaseState.UNREACHABLE, f"error: {outcome.error_detail or 'unknown'}"
    detail = _RESULT_TO_DETAIL.get(outcome.result, "inconclusive call")
    return CaseState.UNREACHABLE, detail


__all__ = [
    "CaseManager",
    "RetryPolicy",
    "build_default_case_manager",
]
