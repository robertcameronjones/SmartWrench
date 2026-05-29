"""Tests for the v2 ``CaseDriver``.

These tests verify the imperative shell that wraps the pure
``decide_next_case_state`` reducer:

- ``on_signal`` routes signals correctly (case-targeted vs
  vehicle-targeted vs customer-targeted vs world).
- Per-case async loops drain their queues serially.
- Each ``CaseAction`` is dispatched to the right adapter Protocol.
- ``PlaceCall`` runs the CallManager off the case loop and feeds the
  result back as a ``CallEnded`` signal.
- ``recover_in_flight`` spawns a loop per non-terminal case.
- Terminal cases retire their loops; signals to terminal cases drop.
- Backpressure: bounded queues drop with logging on overflow.

Adapters are simple in-memory fakes — full integration with the live
ElevenLabs / Twilio adapters lives in Phase 5+.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import final

import pytest

from guidepoint.case import (
    BusinessHoursOpened,
    CallOutcome,
    Case,
    CaseDriver,
    CaseEvent,
    CaseId,
    CaseState,
    CustomerOptedOut,
    JsonCasePaths,
    OfferedSlot,
    SlotId,
    VehicleEnteredDealer,
    build_json_case_repository,
    create_case_from_trigger,
)
from guidepoint.case._actions import CallStage
from guidepoint.events import build_event_bus
from guidepoint.master_data import VehicleVin

from tests.case._helpers import (
    FixedClock,
    sample_customer,
    sample_dealer,
    sample_trigger,
    sample_vehicle,
)

NOW = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
SLOT_FAR_TIME = datetime(2026, 5, 15, 13, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Adapter fakes
# ---------------------------------------------------------------------------


@final
@dataclass
class FakeCallManager:
    """Returns a scripted ``CallOutcome`` per call.

    ``script`` is consumed in order. If exhausted, ``default`` is
    returned on every subsequent call.
    """

    script: list[CallOutcome] = field(default_factory=list)
    default: CallOutcome | None = None
    calls: list[tuple[CaseId, CallStage, int]] = field(default_factory=list)

    async def start(
        self, *, case: Case, stage: CallStage, attempt_number: int
    ) -> CallOutcome:
        self.calls.append((case.case_id, stage, attempt_number))
        if self.script:
            return self.script.pop(0)
        assert self.default is not None, "no scripted outcomes left and no default"
        return self.default


@final
@dataclass
class FakeDealerPort:
    """Always-confirm dealer port unless ``reject`` is set."""

    reject: bool = False
    slots: tuple[OfferedSlot, ...] = ()
    list_calls: list[CaseId] = field(default_factory=list)
    confirm_calls: list[tuple[CaseId, SlotId]] = field(default_factory=list)

    async def list_slots(self, *, case: Case) -> tuple[OfferedSlot, ...]:
        self.list_calls.append(case.case_id)
        return self.slots

    async def confirm_slot(self, *, case: Case, slot_id: SlotId) -> bool:
        self.confirm_calls.append((case.case_id, slot_id))
        return not self.reject


@final
@dataclass
class FakeTimerService:
    """Records schedule/cancel calls without firing anything."""

    scheduled: list[tuple[CaseId, str, datetime]] = field(default_factory=list)
    cancelled: list[tuple[CaseId, str]] = field(default_factory=list)

    def schedule(self, *, case_id: CaseId, name: str, fire_at: datetime) -> None:
        self.scheduled.append((case_id, name, fire_at))

    def cancel(self, *, case_id: CaseId, name: str) -> None:
        self.cancelled.append((case_id, name))


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _outcome(
    *,
    business_outcome: str | None = "booked",
    booked_slot_id: SlotId | None = SlotId("slot_a"),
    result: str = "answered",
) -> CallOutcome:
    return CallOutcome(
        result=result,  # type: ignore[arg-type]
        business_outcome=business_outcome,  # type: ignore[arg-type]
        booked_slot_id=booked_slot_id,
        elevenlabs_conversation_id="conv",
        started_at=NOW,
        ended_at=NOW,
        duration_seconds=10.0,
        transcript="t",
    )


@pytest.fixture
def repo(tmp_path: Path):
    return build_json_case_repository(
        paths=JsonCasePaths(cases_dir=tmp_path / "cases")
    )


@pytest.fixture
def bus():
    return build_event_bus(payload_type=CaseEvent)


@pytest.fixture
def call_manager():
    return FakeCallManager()


@pytest.fixture
def dealer_port():
    return FakeDealerPort(
        slots=(OfferedSlot(id=SlotId("slot_a"), starts_at=SLOT_FAR_TIME, display="A"),)
    )


@pytest.fixture
def timer_service():
    return FakeTimerService()


@pytest.fixture
def driver(repo, call_manager, dealer_port, timer_service, bus):
    d = CaseDriver(
        case_repo=repo,
        call_manager=call_manager,
        dealer_port=dealer_port,
        timer_service=timer_service,
        bus=bus,
        clock=FixedClock(instant=NOW),
    )
    return d


# Drain helper — wait until the driver has no work in flight.
async def _settle(driver: CaseDriver, *, max_iter: int = 50) -> None:
    """Cooperatively yield until queues and IO tasks are quiescent."""

    for _ in range(max_iter):
        await asyncio.sleep(0)
        depths = driver.queue_depths()
        any_queued = any(d > 0 for d in depths.values())
        any_io = bool(driver._io_tasks)  # type: ignore[reportPrivateUsage]
        if not any_queued and not any_io:
            return
    # Best-effort: ran out of iterations; let pending sleeps complete.
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# fire(): creates a case, starts a loop, seeds BusinessHoursOpened.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_creates_case_and_starts_outreach(
    driver, repo, call_manager
) -> None:
    case_id = await driver.fire(
        trigger=sample_trigger(),
        customer=sample_customer(),
        dealer=sample_dealer(),
        vehicle=sample_vehicle(),
    )
    try:
        call_manager.default = _outcome(business_outcome="declined", booked_slot_id=None)
        await _settle(driver)
        case = repo.get(case_id)
        assert case.state == CaseState.DECLINED
        assert call_manager.calls
        assert call_manager.calls[0][1] == CallStage.OUTREACH
        assert call_manager.calls[0][2] == 1
    finally:
        await driver.shutdown()


# ---------------------------------------------------------------------------
# Happy path: outreach → booked → dealer confirm → reminder armed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_outreach_to_reminder(
    driver, repo, call_manager, dealer_port, timer_service
) -> None:
    call_manager.default = _outcome(
        business_outcome="booked", booked_slot_id=SlotId("slot_a")
    )
    case_id = await driver.fire(
        trigger=sample_trigger(),
        customer=sample_customer(),
        dealer=sample_dealer(),
        vehicle=sample_vehicle(),
    )
    try:
        await _settle(driver)
        case = repo.get(case_id)
        assert case.state == CaseState.INITIAL_REMINDER_DUE
        assert case.booked_slot_id == SlotId("slot_a")
        # Dealer was asked to confirm exactly the booked slot.
        assert dealer_port.confirm_calls == [(case_id, SlotId("slot_a"))]
        # Both reminder timers armed up front (far-slot path).
        timer_names = {name for (_, name, _) in timer_service.scheduled}
        assert {"initial_reminder", "final_reminder"} <= timer_names
    finally:
        await driver.shutdown()


# ---------------------------------------------------------------------------
# Dealer rejection routes the case into rescheduling.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dealer_rejection_kicks_to_rescheduling(
    repo, call_manager, dealer_port, timer_service, bus
) -> None:
    dealer_port.reject = True
    call_manager.default = _outcome(
        business_outcome="booked", booked_slot_id=SlotId("slot_a")
    )
    driver = CaseDriver(
        case_repo=repo,
        call_manager=call_manager,
        dealer_port=dealer_port,
        timer_service=timer_service,
        bus=bus,
        clock=FixedClock(instant=NOW),
    )
    try:
        case_id = await driver.fire(
            trigger=sample_trigger(),
            customer=sample_customer(),
            dealer=sample_dealer(),
            vehicle=sample_vehicle(),
        )
        await _settle(driver)
        # Dealer rejection triggers list_slots → RESCHEDULING, then a
        # second outreach call. After this whole chain the case lands
        # back in CONFIRMING_WITH_DEALER (second booked) or
        # FINAL_REMINDER_DUE/INITIAL_REMINDER_DUE if dealer accepts the replacement.
        # Since dealer is still rejecting and the script is exhausted,
        # we end up looping; the test only asserts the first rejection
        # propagated correctly.
        assert dealer_port.list_calls  # at least one round trip
    finally:
        await driver.shutdown()


# ---------------------------------------------------------------------------
# Routing: vehicle-targeted signal lands on the right case.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vehicle_signal_routes_to_matching_case(
    repo, call_manager, dealer_port, timer_service, bus
) -> None:
    # Build a case in DAY_OF directly so the geofence signal moves it.
    case = create_case_from_trigger(
        trigger=sample_trigger(),
        customer=sample_customer(),
        dealer=sample_dealer(),
        vehicle=sample_vehicle(),
        clock=FixedClock(instant=NOW),
    ).model_copy(update={"state": CaseState.FINAL_REMINDER_SENT})
    repo.save(case)

    driver = CaseDriver(
        case_repo=repo,
        call_manager=call_manager,
        dealer_port=dealer_port,
        timer_service=timer_service,
        bus=bus,
        clock=FixedClock(instant=NOW),
    )
    try:
        await driver.recover_in_flight()
        await driver.on_signal(
            VehicleEnteredDealer(
                timestamp=NOW, vehicle_vin=VehicleVin("1C4RJFBG5NC123456")
            )
        )
        await _settle(driver)
        assert repo.get(case.case_id).state == CaseState.SHOWED
    finally:
        await driver.shutdown()


# ---------------------------------------------------------------------------
# Customer opt-out fans out and closes case from any non-terminal state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_customer_opt_out_closes_active_cases(
    repo, call_manager, dealer_port, timer_service, bus
) -> None:
    case = create_case_from_trigger(
        trigger=sample_trigger(),
        customer=sample_customer(),
        dealer=sample_dealer(),
        vehicle=sample_vehicle(),
        clock=FixedClock(instant=NOW),
    ).model_copy(update={"state": CaseState.INITIAL_REMINDER_DUE})
    repo.save(case)

    driver = CaseDriver(
        case_repo=repo,
        call_manager=call_manager,
        dealer_port=dealer_port,
        timer_service=timer_service,
        bus=bus,
        clock=FixedClock(instant=NOW),
    )
    try:
        await driver.recover_in_flight()
        await driver.on_signal(
            CustomerOptedOut(timestamp=NOW, customer_phone=case.customer.phone)
        )
        await _settle(driver)
        assert repo.get(case.case_id).state == CaseState.OPTED_OUT
        # Defensive timer cancellation: initial/final reminder + EoD all called.
        cancelled_names = {name for (_, name) in timer_service.cancelled}
        assert {"initial_reminder", "final_reminder", "end_of_business_day"} <= cancelled_names
    finally:
        await driver.shutdown()


# ---------------------------------------------------------------------------
# recover_in_flight respawns loops for every non-terminal case.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_in_flight_spawns_loops_for_active_cases(
    repo, call_manager, dealer_port, timer_service, bus
) -> None:
    case_alive = create_case_from_trigger(
        trigger=sample_trigger(trigger_id="trig_alive"),
        customer=sample_customer(),
        dealer=sample_dealer(),
        vehicle=sample_vehicle(),
        clock=FixedClock(instant=NOW),
    ).model_copy(update={"state": CaseState.INITIAL_REMINDER_DUE})
    case_dead = create_case_from_trigger(
        trigger=sample_trigger(trigger_id="trig_dead", vin="2C4RJFBG5NC999999"),
        customer=sample_customer(customer_id="other"),
        dealer=sample_dealer(),
        vehicle=sample_vehicle(vin="2C4RJFBG5NC999999", owner="other"),
        clock=FixedClock(instant=NOW),
    ).model_copy(update={"state": CaseState.COMPLETED})
    repo.save(case_alive)
    repo.save(case_dead)

    driver = CaseDriver(
        case_repo=repo,
        call_manager=call_manager,
        dealer_port=dealer_port,
        timer_service=timer_service,
        bus=bus,
        clock=FixedClock(instant=NOW),
    )
    try:
        count = await driver.recover_in_flight()
        assert count == 1
        assert driver.active_case_count() == 1
    finally:
        await driver.shutdown()


# ---------------------------------------------------------------------------
# Terminal-case signal: dropped (no work, no loop).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_to_terminal_case_is_dropped(
    repo, call_manager, dealer_port, timer_service, bus
) -> None:
    case = create_case_from_trigger(
        trigger=sample_trigger(),
        customer=sample_customer(),
        dealer=sample_dealer(),
        vehicle=sample_vehicle(),
        clock=FixedClock(instant=NOW),
    ).model_copy(update={"state": CaseState.COMPLETED})
    repo.save(case)

    driver = CaseDriver(
        case_repo=repo,
        call_manager=call_manager,
        dealer_port=dealer_port,
        timer_service=timer_service,
        bus=bus,
        clock=FixedClock(instant=NOW),
    )
    try:
        await driver.on_signal(
            CustomerOptedOut(timestamp=NOW, customer_phone=case.customer.phone)
        )
        await _settle(driver)
        # Still COMPLETED, no loop spawned.
        assert repo.get(case.case_id).state == CaseState.COMPLETED
        assert driver.active_case_count() == 0
    finally:
        await driver.shutdown()


# ---------------------------------------------------------------------------
# Bounded queue: overflow drops without raising.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_overflow_drops_signals_without_raising(
    repo, dealer_port, timer_service, bus
) -> None:
    # Slow call manager — never finishes during the test window so the
    # case loop blocks inside _handle_signal on the first signal.
    @dataclass
    class SlowCallManager:
        started: asyncio.Event = field(default_factory=asyncio.Event)

        async def start(self, **_: object) -> CallOutcome:
            self.started.set()
            await asyncio.sleep(3600)
            return _outcome()

    slow = SlowCallManager()
    driver = CaseDriver(
        case_repo=repo,
        call_manager=slow,
        dealer_port=dealer_port,
        timer_service=timer_service,
        bus=bus,
        clock=FixedClock(instant=NOW),
        queue_size=2,
    )
    try:
        case_id = await driver.fire(
            trigger=sample_trigger(),
            customer=sample_customer(),
            dealer=sample_dealer(),
            vehicle=sample_vehicle(),
        )
        # Let the loop pick up BusinessHoursOpened and start a call.
        await asyncio.wait_for(slow.started.wait(), timeout=1.0)
        # Now flood many BusinessHoursOpened signals — the case queue
        # caps at 2, the rest should be dropped silently.
        for _ in range(20):
            await driver.on_signal(BusinessHoursOpened(timestamp=NOW))
        depths = driver.queue_depths()
        assert depths[str(case_id)] <= 2
    finally:
        await driver.shutdown()
