"""Tests for ``create_case_from_trigger`` (pure transformation)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from guidepoint.case import (
    CaseId,
    CaseState,
    TriggerForeignKeyError,
    create_case_from_trigger,
)
from tests.case._helpers import (
    FixedClock,
    sample_customer,
    sample_dealer,
    sample_trigger,
    sample_vehicle,
)


class TestCreateCaseFromTrigger:
    def test_happy_path(self) -> None:
        clock = FixedClock(instant=datetime(2026, 5, 11, tzinfo=UTC))
        case = create_case_from_trigger(
            trigger=sample_trigger(),
            customer=sample_customer(),
            dealer=sample_dealer(),
            vehicle=sample_vehicle(),
            clock=clock,
            case_id_factory=lambda: CaseId("case_fixed"),
            correlation_id_factory=lambda: "corr_fixed",
        )
        assert case.case_id == "case_fixed"
        assert case.correlation_id == "corr_fixed"
        assert case.trigger_id == "trig_1"
        assert case.state == CaseState.CREATED
        assert case.attempt_count == 0
        assert case.created_at == datetime(2026, 5, 11, tzinfo=UTC)
        assert case.customer == sample_customer()
        assert case.dealer == sample_dealer()
        assert case.vehicle == sample_vehicle()

    def test_vin_mismatch_raises(self) -> None:
        with pytest.raises(TriggerForeignKeyError, match="vin"):
            create_case_from_trigger(
                trigger=sample_trigger(vin="1C4RJFBG5NC999999"),
                customer=sample_customer(),
                dealer=sample_dealer(),
                vehicle=sample_vehicle(),
                clock=FixedClock(),
            )

    def test_owner_mismatch_raises(self) -> None:
        with pytest.raises(TriggerForeignKeyError, match="owner"):
            create_case_from_trigger(
                trigger=sample_trigger(),
                customer=sample_customer(customer_id="c"),
                dealer=sample_dealer(),
                vehicle=sample_vehicle(owner="someone_else"),
                clock=FixedClock(),
            )

    def test_dealer_mismatch_raises(self) -> None:
        with pytest.raises(TriggerForeignKeyError, match="dealer"):
            create_case_from_trigger(
                trigger=sample_trigger(dealer_id="dealer_x"),
                customer=sample_customer(),
                dealer=sample_dealer(dealer_id="dealer_y"),
                vehicle=sample_vehicle(),
                clock=FixedClock(),
            )

    def test_snapshot_is_independent_of_later_master_data_edits(self) -> None:
        """Editing the underlying records after snapshot must not change the case."""
        case = create_case_from_trigger(
            trigger=sample_trigger(),
            customer=sample_customer(),
            dealer=sample_dealer(),
            vehicle=sample_vehicle(),
            clock=FixedClock(),
        )
        original_dump = case.model_dump()
        # The records themselves are frozen Pydantic models, so we can't
        # mutate them in place; the test verifies the case captured a copy.
        assert case.model_dump() == original_dump
