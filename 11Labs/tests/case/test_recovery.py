"""Startup recovery tests for ``CaseDriver.recover_in_flight`` (Phase 11)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from guidepoint.case import (
    CaseDriver,
    CaseEvent,
    CaseId,
    CaseState,
    FinalReminderDue,
    InitialReminderDue,
    JsonCasePaths,
    build_json_case_repository,
    create_case_from_trigger,
)
from guidepoint.events import build_event_bus

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

RECOVERABLE_STATES = (
    CaseState.CREATED,
    CaseState.CONTACTING_CUSTOMER,
    CaseState.INITIAL_REMINDER_DUE,
    CaseState.INITIAL_REMINDER_SENT,
    CaseState.FINAL_REMINDER_DUE,
    CaseState.FINAL_REMINDER_SENT,
    CaseState.SHOWED,
    CaseState.AWAITING_FEEDBACK,
)


@pytest.fixture
def repo(tmp_path):
    return build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))


@pytest.fixture
def bus():
    return build_event_bus(payload_type=CaseEvent)


@pytest.fixture
def driver(repo, bus):
    return CaseDriver(
        case_repo=repo,
        call_manager=FakeCallManager(),
        dealer_port=FakeDealerPort(),
        timer_service=FakeTimerService(),
        bus=bus,
        clock=FixedClock(instant=NOW),
    )


def _seed_case(repo, *, state: CaseState, case_id: str) -> str:
    vin = f"VIN{case_id}"
    customer_id = f"cust_{case_id}"
    case = create_case_from_trigger(
        trigger=sample_trigger(trigger_id=f"trig_{case_id}", vin=vin),
        customer=sample_customer(customer_id=customer_id),
        dealer=sample_dealer(),
        vehicle=sample_vehicle(vin=vin, owner=customer_id),
        clock=FixedClock(instant=NOW),
    ).model_copy(update={"state": state, "case_id": CaseId(case_id)})
    repo.save(case)
    return case_id


@pytest.mark.asyncio
@pytest.mark.parametrize("state", RECOVERABLE_STATES)
async def test_recover_spawns_loop_for_each_non_terminal_state(
    driver, repo, state: CaseState
) -> None:
    cid = _seed_case(repo, state=state, case_id=f"case_{state.value}")
    repo.save(
        create_case_from_trigger(
            trigger=sample_trigger(trigger_id="trig_terminal", vin="VINTERMINAL"),
            customer=sample_customer(customer_id="cust_terminal"),
            dealer=sample_dealer(),
            vehicle=sample_vehicle(vin="VINTERMINAL", owner="cust_terminal"),
            clock=FixedClock(instant=NOW),
        ).model_copy(update={"state": CaseState.COMPLETED, "case_id": CaseId("case_terminal")})
    )
    try:
        count = await driver.recover_in_flight()
        assert count == 1
        assert driver.active_case_count() == 1
        assert cid in driver.queue_depths()
    finally:
        await driver.shutdown()


@pytest.mark.asyncio
async def test_recovered_initial_reminder_due_accepts_signal(
    driver, repo
) -> None:
    case_id = _seed_case(
        repo, state=CaseState.INITIAL_REMINDER_DUE, case_id="case_recover"
    )
    try:
        await driver.recover_in_flight()
        await driver.on_signal(
            InitialReminderDue(timestamp=NOW, case_id=CaseId(case_id))
        )
        await _settle(driver)
        updated = repo.get(CaseId(case_id))
        assert updated.state in {
            CaseState.INITIAL_REMINDER_SENT,
            CaseState.FINAL_REMINDER_DUE,
            CaseState.CONTACTING_CUSTOMER,
        }
    finally:
        await driver.shutdown()


@pytest.mark.asyncio
async def test_recovered_final_reminder_due_accepts_signal(driver, repo) -> None:
    case_id = _seed_case(
        repo, state=CaseState.FINAL_REMINDER_DUE, case_id="case_dayof"
    )
    try:
        await driver.recover_in_flight()
        await driver.on_signal(
            FinalReminderDue(timestamp=NOW, case_id=CaseId(case_id))
        )
        await _settle(driver)
        assert repo.get(CaseId(case_id)).state == CaseState.FINAL_REMINDER_SENT
    finally:
        await driver.shutdown()
