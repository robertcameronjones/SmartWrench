"""``CaseDriver`` — the imperative shell around the pure case reducer.

The driver is the only place that:

- Owns per-case ``asyncio.Queue[CaseSignal]`` instances (one per active
  case) and the per-case ``asyncio.Task`` that consumes from each.
- Routes incoming ``CaseSignal`` values to the right queue(s) through
  ``on_signal``: case-targeted signals go to one queue, vehicle /
  customer signals are resolved through the repo, world signals fan
  out to every active case.
- Executes the ``CaseAction`` tuple the reducer returns — by handing
  off to the ``CallManager`` / ``DealerSlotPort`` / ``TimerService``
  Protocols defined in ``_ports.py``.
- Persists state transitions and patches through the ``CaseRepository``.
- Recovers in-flight cases at startup via ``recover_in_flight``.

Every decision lives in ``decide_next_case_state`` (pure); every
side-effect lives here (shell). That split is what makes the reducer
exhaustively unit-testable and lets the driver be integration-tested
against fakes for each adapter Protocol.

Concurrency model
-----------------

- **One asyncio.Task per active case.** Each task drains its own
  bounded queue. Cases run in parallel; signals for one case never
  block another.
- **I/O actions spawn sub-tasks.** ``PlaceCall``,
  ``RequestDealerSlots``, ``RequestDealerConfirmation`` each spawn a
  separate task so the case loop is free to handle other signals
  (notably ``CustomerOptedOut``) while the call / dealer round-trip
  is in flight. When the sub-task completes, it feeds the result back
  in as a ``CallEnded`` / ``DealerSlotsListed`` / ``DealerConfirmed``
  / ``DealerRejected`` signal through the same ``on_signal`` path
  everything else uses.
- **Bounded per-case queues** (default 64). Overflow drops the newest
  signal with a ``queue.overflow.case_signal`` warn-level log. The
  simulator's expected load is well under that ceiling; production
  loads will be re-evaluated when SQLite + multi-tenant land.

Persistence model
-----------------

Phase 4 reads/writes through the existing JSON-file ``CaseRepository``.
That repo's ``save()`` is a full-file rewrite, which means concurrent
``append_event`` + ``save`` calls on the same case can race. For the
single-process simulator workload this is acceptable; Phase 9 (SQLite)
introduces real transactions. The driver is structured so that swap is
a one-line change.

Why ``fire()`` seeds a ``CaseCreated`` signal
---------------------------------------------

The reducer's ``CREATED → CONTACTING_CUSTOMER`` transition is driven
by the explicit ``CaseCreated`` lifecycle signal. ``fire()`` emits it
immediately after persisting the new case, so callers never have to
remember to kick the case off themselves. Production callers that want
to create a case without auto-starting outreach can use the lower
level repository + ``on_signal`` API directly.

Note: ``fire()`` does **not** check business hours. Sends produced by
the resulting ``PlaceCall`` flow through the outbound queue, which is
the single owner of the hours-gating policy — messages enqueued during
closed hours simply sit until the gate reopens.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Mapping
from contextlib import suppress
from typing import Any, final

import structlog

from guidepoint.case._actions import (
    CancelTimer,
    CaseAction,
    PlaceCall,
    RecordEvent,
    RequestDealerConfirmation,
    RequestDealerSlots,
    ScheduleTimer,
)
from guidepoint.case._factory import create_case_from_trigger
from guidepoint.case._models import (
    CallOutcome,
    Case,
    CaseEvent,
    CaseId,
    CaseState,
    Channel,
    Trigger,
)
from guidepoint.case._ports import (
    CallManager,
    DealerSlotPort,
    SmsDispatcher,
    TimerService,
)
from guidepoint.case._reducer import (
    CaseDecision,
    decide_next_case_state,
)
from guidepoint.case._repository import CaseRepository
from guidepoint.case._signals import (
    CallEnded,
    CaseCreated,
    CaseSignal,
    DealerConfirmed,
    DealerRejected,
    DealerSlotsListed,
    InboundSmsReceived,
    is_case_targeted,
    is_customer_targeted,
    is_vehicle_targeted,
    is_world_signal,
)
from guidepoint.clock import Clock
from guidepoint.events import EventBus
from guidepoint.master_data import (
    CustomerRecord,
    DealerRecord,
    VehicleRecord,
)

_CaseEventBus = EventBus[CaseEvent]

_log = structlog.get_logger(__name__)

#: Default per-case signal queue size. Overflow drops the newest signal
#: with a ``queue.overflow.case_signal`` warning. Surfaced as an
#: ``__init__`` parameter so high-volume tests can tighten it.
DEFAULT_CASE_QUEUE_SIZE: int = 64


@final
class CaseDriver:
    """Per-case asyncio task pool around the pure reducer.

    Construct one instance per process. Wire it up at boot:

    .. code-block:: python

        driver = CaseDriver(
            case_repo=repo,
            call_manager=call_manager,
            dealer_port=dealer_port,
            timer_service=timer_service,
            bus=bus,
            clock=clock,
        )
        await driver.recover_in_flight()
        # then route trigger / webhook / geofence events through:
        await driver.on_signal(signal)
    """

    def __init__(
        self,
        *,
        case_repo: CaseRepository,
        call_manager: CallManager | None = None,
        call_managers: Mapping[Channel, CallManager] | None = None,
        sms_dispatcher: SmsDispatcher | None = None,
        dealer_port: DealerSlotPort,
        timer_service: TimerService,
        bus: _CaseEventBus,
        clock: Clock,
        queue_size: int = DEFAULT_CASE_QUEUE_SIZE,
    ) -> None:
        if call_manager is not None and call_managers is not None:
            raise ValueError("pass call_manager or call_managers, not both")
        if call_manager is None and call_managers is None:
            raise ValueError("pass call_manager or call_managers")
        self._case_repo = case_repo
        if call_managers is not None:
            self._call_managers: dict[Channel, CallManager] = dict(call_managers)
        else:
            assert call_manager is not None
            self._call_managers = {"voice": call_manager}
        # SMS is *not* a CallManager. It's a turn-by-turn dispatcher
        # (compose-and-send) hand in hand with the InboundSmsReceived
        # signal path. The driver routes SMS PlaceCall actions through
        # this dispatcher instead of CallManager.start when the case's
        # initial_channel is "sms".
        self._sms_dispatcher = sms_dispatcher
        self._dealer_port = dealer_port
        self._timer_service = timer_service
        self._bus = bus
        self._clock = clock
        self._queue_size = queue_size

        # Per-case state. ``_queues`` and ``_tasks`` are kept in lockstep
        # under ``_lock``: when a case loop ends (terminal state reached
        # or cancellation), both entries are removed. Background I/O
        # tasks (PlaceCall, dealer calls) are tracked separately in
        # ``_io_tasks`` so ``shutdown`` can wait on them too.
        self._queues: dict[CaseId, asyncio.Queue[CaseSignal]] = {}
        self._tasks: dict[CaseId, asyncio.Task[None]] = {}
        self._io_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    # -- Public surface ---------------------------------------------------

    async def fire(
        self,
        *,
        trigger: Trigger,
        customer: CustomerRecord,
        dealer: DealerRecord,
        vehicle: VehicleRecord,
    ) -> CaseId:
        """Create a case from a trigger, persist it, and start its loop.

        Emits a :class:`CaseCreated` lifecycle signal once the case row
        is persisted, which the reducer translates into the initial
        outreach transition. Returns the new ``CaseId``.
        """

        case = create_case_from_trigger(
            trigger=trigger,
            customer=customer,
            dealer=dealer,
            vehicle=vehicle,
            clock=self._clock,
        )
        self._case_repo.save(case)
        await self._ensure_loop(case.case_id)
        await self.on_signal(
            CaseCreated(
                timestamp=self._clock.now(),
                case_id=case.case_id,
            )
        )
        return case.case_id

    async def on_signal(self, signal: CaseSignal) -> None:
        """Route one signal to the correct case queue(s).

        Case-targeted signals go to one queue; vehicle / customer
        signals are resolved via the repo and may fan out to several
        queues; world signals fan out to every active case. Signals
        for terminal or unknown cases are dropped with a debug log.
        """

        if is_case_targeted(signal):
            await self._enqueue_for_case(signal.case_id, signal)
            return

        if is_vehicle_targeted(signal):
            for case in self._case_repo.list_by_vehicle_vin(signal.vehicle_vin):
                await self._enqueue_for_case(case.case_id, signal)
            return

        if is_customer_targeted(signal):
            for case in self._case_repo.list_by_customer_phone(signal.customer_phone):
                await self._enqueue_for_case(case.case_id, signal)
            return

        if is_world_signal(signal):
            for case in self._case_repo.list_active():
                await self._enqueue_for_case(case.case_id, signal)
            return

        # The discriminated union is exhaustive — this branch is purely
        # defensive against future signal types that forget a classifier.
        _log.warning("case_driver.signal.unclassified", signal=type(signal).__name__)

    async def on_inbound_sms(
        self,
        *,
        case_id: CaseId,
        from_phone: str,
        body: str,
        message_sid: str,
    ) -> None:
        """Webhook entry: record one inbound SMS + fan out the signal.

        The webhook layer has already translated ``from_phone`` into
        the active ``case_id`` via the routing store. From here:

        1. The SMS dispatcher appends the customer turn to the SMS
           history store (and tails the SMS event log) so that the
           next LLM-composed reply sees what the customer actually
           sent.
        2. An :class:`InboundSmsReceived` signal is enqueued onto the
           case's signal queue. The reducer picks it up on the next
           tick and decides what to do (digit pick → state move,
           keyword → state move, free text → emit a new ``PlaceCall``
           for an LLM reply).

        Opt-out (``STOP`` / ``UNSUBSCRIBE``) is NOT routed through
        here; the webhook short-circuits to
        :class:`CustomerOptedOut` for consent updates that must run
        even when no case is active.
        """

        if self._sms_dispatcher is None:
            _log.error(
                "sms_dispatcher.missing_on_inbound",
                case_id=case_id,
                from_phone=from_phone,
                message_sid=message_sid,
            )
            return
        try:
            await self._sms_dispatcher.record_inbound(
                case_id=case_id,
                from_phone=from_phone,
                body=body,
                message_sid=message_sid,
            )
        except Exception as exc:
            _log.exception(
                "sms_dispatcher.record_inbound.failed",
                case_id=case_id,
                from_phone=from_phone,
                message_sid=message_sid,
                error=str(exc),
            )
        await self.on_signal(
            InboundSmsReceived(
                timestamp=self._clock.now(),
                case_id=case_id,
                from_phone=from_phone,
                body=body,
                message_sid=message_sid,
            )
        )

    async def recover_in_flight(self) -> int:
        """Spawn a case loop for every non-terminal case in the repo.

        Call this once at process startup before routing any external
        signals. Returns the number of loops spawned (for observability /
        startup logs).

        Note: per-case wall-clock timers are *not* recovered here.
        Reminders / day-of / EoD timers are in-memory in the
        ``TimerService`` and must be re-armed by the simulator's
        operator UI (or, in production, by the cron / scheduler). Phase 9
        (SQLite + persistent timers) addresses this.
        """

        count = 0
        for case in self._case_repo.list_active():
            await self._ensure_loop(case.case_id)
            count += 1
        _log.info("case_driver.recovered", count=count)
        return count

    async def shutdown(self) -> None:
        """Cancel every running task. Idempotent."""

        async with self._lock:
            case_tasks = list(self._tasks.values())
            io_tasks = list(self._io_tasks)
            self._tasks.clear()
            self._queues.clear()
            self._io_tasks.clear()
        for task in (*case_tasks, *io_tasks):
            task.cancel()
        for task in (*case_tasks, *io_tasks):
            with suppress(asyncio.CancelledError):
                await task

    def queue_depths(self) -> Mapping[str, int]:
        """Snapshot of per-case queue depths, keyed by case id.

        Returned as plain ``str -> int`` so it's trivial to dump as JSON
        for a runtime health endpoint.
        """

        return {str(cid): q.qsize() for cid, q in self._queues.items()}

    def active_case_count(self) -> int:
        """Number of currently-running case loops."""

        return len(self._tasks)

    # -- Internal: queue + task lifecycle ---------------------------------

    async def _ensure_loop(self, case_id: CaseId) -> asyncio.Queue[CaseSignal]:
        """Spawn the case loop if not already running; return its queue."""

        async with self._lock:
            queue = self._queues.get(case_id)
            if queue is not None:
                return queue
            queue = asyncio.Queue(maxsize=self._queue_size)
            self._queues[case_id] = queue
            task = asyncio.create_task(
                self._case_loop(case_id), name=f"case_loop:{case_id}"
            )
            self._tasks[case_id] = task
            return queue

    async def _enqueue_for_case(
        self, case_id: CaseId, signal: CaseSignal
    ) -> None:
        """Drop signal onto the case's queue. Lazily spawns the loop.

        Terminal cases are skipped (no point waking them up just to
        bail on the first tick).
        """

        # If the case is terminal, drop. Doing the repo read outside
        # the lock keeps the hot path free.
        try:
            case = self._case_repo.get(case_id)
        except Exception:
            _log.warning(
                "case_driver.signal.missing_case",
                case_id=case_id,
                signal_type=type(signal).__name__,
            )
            return
        if case.state.is_terminal:
            _log.debug(
                "case_driver.signal.dropped_terminal",
                case_id=case_id,
                signal_type=type(signal).__name__,
                state=case.state.value,
            )
            return

        queue = await self._ensure_loop(case_id)
        try:
            queue.put_nowait(signal)
        except asyncio.QueueFull:
            _log.warning(
                "queue.overflow.case_signal",
                case_id=case_id,
                signal_type=type(signal).__name__,
                depth=queue.qsize(),
                cap=self._queue_size,
            )

    async def _case_loop(self, case_id: CaseId) -> None:
        """One per-case worker. Drains its queue, retires on terminal."""

        queue = self._queues[case_id]
        while True:
            try:
                signal = await queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._handle_signal(case_id, signal)
            except Exception as exc:  # pragma: no cover (defensive)
                _log.exception(
                    "case_loop.error",
                    case_id=case_id,
                    signal_type=type(signal).__name__,
                    error=str(exc),
                )
            # Retire the loop if the case is now terminal. Reading
            # state on each tick keeps us honest about decisions taken
            # in I/O sub-tasks while we were busy.
            try:
                case = self._case_repo.get(case_id)
            except Exception:
                return
            if case.state.is_terminal:
                async with self._lock:
                    self._queues.pop(case_id, None)
                    self._tasks.pop(case_id, None)
                return

    async def _handle_signal(self, case_id: CaseId, signal: CaseSignal) -> None:
        """Run one reducer tick + apply the resulting decision."""

        case = self._case_repo.get(case_id)
        if case.state.is_terminal:
            return
        decision = decide_next_case_state(case, signal, now=self._clock.now())
        if decision.is_noop and decision.next_state == case.state:
            _log.debug(
                "case_driver.decision.noop",
                case_id=case_id,
                state=case.state.value,
                reason=decision.reason,
            )
            return
        await self._apply_decision(case, decision)

    async def _apply_decision(self, case: Case, decision: CaseDecision) -> None:
        """Persist patch + state, then dispatch every action in order."""

        persisted = self._persist(case, decision)
        for action in decision.actions:
            await self._dispatch(persisted, action)

    def _persist(self, case: Case, decision: CaseDecision) -> Case:
        """Apply the patch + state transition to the case row."""

        updates: dict[str, Any] = {}
        if decision.next_state != case.state:
            updates["state"] = decision.next_state
            if decision.next_state.is_terminal:
                updates["closed_at"] = self._clock.now()
                if not case.outcome_detail:
                    updates["outcome_detail"] = decision.reason

        patch = decision.patch
        if patch.booked_slot_id is not None:
            updates["booked_slot_id"] = patch.booked_slot_id
        if patch.booked_slot_display is not None:
            updates["booked_slot_display"] = patch.booked_slot_display
        if patch.next_attempt_at is not None:
            updates["next_attempt_at"] = patch.next_attempt_at
        if patch.context_notes is not None:
            updates["context_notes"] = patch.context_notes
        if patch.increment_reschedule_count:
            updates["reschedule_count"] = case.reschedule_count + 1
        if patch.increment_attempt_count:
            updates["attempt_count"] = case.attempt_count + 1

        if not updates:
            return case
        updated = case.model_copy(update=updates)
        self._case_repo.save(updated)
        return updated

    # -- Internal: action dispatch ----------------------------------------

    async def _dispatch(self, case: Case, action: CaseAction) -> None:
        if isinstance(action, PlaceCall):
            self._spawn_io(self._run_place_call(case, action))
            return
        if isinstance(action, RequestDealerSlots):
            self._spawn_io(self._run_request_slots(case))
            return
        if isinstance(action, RequestDealerConfirmation):
            self._spawn_io(self._run_request_confirmation(case, action))
            return
        if isinstance(action, ScheduleTimer):
            self._timer_service.schedule(
                case_id=action.case_id, name=action.name, fire_at=action.fire_at
            )
            return
        if isinstance(action, CancelTimer):
            self._timer_service.cancel(case_id=action.case_id, name=action.name)
            return
        if isinstance(action, RecordEvent):
            await self._record_event(case, action)
            return
        # Defensive — discriminated union should make this unreachable.
        _log.warning(
            "case_driver.action.unknown", action=type(action).__name__
        )

    def _spawn_io(self, coro: Any) -> None:
        """Schedule a background I/O coroutine and track it for shutdown."""

        task = asyncio.create_task(coro)
        self._io_tasks.add(task)
        task.add_done_callback(self._io_tasks.discard)

    async def _run_place_call(self, case: Case, action: PlaceCall) -> None:
        """Dispatch one PlaceCall.

        Routing depends on the case's ``initial_channel``:

        - ``"sms"`` — compose one assistant reply via the injected
          :class:`SmsDispatcher` and hand it to the outbound queue.
          No ``CallEnded`` signal is emitted; SMS is turn-by-turn and
          the conversation continues via :class:`InboundSmsReceived`
          when the customer replies.
        - everything else (voice today) — call the channel's
          :class:`CallManager` ``start`` method, which runs the call
          until terminal and produces a :class:`CallOutcome`; the
          driver wraps it into ``CallEnded`` for the reducer.
        """

        fresh = self._safe_get(case.case_id)
        if fresh is None:
            return

        if fresh.initial_channel == "sms":
            await self._run_sms_outbound(fresh, action)
            return

        mgr = self._call_managers.get(fresh.initial_channel)
        if mgr is None:
            _log.error(
                "call_manager.missing_for_channel",
                case_id=case.case_id,
                channel=fresh.initial_channel,
                available=sorted(self._call_managers.keys()),
            )
            now = self._clock.now()
            outcome = CallOutcome(
                result="error",
                business_outcome="inconclusive",
                started_at=now,
                ended_at=now,
                duration_seconds=0.0,
                error_detail=f"no CallManager for channel {fresh.initial_channel!r}",
            )
            await self.on_signal(
                CallEnded(
                    timestamp=self._clock.now(),
                    case_id=case.case_id,
                    outcome=outcome,
                )
            )
            return
        try:
            outcome = await mgr.start(
                case=fresh, stage=action.stage, attempt_number=action.attempt_number
            )
        except Exception as exc:
            _log.exception(
                "call_manager.start.failed",
                case_id=case.case_id,
                stage=action.stage.value,
                attempt_number=action.attempt_number,
                error=str(exc),
            )
            now = self._clock.now()
            outcome = CallOutcome(
                result="error",
                business_outcome="inconclusive",
                started_at=now,
                ended_at=now,
                duration_seconds=0.0,
                error_detail=f"{type(exc).__name__}: {exc}",
            )
        await self.on_signal(
            CallEnded(
                timestamp=self._clock.now(),
                case_id=case.case_id,
                outcome=outcome,
            )
        )

    async def _run_sms_outbound(self, case: Case, action: PlaceCall) -> None:
        """SMS path for PlaceCall — compose + send, then return.

        No long-running session, no terminal outcome to wait for. The
        reducer drives the next state via :class:`InboundSmsReceived`
        when the customer replies, or via the reminder timers / EoD /
        geofence signals when the wall clock advances.
        """

        if self._sms_dispatcher is None:
            _log.error(
                "sms_dispatcher.missing",
                case_id=case.case_id,
                stage=action.stage.value,
                attempt_number=action.attempt_number,
            )
            return
        try:
            await self._sms_dispatcher.dispatch_outbound(
                case_id=case.case_id,
                to_phone=case.customer.phone,
                stage=action.stage,
            )
        except Exception as exc:
            _log.exception(
                "sms_dispatcher.dispatch_outbound.failed",
                case_id=case.case_id,
                stage=action.stage.value,
                attempt_number=action.attempt_number,
                error=str(exc),
            )

    async def _run_request_slots(self, case: Case) -> None:
        """Run DealerSlotPort.list_slots() and feed DealerSlotsListed back."""

        fresh = self._safe_get(case.case_id)
        if fresh is None:
            return
        try:
            slots = await self._dealer_port.list_slots(case=fresh)
        except Exception as exc:
            _log.exception(
                "dealer_port.list_slots.failed",
                case_id=case.case_id,
                error=str(exc),
            )
            slots = ()
        await self.on_signal(
            DealerSlotsListed(
                timestamp=self._clock.now(),
                case_id=case.case_id,
                slots=slots,
            )
        )

    async def _run_request_confirmation(
        self, case: Case, action: RequestDealerConfirmation
    ) -> None:
        """Run DealerSlotPort.confirm_slot() and feed back confirm/reject."""

        fresh = self._safe_get(case.case_id)
        if fresh is None:
            return
        try:
            ok = await self._dealer_port.confirm_slot(
                case=fresh, slot_id=action.slot_id
            )
        except Exception as exc:
            _log.exception(
                "dealer_port.confirm_slot.failed",
                case_id=case.case_id,
                slot_id=action.slot_id,
                error=str(exc),
            )
            ok = False

        now = self._clock.now()
        if ok:
            await self.on_signal(
                DealerConfirmed(
                    timestamp=now, case_id=case.case_id, slot_id=action.slot_id
                )
            )
        else:
            await self.on_signal(
                DealerRejected(
                    timestamp=now,
                    case_id=case.case_id,
                    slot_id=action.slot_id,
                    reason="dealer_port rejected",
                )
            )

    async def _record_event(self, case: Case, action: RecordEvent) -> None:
        """Append a CaseEvent to the case + publish on the bus."""

        event = CaseEvent(
            event_id=f"evt_{secrets.token_hex(6)}",
            case_id=case.case_id,
            correlation_id=case.correlation_id,
            attempt_number=case.attempt_count or None,
            timestamp=self._clock.now(),
            source="system",
            level=action.level,
            event=action.event,
            detail=action.detail,
        )
        self._case_repo.append_event(case.case_id, event)
        await self._bus.publish(event)

    # -- Small helpers ----------------------------------------------------

    def _safe_get(self, case_id: CaseId) -> Case | None:
        """Read a case from the repo, swallowing not-found. Used by I/O
        sub-tasks where the case may have been deleted between dispatch
        and execution (rare under the simulator workload, but worth
        guarding)."""

        try:
            case = self._case_repo.get(case_id)
        except Exception:
            _log.warning("case_driver.io.case_missing", case_id=case_id)
            return None
        if case.state == CaseState.OPTED_OUT:
            # An opt-out came in between dispatch and execution; skip
            # the round-trip rather than racing with the closure.
            return None
        return case


__all__ = [
    "DEFAULT_CASE_QUEUE_SIZE",
    "CaseDriver",
]
