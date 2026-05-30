"""Adapter Protocols the case driver depends on.

The Phase 4 ``CaseDriver`` is the imperative shell that walks the
actions returned by the pure ``decide_next_case_state`` reducer. Every
side-effect the driver performs goes through one of three boundaries
defined here:

- ``CallManager`` — places one call (voice or SMS) and returns its
  rolled-up ``CallOutcome``. The same Protocol covers both channels:
  ``VoiceCallManager`` and ``SmsCallManager`` will be the two concrete
  implementations once Phase 5 lands. Until then, tests use a small
  fake and v1's ``CallSession`` continues to drive single-shot voice
  calls via ``CaseManager``.

- ``DealerSlotPort`` — talks to the dealer's slot system (list /
  confirm). Phase 6 wires the simulator implementation (canned slots
  + always-confirm) and the eventual real binding to your colleague's
  tool. The driver only ever sees this Protocol.

- ``TimerService`` — schedules and cancels per-case wall-clock timers
  (reminder window at T-24h, day-of touchpoint at T-2h, end-of-day for
  the no-show gate). When a timer fires, the implementation calls back
  into the driver via the same ``on_signal`` entry point everything
  else uses, so the case loop sees timers as ordinary signals.

Keeping these as Protocols (not abstract base classes) means:

1. Tests can substitute a tiny ``@dataclass`` fake without inheritance.
2. Concrete implementations live in their own modules and don't have to
   import from the driver (avoiding circular imports).
3. The driver remains the only place that knows about all three.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, final

from guidepoint.case._actions import CallStage
from guidepoint.case._models import (
    CallOutcome,
    Case,
    CaseId,
    OfferedSlot,
    SlotId,
)
from guidepoint.clock import UtcDatetime
from guidepoint.master_data import VehicleVin

GeofenceEventKind = Literal["entered", "exited"]


@final
@dataclass(frozen=True, slots=True)
class GeofenceEvent:
    """One geofence transition for a vehicle.

    Channel adapters translate this into ``VehicleEnteredDealer`` /
    ``VehicleExitedDealer`` ``CaseSignal`` values before handing them
    to the ``CaseDriver``. Keeping the port vocabulary small means
    telematics bindings never need to import the full signal union.
    """

    vehicle_vin: VehicleVin
    kind: GeofenceEventKind


class CallManager(Protocol):
    """Place one call for a case in a specific stage.

    Implementations:

    - ``VoiceCallManager`` (today still the v1 ``_LiveCallSession``)
      — ElevenLabs outbound twilio call, polls until terminal, returns
      the rolled-up ``CallOutcome``.

    SMS is **not** a CallManager — see :class:`SmsDispatcher` below.
    SMS is turn-by-turn (one outbound text per ``PlaceCall`` action,
    one inbound text per :class:`InboundSmsReceived` signal) and does
    not match the "place one call, get one rolled-up outcome" shape.

    The driver passes the *case snapshot at the time of dispatch*. The
    CallManager must not assume the case won't change while the call is
    in flight (e.g. an opt-out signal might close the case from another
    thread of execution); the only side-effect of ``start`` is to return
    a ``CallOutcome`` describing what happened on this attempt.
    """

    async def start(
        self,
        *,
        case: Case,
        stage: CallStage,
        attempt_number: int,
    ) -> CallOutcome:
        """Place the call, run the conversation, return the terminal outcome.

        Args:
            case: Frozen snapshot of the case the call belongs to.
            stage: Which conversational stage this call is for. Drives
                prompt and tool surface selection inside the CallManager.
            attempt_number: 1-based index of this attempt within the
                stage. Used for logging and the audit trail.

        Returns:
            A ``CallOutcome``. The driver will wrap this into a
            ``CallEnded`` signal and re-enqueue onto the case's queue,
            where the reducer picks it up on the next tick.
        """
        ...


class SmsDispatcher(Protocol):
    """Compose-and-send one outbound SMS reply for a case.

    Replaces the v1 ``CallManager`` path for SMS. SMS is turn-by-turn
    (one outbound text per ``PlaceCall`` action), not a long-running
    "call" with a single rolled-up outcome, so this Protocol returns
    nothing structural — only the queued ``item_id`` for the audit
    trail. The real Twilio MessageSid arrives back as a separate
    :class:`OutboundDispatched` signal once the worker dispatches.

    The driver also delegates the inbound side to this Protocol: it
    asks the dispatcher to record the customer's turn (history +
    audit log) before handing the inbound off to the reducer. Keeping
    the call shape inside one Protocol means the driver never imports
    the SMS adapter directly — sms_adapter implements this Protocol
    and the simulator (or tests) inject the implementation.
    """

    async def dispatch_outbound(
        self,
        *,
        case_id: CaseId,
        to_phone: str,
        stage: CallStage,
    ) -> str:
        """Compose one assistant reply for ``case_id`` at ``stage`` and
        hand it to the outbound queue.

        Returns the queue's ``item_id`` so the driver can correlate the
        eventual :class:`OutboundDispatched` signal back to this
        dispatch (and so callers can audit-link the call). Implementations
        should append the assistant turn to the SMS history and emit a
        ``sms.outbound`` audit event once the queue accepts the item.
        """
        ...

    async def record_inbound(
        self,
        *,
        case_id: CaseId,
        from_phone: str,
        body: str,
        message_sid: str,
    ) -> None:
        """Persist one inbound customer turn before reducer classification.

        Called by the driver as soon as the webhook resolves
        ``from_phone -> case_id``, before the
        :class:`InboundSmsReceived` signal is fanned out to the
        reducer. Doing the append here keeps the LLM's view of the
        conversation (when the reducer eventually triggers a reply
        turn) consistent with what the customer actually saw.
        """
        ...


class DealerSlotPort(Protocol):
    """Driver's boundary to the dealer's slot system.

    The simulator backs this with canned offered slots from the trigger
    plus an always-confirm semantic. At go-live this gets swapped for
    your colleague's tool with no driver changes.
    """

    async def list_slots(self, *, case: Case) -> tuple[OfferedSlot, ...]:
        """Return the currently bookable slots for this case's dealer.

        Called when the case enters RESCHEDULING (and on dealer reject,
        which also routes to RESCHEDULING). The driver wraps the
        response into a ``DealerSlotsListed`` signal for the case queue.
        """
        ...

    async def confirm_slot(self, *, case: Case, slot_id: SlotId) -> bool:
        """Ask the dealer to confirm one slot for this case.

        Returns ``True`` on confirmation (driver enqueues
        ``DealerConfirmed``), ``False`` on rejection (driver enqueues
        ``DealerRejected`` with a generic reason). Future iterations
        may replace the bool with a richer result enum.
        """
        ...


class GeofenceSubscription(Protocol):
    """Handle returned by ``GeofencePort.subscribe`` — cancel to stop."""

    def cancel(self) -> None:
        """Stop delivering events for this subscription."""
        ...


class GeofencePort(Protocol):
    """Subscribe to dealer geofence transitions for a vehicle.

    Production telematics pushes updates asynchronously; the simulator
    slider calls ``set_at_dealer`` on the concrete binding (see
    ``simulator._sim_ports.SimulatorGeofencePort``).
    """

    def subscribe(
        self,
        *,
        vehicle_vin: VehicleVin,
        on_event: Callable[[GeofenceEvent], None],
    ) -> GeofenceSubscription:
        """Register ``on_event`` for ``vehicle_vin``.

        Implementations must deliver ``entered`` when the vehicle
        crosses into the dealer geofence and ``exited`` when it leaves.
        Duplicate transitions (already inside, slider still "at dealer")
        must not fire.
        """
        ...


class TimerService(Protocol):
    """Schedule and cancel per-case wall-clock timers.

    The implementation is responsible for calling back into the driver
    (typically via the same ``on_signal`` entry the rest of the world
    uses) when a timer fires. The timer name selects which signal to
    raise — the canonical names are exposed as
    ``TIMER_INITIAL_REMINDER``, ``TIMER_FINAL_REMINDER``,
    ``TIMER_END_OF_DAY`` from ``guidepoint.case``.
    """

    def schedule(
        self,
        *,
        case_id: CaseId,
        name: str,
        fire_at: UtcDatetime,
    ) -> None:
        """Arm a one-shot timer. Replaces any existing timer with the same
        ``(case_id, name)`` pair."""
        ...

    def cancel(self, *, case_id: CaseId, name: str) -> None:
        """Cancel a previously-armed timer. No-op if none exists."""
        ...


__all__ = [
    "CallManager",
    "DealerSlotPort",
    "GeofenceEvent",
    "GeofenceEventKind",
    "GeofencePort",
    "GeofenceSubscription",
    "SmsDispatcher",
    "TimerService",
]
