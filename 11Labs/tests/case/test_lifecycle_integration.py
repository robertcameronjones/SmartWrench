"""End-to-end CaseDriver lifecycle scripts (Phase 11)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from guidepoint.case import (
    CallOutcome,
    CaseDriver,
    CaseEvent,
    CaseState,
    CustomerOptedOut,
    EndOfBusinessDayReached,
    FinalReminderDue,
    InitialReminderDue,
    JsonCasePaths,
    OfferedSlot,
    SlotId,
    VehicleEnteredDealer,
    VehicleExitedDealer,
    build_json_case_repository,
    create_case_from_trigger,
)
from guidepoint.events import build_event_bus
from guidepoint.master_data import VehicleVin

from tests.case._helpers import (
    FixedClock,
    sample_customer,
    sample_dealer,
    sample_trigger,
    sample_vehicle,
)
from tests.case.test_driver import (
    FakeCallManager,
    FakeDealerPort,
    FakeTimerService,
    _settle,
)

NOW = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
SLOT_TIME = datetime(2026, 5, 15, 13, 30, tzinfo=UTC)


def _outcome(*, business_outcome: str, slot: str | None = "slot_a") -> CallOutcome:
    return CallOutcome(
        result="answered",
        business_outcome=business_outcome,  # type: ignore[arg-type]
        booked_slot_id=SlotId(slot) if slot else None,
        started_at=NOW,
        ended_at=NOW,
        duration_seconds=30.0,
    )


@pytest.fixture
def repo(tmp_path):
    return build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))


@pytest.fixture
def bus():
    return build_event_bus(payload_type=CaseEvent)


@pytest.fixture
def timer_service():
    return FakeTimerService()


@pytest.fixture
def dealer_port():
    return FakeDealerPort(
        slots=(
            OfferedSlot(id=SlotId("slot_a"), starts_at=SLOT_TIME, display="Tuesday 8:30 AM"),
        )
    )


def _driver(repo, call_manager, dealer_port, timer_service, bus):
    return CaseDriver(
        case_repo=repo,
        call_manager=call_manager,
        dealer_port=dealer_port,
        timer_service=timer_service,
        bus=bus,
        clock=FixedClock(instant=NOW),
    )


@pytest.mark.asyncio
async def test_lifecycle_happy_path_to_completed(
    repo, dealer_port, timer_service, bus
) -> None:
    call_manager = FakeCallManager(
        script=[
            _outcome(business_outcome="booked"),
            _outcome(business_outcome="confirmed"),
            _outcome(business_outcome="feedback"),
        ]
    )
    driver = _driver(repo, call_manager, dealer_port, timer_service, bus)
    try:
        case_id = await driver.fire(
            trigger=sample_trigger(),
            customer=sample_customer(),
            dealer=sample_dealer(),
            vehicle=sample_vehicle(),
        )
        await _settle(driver)
        assert repo.get(case_id).state == CaseState.INITIAL_REMINDER_DUE

        await driver.on_signal(InitialReminderDue(timestamp=NOW, case_id=case_id))
        await _settle(driver)
        # Customer confirms at the initial reminder. State stays in
        # INITIAL_REMINDER_SENT — the final reminder fires from its own
        # timer on the day of the appointment.
        assert repo.get(case_id).state == CaseState.INITIAL_REMINDER_SENT

        await driver.on_signal(FinalReminderDue(timestamp=NOW, case_id=case_id))
        await _settle(driver)
        assert repo.get(case_id).state == CaseState.FINAL_REMINDER_SENT

        vin = VehicleVin("1C4RJFBG5NC123456")
        await driver.on_signal(VehicleEnteredDealer(timestamp=NOW, vehicle_vin=vin))
        await _settle(driver)
        assert repo.get(case_id).state == CaseState.SHOWED

        await driver.on_signal(VehicleExitedDealer(timestamp=NOW, vehicle_vin=vin))
        await _settle(driver)
        assert repo.get(case_id).state == CaseState.COMPLETED
    finally:
        await driver.shutdown()


@pytest.mark.asyncio
async def test_terminal_declined_from_outreach(repo, dealer_port, timer_service, bus) -> None:
    call_manager = FakeCallManager(default=_outcome(business_outcome="declined", slot=None))
    driver = _driver(repo, call_manager, dealer_port, timer_service, bus)
    try:
        case_id = await driver.fire(
            trigger=sample_trigger(),
            customer=sample_customer(),
            dealer=sample_dealer(),
            vehicle=sample_vehicle(),
        )
        await _settle(driver)
        assert repo.get(case_id).state == CaseState.DECLINED
    finally:
        await driver.shutdown()


@pytest.mark.asyncio
async def test_terminal_opted_out_from_outreach(repo, dealer_port, timer_service, bus) -> None:
    call_manager = FakeCallManager(default=_outcome(business_outcome="opted_out", slot=None))
    driver = _driver(repo, call_manager, dealer_port, timer_service, bus)
    try:
        case_id = await driver.fire(
            trigger=sample_trigger(),
            customer=sample_customer(),
            dealer=sample_dealer(),
            vehicle=sample_vehicle(),
        )
        await _settle(driver)
        assert repo.get(case_id).state == CaseState.OPTED_OUT
    finally:
        await driver.shutdown()


@pytest.mark.asyncio
async def test_terminal_no_show_on_end_of_day(repo, dealer_port, timer_service, bus) -> None:
    call_manager = FakeCallManager(default=_outcome(business_outcome="booked"))
    driver = _driver(repo, call_manager, dealer_port, timer_service, bus)
    try:
        case_id = await driver.fire(
            trigger=sample_trigger(),
            customer=sample_customer(),
            dealer=sample_dealer(),
            vehicle=sample_vehicle(),
        )
        await _settle(driver)
        case = repo.get(case_id)
        repo.save(case.model_copy(update={"state": CaseState.FINAL_REMINDER_SENT}))

        await driver.on_signal(EndOfBusinessDayReached(timestamp=NOW))
        await _settle(driver)
        assert repo.get(case_id).state == CaseState.NO_SHOW
    finally:
        await driver.shutdown()


@pytest.mark.asyncio
async def test_terminal_cancelled_post_booking(repo, dealer_port, timer_service, bus) -> None:
    call_manager = FakeCallManager(
        script=[_outcome(business_outcome="booked"), _outcome(business_outcome="cancelled", slot=None)]
    )
    driver = _driver(repo, call_manager, dealer_port, timer_service, bus)
    try:
        case_id = await driver.fire(
            trigger=sample_trigger(),
            customer=sample_customer(),
            dealer=sample_dealer(),
            vehicle=sample_vehicle(),
        )
        await _settle(driver)
        await driver.on_signal(InitialReminderDue(timestamp=NOW, case_id=case_id))
        await _settle(driver)
        assert repo.get(case_id).state == CaseState.CANCELLED
    finally:
        await driver.shutdown()


@pytest.mark.asyncio
async def test_customer_opted_out_signal_closes_mid_lifecycle(
    repo, dealer_port, timer_service, bus
) -> None:
    case = create_case_from_trigger(
        trigger=sample_trigger(),
        customer=sample_customer(),
        dealer=sample_dealer(),
        vehicle=sample_vehicle(),
        clock=FixedClock(instant=NOW),
    ).model_copy(update={"state": CaseState.INITIAL_REMINDER_SENT})
    repo.save(case)

    driver = _driver(repo, FakeCallManager(), dealer_port, timer_service, bus)
    try:
        await driver.recover_in_flight()
        await driver.on_signal(
            CustomerOptedOut(timestamp=NOW, customer_phone=case.customer.phone)
        )
        await _settle(driver)
        assert repo.get(case.case_id).state == CaseState.OPTED_OUT
    finally:
        await driver.shutdown()
