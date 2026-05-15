"""Tests for scheduling value types — immutability and identity safety."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from guidepoint.scheduling import (
    AppointmentId,
    DealerId,
    Slot,
    SlotId,
    SlotUnavailableError,
)


class TestSlotImmutability:
    def test_slot_cannot_be_mutated(self) -> None:
        slot = Slot(
            id=SlotId("a"),
            dealer_id=DealerId("village-jeep"),
            starts_at=datetime(2026, 5, 11, 9, 0, tzinfo=UTC),
            duration_minutes=60,
        )

        with pytest.raises(FrozenInstanceError):
            slot.duration_minutes = 30  # type: ignore[misc]

    def test_slot_has_no_dunder_dict(self) -> None:
        slot = Slot(
            id=SlotId("a"),
            dealer_id=DealerId("village-jeep"),
            starts_at=datetime(2026, 5, 11, 9, 0, tzinfo=UTC),
            duration_minutes=60,
        )

        assert not hasattr(slot, "__dict__")  # slots=True


class TestSlotUnavailableError:
    def test_carries_slot_id(self) -> None:
        err = SlotUnavailableError(SlotId("a"))
        assert err.slot_id == "a"
        assert "a" in str(err)


class TestPhantomTypes:
    def test_ids_are_distinct_at_runtime_strings(self) -> None:
        # NewType is a type-checker concept; at runtime they're just strings.
        # This test documents that fact so a future contributor doesn't get
        # surprised. The protection is at type-check time (Pyright will
        # reject passing a CustomerId where a DealerId is expected).
        assert isinstance(SlotId("a"), str)
        assert isinstance(DealerId("a"), str)
        assert isinstance(AppointmentId("a"), str)
