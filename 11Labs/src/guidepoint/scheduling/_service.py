"""Concrete scheduling implementation and its factory.

The implementation class ``_SchedulingImpl`` is unimportable across package
boundaries (Pyright ``reportPrivateUsage`` is ``error``). Construct one via
``build_scheduling``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from guidepoint.scheduling._models import (
    Appointment,
    CustomerId,
    DealerId,
    Slot,
    SlotId,
    SlotProvider,
)
from guidepoint.scheduling._protocol import SchedulingService

if TYPE_CHECKING:
    pass


@final
@dataclass(frozen=True, slots=True)
class _SchedulingImpl:
    """Default ``SchedulingService`` implementation.

    Pure orchestration over a ``SlotProvider``; no I/O of its own. Side effects
    happen inside the provider.
    """

    _slot_provider: SlotProvider

    def get_slots(self, *, dealer_id: DealerId) -> tuple[Slot, ...]:
        return self._slot_provider.list_open(dealer_id=dealer_id)

    def book(
        self,
        *,
        slot_id: SlotId,
        customer_id: CustomerId,
    ) -> Appointment:
        return self._slot_provider.reserve(
            slot_id=slot_id,
            customer_id=customer_id,
        )


def build_scheduling(*, slot_provider: SlotProvider) -> SchedulingService:
    """Construct the canonical ``SchedulingService``.

    All dependencies are injected. There are no module-level singletons, no
    globals, and no implicit defaults.

    Args:
        slot_provider: the port to whichever system actually owns slot
            availability (a real dealer DMS adapter in production; an
            in-memory fake in tests).
    """
    return _SchedulingImpl(_slot_provider=slot_provider)
