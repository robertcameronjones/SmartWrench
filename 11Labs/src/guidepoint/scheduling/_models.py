"""Frozen value types and errors for the scheduling module.

All public types here are exposed via ``scheduling/__init__.py``. Internal
helpers stay private (underscore-prefixed names are blocked from cross-module
import by Pyright's ``reportPrivateUsage`` rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NewType, Protocol, final

# Phantom types: distinct strings the type checker treats as different types.
# Prevents passing a CustomerId where a DealerId is expected.
CustomerId = NewType("CustomerId", str)
DealerId = NewType("DealerId", str)
SlotId = NewType("SlotId", str)
AppointmentId = NewType("AppointmentId", str)


@final
@dataclass(frozen=True, slots=True)
class Slot:
    """A single bookable service-appointment slot at a dealer."""

    id: SlotId
    dealer_id: DealerId
    starts_at: datetime  # tz-aware UTC; enforced at construction by validators upstream
    duration_minutes: int


@final
@dataclass(frozen=True, slots=True)
class Appointment:
    """A booked service appointment, returned by ``SchedulingService.book``."""

    id: AppointmentId
    slot: Slot
    customer_id: CustomerId
    booked_at: datetime


class SlotProvider(Protocol):
    """Port for whatever system actually owns slot availability.

    The dealer DMS is the canonical implementation. Tests substitute an
    in-memory fake. The scheduling service depends on this Protocol, never on
    a concrete implementation.
    """

    def list_open(self, *, dealer_id: DealerId) -> tuple[Slot, ...]:
        """Return all currently open slots for a dealer, oldest first."""
        ...

    def reserve(self, *, slot_id: SlotId, customer_id: CustomerId) -> Appointment:
        """Atomically reserve a slot for a customer.

        Raises:
            SlotUnavailableError: the slot was taken or no longer exists.
        """
        ...


class BookingError(Exception):
    """Base class for all expected booking-flow failures."""


@final
class SlotUnavailableError(BookingError):
    """The requested slot is no longer available."""

    def __init__(self, slot_id: SlotId) -> None:
        super().__init__(f"Slot {slot_id!r} is not available")
        self.slot_id = slot_id
