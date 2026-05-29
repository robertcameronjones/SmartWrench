"""Case Manager — orchestrates one trigger → one case → one+ call attempts.

The case manager is the **only** public entry point for starting customer
interactions (per ADR 0006), regardless of channel. It loads master data
through the ``MasterDataRepository``, builds a ``Case`` via
``create_case_from_trigger``, persists it through ``CaseRepository``,
delegates the actual interaction to the channel-appropriate
``CallSession``, and walks the case through its terminal state based on
the ``CallOutcome`` returned.

Two entry points:

- ``start(trigger)`` — builds the case, transitions it to ``CALLING``,
  spawns the call attempt as a background task, and returns
  immediately. The terminal state lands on the case (and on the bus)
  whenever the channel session finishes. **Use this for any channel
  whose conversation can outlive the HTTP request that fired it** —
  SMS conversations can run for hours, so the route handler must not
  block on them.
- ``fire(trigger)`` — same pipeline, but awaits the background task
  before returning. Convenient for tests / CLI tools that want the
  terminal case in hand.

Channel routing: the manager holds one ``CallSession`` per
``Channel`` (today: ``"voice"`` for the ElevenLabs adapter, ``"sms"``
for the SMS adapter). ``trigger.channel_preference`` picks which one
runs an attempt — the manager's state machine doesn't know or care
which channel ran.

The retry policy is encapsulated in ``RetryPolicy`` (today: single-shot,
no retries). The state-transition decision is a **pure function**
(``_decide_terminal_state``) so it can be property-tested independently
of the I/O.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, final

import structlog

from guidepoint.case._call_session import CallSession
from guidepoint.case._factory import create_case_from_trigger
from guidepoint.case._models import (
    CallAttempt,
    CallOutcome,
    Case,
    CaseError,
    CaseEvent,
    CaseId,
    CaseState,
    Channel,
    Trigger,
    TriggerForeignKeyError,
)
from guidepoint.case._repository import CaseRepository
from guidepoint.case._trigger_source import TriggerSource
from guidepoint.clock import Clock
from guidepoint.events import EventBus
from guidepoint.master_data import (
    CustomerNotFoundError,
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
    """SPOT to every channel. Owns the case state machine."""

    async def start(self, trigger: Trigger) -> Case:
        """Create the case, spawn its attempt in the background, return immediately.

        Pipeline (synchronous portion, before returning):

        1. Load customer / dealer / vehicle from master data.
        2. ``create_case_from_trigger`` → new Case in state CREATED.
        3. Persist + publish ``case.created``.
        4. Transition to CALLING; persist + publish ``case.calling``.
        5. Spawn the background attempt task.

        Background portion (runs after start returns):

        6. ``call_session.place(case)`` → returns ``CallOutcome``.
        7. Append the attempt to the case.
        8. Decide terminal state from outcome + retry policy.
        9. Persist outcome + publish terminal event.
        10. Mark trigger as fired in the trigger source.

        Returns the Case in ``state=CALLING``. Subscribe to the
        ``EventBus`` (or poll ``case_repo``) to observe the terminal
        transition.
        """
        ...

    async def fire(self, trigger: Trigger) -> Case:
        """``start(trigger)`` and await the background attempt to completion.

        Returns the Case in its terminal state. Convenient for tests
        and short-lived voice calls; do not use from an HTTP request
        handler that might serve an SMS case.
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
    bus: _CaseEventBus,
    clock: Clock,
    call_session: CallSession | None = None,
    call_sessions: Mapping[Channel, CallSession] | None = None,
    retry_policy: RetryPolicy | None = None,
) -> CaseManager:
    """Construct the default ``CaseManager`` with all dependencies injected.

    Exactly one of ``call_session`` or ``call_sessions`` must be
    provided. ``call_session`` is the legacy shorthand for a voice-only
    deployment; it's wrapped as ``{"voice": call_session}``.
    ``call_sessions`` is the modern form that supports SMS too.
    """
    if call_session is not None and call_sessions is not None:
        raise CaseError(
            "build_default_case_manager: pass either call_session "
            "(legacy voice-only) or call_sessions (channel mapping), not both"
        )
    if call_session is None and call_sessions is None:
        raise CaseError(
            "build_default_case_manager: must pass call_session "
            "(legacy voice-only) or call_sessions (channel mapping)"
        )
    resolved_sessions: dict[Channel, CallSession] = (
        {"voice": call_session} if call_session is not None else dict(call_sessions or {})
    )
    if not resolved_sessions:
        raise CaseError("build_default_case_manager: call_sessions is empty")
    return _DefaultCaseManager(
        master_data=master_data,
        case_repo=case_repo,
        trigger_source=trigger_source,
        call_sessions=resolved_sessions,
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
        call_sessions: Mapping[Channel, CallSession],
        bus: _CaseEventBus,
        clock: Clock,
        retry_policy: RetryPolicy,
    ) -> None:
        self._master_data = master_data
        self._case_repo = case_repo
        self._trigger_source = trigger_source
        self._call_sessions: dict[Channel, CallSession] = dict(call_sessions)
        self._bus = bus
        self._clock = clock
        self._retry_policy = retry_policy

    async def start(self, trigger: Trigger) -> Case:
        case = await self._initialize(trigger)
        # Fire-and-forget: terminal state lands on the bus when the
        # background attempt completes. We log on failure inside the
        # wrapper rather than letting an unhandled task exception go to
        # the asyncio default handler (which would just print to stderr).
        _ = asyncio.create_task(
            self._run_attempt_logged(case=case, trigger_id=trigger.id),
            name=f"case-attempt-{case.case_id}",
        )
        return case

    async def fire(self, trigger: Trigger) -> Case:
        case = await self._initialize(trigger)
        await self._run_attempt(case=case, trigger_id=trigger.id)
        return self._case_repo.get(case.case_id)

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

    async def _initialize(self, trigger: Trigger) -> Case:
        """Build the case, persist it, transition to CONTACTING_CUSTOMER, emit.

        Synchronous from the caller's POV: any error here surfaces to
        the route handler as a 409 / 500 rather than getting swallowed
        in a background task. Once we return CONTACTING_CUSTOMER,
        ownership flips to the background attempt.
        """
        case = self._build_case(trigger)
        if trigger.channel_preference == "sms":
            customer = self._master_data.get_customer(
                self._master_data.get_vehicle(trigger.vehicle_vin).owner_id
            )
            if not customer.sms_consent:
                detail = f"customer {customer.id!r} has opted out of SMS"
                self._trigger_source.mark_failed(trigger.id, error_detail=detail)
                raise CaseError(detail)
        self._case_repo.save(case)
        await self._emit(case, "case.created", f"trigger={trigger.id}")

        case = self._case_repo.update_state(
            case.case_id, new_state=CaseState.CONTACTING_CUSTOMER
        )
        await self._emit(
            case, "case.contacting_customer", f"attempt {case.attempt_count + 1}"
        )
        return case

    async def _run_attempt(self, *, case: Case, trigger_id: str) -> None:
        """Place the attempt, append it, decide terminal, persist + emit + mark trigger."""
        session = self._call_sessions.get(case.initial_channel)
        if session is None:
            available = ", ".join(sorted(self._call_sessions.keys())) or "(none)"
            raise CaseError(
                f"no CallSession registered for channel {case.initial_channel!r} "
                f"(available: {available})"
            )

        outcome = await session.place(case)
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
            booked_slot_display=outcome.booked_slot_display,
            closed_at=self._clock.now(),
        )
        await self._emit(case, f"case.{terminal_state.value}", detail)

        if outcome.business_outcome == "opted_out":
            self._persist_customer_opt_out(case)

        self._trigger_source.mark_fired(trigger_id, case_id=case.case_id)
        _log.info(
            "case.attempt.complete",
            case_id=case.case_id,
            trigger_id=trigger_id,
            channel=case.initial_channel,
            terminal_state=terminal_state.value,
            correlation_id=case.correlation_id,
        )

    async def _run_attempt_logged(self, *, case: Case, trigger_id: str) -> None:
        """Wrapper for the fire-and-forget code path.

        ``asyncio.create_task`` swallows exceptions until the task is
        awaited; the ``start()`` caller never awaits, so we log here
        and best-effort write a terminal ABANDONED on the case so
        the operator isn't left staring at a CONTACTING_CUSTOMER that
        never resolves.
        """
        try:
            await self._run_attempt(case=case, trigger_id=trigger_id)
        except Exception as exc:
            _log.error(
                "case.attempt.errored",
                case_id=case.case_id,
                trigger_id=trigger_id,
                channel=case.initial_channel,
                error=f"{type(exc).__name__}: {exc}",
            )
            try:
                terminal = self._case_repo.update_outcome(
                    case.case_id,
                    new_state=CaseState.ABANDONED,
                    outcome_detail=f"attempt errored: {type(exc).__name__}: {exc}",
                    booked_slot_id=None,
                    closed_at=self._clock.now(),
                )
                await self._emit(
                    terminal,
                    "case.abandoned",
                    f"attempt errored: {type(exc).__name__}: {exc}",
                )
                self._trigger_source.mark_failed(trigger_id, error_detail=str(exc))
            except Exception as inner:
                _log.error(
                    "case.attempt.finalize_failed",
                    case_id=case.case_id,
                    error=f"{type(inner).__name__}: {inner}",
                )

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

    def _persist_customer_opt_out(self, case: Case) -> None:
        try:
            customer = self._master_data.get_customer(case.customer.id)
        except CustomerNotFoundError:
            return
        if customer.opt_status == "opted_out":
            return
        self._master_data.save_customer(
            customer.model_copy(update={"opt_status": "opted_out"})
        )

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
    "opted_out": (CaseState.OPTED_OUT, "customer opted out"),
    # "transferred" formerly mapped to ESCALATED; v2 has no human-handoff
    # state today, so we collapse it into ABANDONED with a specific
    # detail string. If/when escalation comes back as a v2 stage the
    # reducer (Phase 3) becomes the right place to encode it.
    "transferred": (CaseState.ABANDONED, "transferred to human"),
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
    every non-business outcome lands in ``ABANDONED``. The future v2
    reducer (Phase 3) supersedes this logic; v1 keeps it for the
    pre-reducer path that still drives single-attempt cases today.
    """
    if outcome.business_outcome == "booked":
        slot = outcome.booked_slot_id or "(unknown slot)"
        return CaseState.BOOKED, f"booked slot {slot}"
    if outcome.business_outcome in _BUSINESS_TO_STATE:
        return _BUSINESS_TO_STATE[outcome.business_outcome]
    if current_attempt < retry_policy.max_attempts:  # pragma: no cover (multi-attempt unused)
        return CaseState.CONTACTING_CUSTOMER, "scheduling retry"
    if outcome.result == "error":
        return CaseState.ABANDONED, f"error: {outcome.error_detail or 'unknown'}"
    detail = _RESULT_TO_DETAIL.get(outcome.result, "inconclusive call")
    return CaseState.ABANDONED, detail


__all__ = [
    "CaseManager",
    "RetryPolicy",
    "build_default_case_manager",
]
