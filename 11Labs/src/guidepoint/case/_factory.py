"""Pure transformation: Trigger + master data → Case.

No I/O. Tests can call this with hand-built records and assert on the
result. The clock and id factories are injected per architecture
rules #8 and #12.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

from guidepoint.case._models import (
    Case,
    CaseId,
    CaseState,
    Trigger,
    TriggerForeignKeyError,
)
from guidepoint.clock import Clock
from guidepoint.master_data import (
    CustomerRecord,
    DealerRecord,
    VehicleRecord,
)


def create_case_from_trigger(
    *,
    trigger: Trigger,
    customer: CustomerRecord,
    dealer: DealerRecord,
    vehicle: VehicleRecord,
    clock: Clock,
    case_id_factory: Callable[[], CaseId] = lambda: CaseId(f"case_{secrets.token_hex(8)}"),
    correlation_id_factory: Callable[[], str] = lambda: f"corr_{secrets.token_hex(6)}",
) -> Case:
    """Snapshot master data into a new Case in ``state=CREATED``.

    Validates that the master records the trigger references match each
    other before snapshotting:

    - the vehicle's ``owner_id`` must equal the customer's ``id``
    - the dealer's ``id`` must equal the trigger's ``dealer_id``
    - the vehicle's ``vin`` must equal the trigger's ``vehicle_vin``

    Raises ``TriggerForeignKeyError`` on any mismatch so the monitor
    task can mark the trigger ``status='failed'`` and move on.
    """
    if vehicle.vin != trigger.vehicle_vin:
        raise TriggerForeignKeyError(
            trigger.id, f"vehicle vin mismatch ({vehicle.vin!r} vs {trigger.vehicle_vin!r})"
        )
    if vehicle.owner_id != customer.id:
        raise TriggerForeignKeyError(
            trigger.id,
            f"customer/vehicle owner mismatch ({customer.id!r} vs {vehicle.owner_id!r})",
        )
    if dealer.id != trigger.dealer_id:
        raise TriggerForeignKeyError(
            trigger.id, f"dealer mismatch ({dealer.id!r} vs {trigger.dealer_id!r})"
        )

    return Case(
        case_id=case_id_factory(),
        trigger_id=trigger.id,
        correlation_id=correlation_id_factory(),
        customer=customer,
        dealer=dealer,
        vehicle=vehicle,
        service_event=trigger.service_event,
        offered_slots=trigger.offered_slots,
        channel=trigger.channel_preference,
        user_id=trigger.user_id,
        state=CaseState.CREATED,
        attempt_count=0,
        next_attempt_at=None,
        call_attempts=(),
        events=(),
        outcome_detail="",
        booked_slot_id=None,
        created_at=clock.now(),
        closed_at=None,
    )


__all__ = ["create_case_from_trigger"]
