"""Tests for the default CaseManager."""

from __future__ import annotations

from pathlib import Path

import pytest

from guidepoint.case import (
    CaseError,
    CaseEvent,
    CaseId,
    CaseState,
    JsonCasePaths,
    JsonTriggerPaths,
    RetryPolicy,
    build_default_case_manager,
    build_json_case_repository,
    build_json_trigger_source,
)
from guidepoint.events import build_event_bus
from guidepoint.master_data import (
    CustomerNotFoundError,
    JsonFilePaths,
    MasterDataError,
    build_json_master_data_repository,
)
from tests.case._helpers import (
    FakeBookedCallSession,
    FakeOptedOutCallSession,
    FixedClock,
    sample_customer,
    sample_dealer,
    sample_trigger,
    sample_vehicle,
)


def _scaffold(tmp_path: Path):  # type: ignore[no-untyped-def]
    master = build_json_master_data_repository(paths=JsonFilePaths.for_root(tmp_path))
    master.save_customer(sample_customer())
    master.save_dealer(sample_dealer())
    master.save_vehicle(sample_vehicle())
    triggers = build_json_trigger_source(paths=JsonTriggerPaths.for_root(tmp_path))
    triggers.save(sample_trigger())
    cases = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    bus = build_event_bus(payload_type=CaseEvent)
    clock = FixedClock()
    manager = build_default_case_manager(
        master_data=master,
        case_repo=cases,
        trigger_source=triggers,
        call_session=FakeBookedCallSession(),
        bus=bus,
        clock=clock,
        retry_policy=RetryPolicy(),
    )
    return manager, master, triggers, cases


@pytest.mark.asyncio
async def test_fire_creates_case_and_books_slot(tmp_path: Path) -> None:
    manager, _master, triggers, cases = _scaffold(tmp_path)
    case = await manager.fire(sample_trigger())
    assert case.state == CaseState.BOOKED
    assert case.booked_slot_id == "slot_a"
    persisted = cases.get(case.case_id)
    assert persisted.state == CaseState.BOOKED
    fired_trigger = triggers.get(sample_trigger().id)
    assert fired_trigger.status == "fired"


@pytest.mark.asyncio
async def test_fire_with_unresolvable_master_data_marks_trigger_failed(
    tmp_path: Path,
) -> None:
    triggers = build_json_trigger_source(paths=JsonTriggerPaths.for_root(tmp_path))
    triggers.save(sample_trigger())
    cases = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    bus = build_event_bus(payload_type=CaseEvent)
    clock = FixedClock()
    manager = build_default_case_manager(
        master_data=build_json_master_data_repository(paths=JsonFilePaths.for_root(tmp_path)),
        case_repo=cases,
        trigger_source=triggers,
        call_session=FakeBookedCallSession(),
        bus=bus,
        clock=clock,
    )
    with pytest.raises(MasterDataError):
        await manager.fire(sample_trigger())
    failed = triggers.get(sample_trigger().id)
    assert failed.status == "failed"


@pytest.mark.asyncio
async def test_fire_records_call_attempt(tmp_path: Path) -> None:
    manager, _master, _triggers, cases = _scaffold(tmp_path)
    case = await manager.fire(sample_trigger())
    persisted = cases.get(case.case_id)
    assert persisted.attempt_count == 1
    assert persisted.call_attempts[0].outcome.business_outcome == "booked"


@pytest.mark.asyncio
async def test_cancel_marks_terminal_state(tmp_path: Path) -> None:
    manager, _master, _triggers, _cases = _scaffold(tmp_path)
    case = await manager.fire(sample_trigger())
    cancelled = await manager.cancel(CaseId(case.case_id), reason="test cancel")
    assert cancelled.state == CaseState.CANCELLED
    assert "test cancel" in cancelled.outcome_detail


@pytest.mark.asyncio
async def test_fire_with_mismatched_records_marks_trigger_failed(tmp_path: Path) -> None:
    """If the trigger's foreign keys don't line up, the manager surfaces a typed error."""
    master = build_json_master_data_repository(paths=JsonFilePaths.for_root(tmp_path))
    master.save_customer(sample_customer())
    master.save_dealer(sample_dealer())
    # Vehicle owner points at a customer that doesn't exist in master data.
    master.save_vehicle(sample_vehicle(owner="someone_else"))
    triggers = build_json_trigger_source(paths=JsonTriggerPaths.for_root(tmp_path))
    triggers.save(sample_trigger())
    cases = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    bus = build_event_bus(payload_type=CaseEvent)
    clock = FixedClock()
    manager = build_default_case_manager(
        master_data=master,
        case_repo=cases,
        trigger_source=triggers,
        call_session=FakeBookedCallSession(),
        bus=bus,
        clock=clock,
    )
    # Customer lookup fails before the FK check fires (someone_else has no record).
    with pytest.raises(CustomerNotFoundError):
        await manager.fire(sample_trigger())
    assert triggers.get(sample_trigger().id).status == "failed"


@pytest.mark.asyncio
async def test_fire_opted_out_closes_case_and_updates_master_data(tmp_path: Path) -> None:
    master = build_json_master_data_repository(paths=JsonFilePaths.for_root(tmp_path))
    master.save_customer(sample_customer())
    master.save_dealer(sample_dealer())
    master.save_vehicle(sample_vehicle())
    triggers = build_json_trigger_source(paths=JsonTriggerPaths.for_root(tmp_path))
    triggers.save(sample_trigger())
    cases = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    bus = build_event_bus(payload_type=CaseEvent)
    clock = FixedClock()
    manager = build_default_case_manager(
        master_data=master,
        case_repo=cases,
        trigger_source=triggers,
        call_session=FakeOptedOutCallSession(),
        bus=bus,
        clock=clock,
        retry_policy=RetryPolicy(),
    )
    case = await manager.fire(sample_trigger())
    assert case.state == CaseState.OPTED_OUT
    customer = master.get_customer(sample_customer().id)
    assert customer.opt_status == "opted_out"


@pytest.mark.asyncio
async def test_start_rejects_sms_when_customer_opted_out(tmp_path: Path) -> None:
    master = build_json_master_data_repository(paths=JsonFilePaths.for_root(tmp_path))
    master.save_customer(
        sample_customer().model_copy(update={"opt_status": "opted_out"})
    )
    master.save_dealer(sample_dealer())
    master.save_vehicle(sample_vehicle())
    triggers = build_json_trigger_source(paths=JsonTriggerPaths.for_root(tmp_path))
    trigger = sample_trigger().model_copy(update={"channel_preference": "sms"})
    triggers.save(trigger)
    cases = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))
    bus = build_event_bus(payload_type=CaseEvent)
    clock = FixedClock()
    manager = build_default_case_manager(
        master_data=master,
        case_repo=cases,
        trigger_source=triggers,
        call_sessions={"voice": FakeBookedCallSession(), "sms": FakeBookedCallSession()},
        bus=bus,
        clock=clock,
    )
    with pytest.raises(CaseError, match="opted out"):
        await manager.start(trigger)
    assert triggers.get(trigger.id).status == "failed"
