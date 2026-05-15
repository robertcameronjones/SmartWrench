"""Validation tests for master-data record models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from guidepoint.master_data import (
    CustomerId,
    CustomerRecord,
    DealerId,
    DealerRecord,
    Location,
    VehicleRecord,
    VehicleVin,
)


class TestCustomerRecord:
    def test_full_name_is_first_plus_last(self) -> None:
        c = CustomerRecord(
            id=CustomerId("c"),
            first_name="Robert",
            last_name="Jones",
            phone="+13139095330",
        )
        assert c.full_name == "Robert Jones"

    def test_invalid_opt_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CustomerRecord.model_validate(
                {
                    "id": "c",
                    "first_name": "A",
                    "last_name": "B",
                    "phone": "5550000",
                    "opt_status": "maybe",
                }
            )

    def test_short_phone_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CustomerRecord(id=CustomerId("c"), first_name="A", last_name="B", phone="123")

    def test_default_opt_status_is_unknown(self) -> None:
        c = CustomerRecord(id=CustomerId("c"), first_name="A", last_name="B", phone="5550000")
        assert c.opt_status == "unknown"
        assert c.preferred_channel == "unknown"

    def test_round_trip(self) -> None:
        c = CustomerRecord(
            id=CustomerId("c"),
            first_name="A",
            last_name="B",
            phone="5550000",
            opt_status="opted_in",
        )
        assert CustomerRecord.model_validate(c.model_dump()) == c


class TestDealerRecord:
    def test_round_trip(self) -> None:
        d = DealerRecord(
            id=DealerId("d"),
            name="Village Jeep",
            phone="5550000",
            address="1 Main St",
            ride_radius_miles=10,
        )
        assert DealerRecord.model_validate(d.model_dump()) == d

    def test_negative_radius_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DealerRecord(
                id=DealerId("d"),
                name="x",
                phone="5550000",
                address="1 Main St",
                ride_radius_miles=-1,
            )


class TestVehicleRecord:
    def test_round_trip(self) -> None:
        v = VehicleRecord(
            vin=VehicleVin("1C4RJFBG5NC123456"),
            owner_id=CustomerId("c"),
            year=2025,
            make="Jeep",
            model="GC",
            odometer_miles=100,
            current_location=Location(latitude=42.0, longitude=-83.0, description="here"),
        )
        assert VehicleRecord.model_validate(v.model_dump()) == v

    def test_invalid_year_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VehicleRecord(
                vin=VehicleVin("1C4RJFBG5NC123456"),
                owner_id=CustomerId("c"),
                year=1900,
                make="x",
                model="y",
                odometer_miles=0,
                current_location=Location(latitude=0.0, longitude=0.0, description="x"),
            )


class TestLocation:
    def test_lat_bounds(self) -> None:
        with pytest.raises(ValidationError):
            Location(latitude=120.0, longitude=0.0, description="x")
        with pytest.raises(ValidationError):
            Location(latitude=0.0, longitude=200.0, description="x")
