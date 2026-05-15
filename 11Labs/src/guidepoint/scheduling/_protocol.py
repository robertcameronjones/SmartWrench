"""The public Protocol for the scheduling subsystem.

Re-exported from ``scheduling/__init__.py``. Consumers depend on this
Protocol; concrete implementations live in private modules.
"""

from __future__ import annotations

from typing import Protocol

from guidepoint.scheduling._models import (
    Appointment,
    CustomerId,
    DealerId,
    Slot,
    SlotId,
)


class SchedulingService(Protocol):
    """Find open service slots and book appointments.

    Backed by whichever ``SlotProvider`` is wired in via ``build_scheduling``.
    """

    def get_slots(self, *, dealer_id: DealerId) -> tuple[Slot, ...]:
        """Return open slots at a dealer, oldest first."""
        ...

    def book(
        self,
        *,
        slot_id: SlotId,
        customer_id: CustomerId,
    ) -> Appointment:
        """Book a slot for a customer.

        Raises:
            SlotUnavailableError: slot was taken between read and write.
        """
        ...
