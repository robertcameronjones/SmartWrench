"""Scheduling — public surface.

The only names a consumer may import from this package. Implementation lives
in private modules (``_models``, ``_service``) and is unimportable across
package boundaries (Pyright ``reportPrivateUsage`` is set to ``error``).

Typical use::

    from guidepoint.scheduling import Slot, SchedulingService, build_scheduling

    scheduling: SchedulingService = build_scheduling(
        clock=my_clock,
        slot_provider=my_provider,
    )
    slots = scheduling.get_slots(dealer_id=DealerId("village-jeep"))
"""

from guidepoint.scheduling._models import (
    Appointment,
    AppointmentId,
    BookingError,
    CustomerId,
    DealerId,
    Slot,
    SlotId,
    SlotProvider,
    SlotUnavailableError,
)
from guidepoint.scheduling._protocol import SchedulingService
from guidepoint.scheduling._service import build_scheduling

__all__ = [
    "Appointment",
    "AppointmentId",
    "BookingError",
    "CustomerId",
    "DealerId",
    "SchedulingService",
    "Slot",
    "SlotId",
    "SlotProvider",
    "SlotUnavailableError",
    "build_scheduling",
]
