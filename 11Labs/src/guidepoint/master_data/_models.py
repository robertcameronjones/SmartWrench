"""Boundary models for the master-data domain.

Master data is the *reference* information that exists independently of
any one phone call: customers, dealers, vehicles. These records are
edited and looked up; they are never created by the call lifecycle.
A new customer or vehicle entering the system arrives via an upstream
write (CSV import, dealer onboarding, telematics enrollment) — not via
``CaseManager``.

In production the rows live in MySQL. In simulator mode they live as
JSON files under ``fixtures/customers/`` etc. ``MasterDataRepository``
abstracts the storage; this file just defines the shapes.

Per ADR 0006 these records are intentionally separate from the
``case.Case`` snapshot: a Case carries a *frozen copy* of these records
captured at trigger-fire time so the audit trail is stable even if the
underlying record is later edited.
"""

from __future__ import annotations

from typing import Literal, NewType, final

from pydantic import BaseModel, ConfigDict, Field

# Phantom-typed ids: distinct strings the type checker treats as different
# types. Prevents passing a CustomerId where a DealerId is expected.
CustomerId = NewType("CustomerId", str)
DealerId = NewType("DealerId", str)
VehicleVin = NewType("VehicleVin", str)

OptStatus = Literal["opted_in", "opted_out", "unknown"]
PreferredChannel = Literal["voice", "sms", "email", "unknown"]


def _frozen_strict() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class Location(BaseModel):
    """A geographic point with a human-readable label.

    Used today for the vehicle's current location (telematics-reported);
    could later carry the dealer's service-bay coordinates.
    """

    model_config = _frozen_strict()

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    description: str = Field(min_length=1)


class CustomerRecord(BaseModel):
    """A person who may be called.

    Owned outside the call lifecycle: this record exists before any
    Trigger references it and persists after every Case for this customer
    has closed. The ``id`` is the foreign key both Vehicles and the
    snapshot inside a Case point at.
    """

    model_config = _frozen_strict()

    id: CustomerId
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    phone: str = Field(min_length=7)
    opt_status: OptStatus = "opted_in"
    preferred_channel: PreferredChannel = "unknown"
    timezone: str = Field(min_length=3, default="UTC")

    @property
    def full_name(self) -> str:
        """First + last, recomputed on access (frozen model so safe)."""
        return f"{self.first_name} {self.last_name}"

    @property
    def sms_consent(self) -> bool:
        """Whether outbound SMS is permitted for this customer.

        Maps to ``opt_status``: only an explicit ``opted_out`` blocks
        sends. New customers default to ``opted_in``; legacy ``unknown``
        records are still treated as consent-not-withdrawn.
        """
        return self.opt_status != "opted_out"


class DealerRecord(BaseModel):
    """A dealership Kate can represent on a call."""

    model_config = _frozen_strict()

    id: DealerId
    name: str = Field(min_length=1)
    phone: str = Field(min_length=7)
    address: str = Field(min_length=1)
    ride_radius_miles: int = Field(ge=0, le=500)


class VehicleRecord(BaseModel):
    """A specific vehicle, identified by VIN.

    Carries an ``owner_id`` foreign key to ``CustomerRecord``. A trigger
    references the vehicle (by VIN); the customer is derived through the
    vehicle. If a future product needs loaner/fleet handling, an
    explicit ``customer_id_override`` lands on the Trigger then, not
    here.
    """

    model_config = _frozen_strict()

    vin: VehicleVin
    owner_id: CustomerId
    year: int = Field(ge=1980, le=2100)
    make: str = Field(min_length=1)
    model: str = Field(min_length=1)
    odometer_miles: int = Field(ge=0, le=1_000_000)
    current_location: Location


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MasterDataError(Exception):
    """Base class for all expected master-data failures."""


@final
class CustomerNotFoundError(MasterDataError):
    """No customer with the requested id."""

    def __init__(self, customer_id: CustomerId) -> None:
        super().__init__(f"Customer {customer_id!r} not found")
        self.customer_id = customer_id


@final
class DealerNotFoundError(MasterDataError):
    """No dealer with the requested id."""

    def __init__(self, dealer_id: DealerId) -> None:
        super().__init__(f"Dealer {dealer_id!r} not found")
        self.dealer_id = dealer_id


@final
class VehicleNotFoundError(MasterDataError):
    """No vehicle with the requested VIN."""

    def __init__(self, vin: VehicleVin) -> None:
        super().__init__(f"Vehicle {vin!r} not found")
        self.vin = vin


__all__ = [
    "CustomerId",
    "CustomerNotFoundError",
    "CustomerRecord",
    "DealerId",
    "DealerNotFoundError",
    "DealerRecord",
    "Location",
    "MasterDataError",
    "OptStatus",
    "PreferredChannel",
    "VehicleNotFoundError",
    "VehicleRecord",
    "VehicleVin",
]
