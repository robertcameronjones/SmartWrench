"""In-memory fakes for the scheduling Protocols.

Lives in ``tests/`` because production code must never import a fake.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import final, override

from guidepoint.scheduling import (
    Appointment,
    AppointmentId,
    CustomerId,
    DealerId,
    Slot,
    SlotId,
    SlotProvider,
    SlotUnavailableError,
)


@final
class InMemorySlotProvider(SlotProvider):
    """Trivial ``SlotProvider`` for unit tests."""

    def __init__(self, *, slots: tuple[Slot, ...]) -> None:
        self._open: dict[SlotId, Slot] = {s.id: s for s in slots}
        self._booked: list[Appointment] = []
        self._next_appt = 0

    @override
    def list_open(self, *, dealer_id: DealerId) -> tuple[Slot, ...]:
        return tuple(s for s in self._open.values() if s.dealer_id == dealer_id)

    @override
    def reserve(self, *, slot_id: SlotId, customer_id: CustomerId) -> Appointment:
        slot = self._open.pop(slot_id, None)
        if slot is None:
            raise SlotUnavailableError(slot_id)
        self._next_appt += 1
        appt = Appointment(
            id=AppointmentId(f"appt-{self._next_appt:04d}"),
            slot=slot,
            customer_id=customer_id,
            booked_at=datetime.now(UTC),
        )
        self._booked.append(appt)
        return appt

    @property
    def booked(self) -> tuple[Appointment, ...]:
        return tuple(self._booked)
