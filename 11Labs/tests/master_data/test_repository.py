"""JSON-file MasterDataRepository round-trip tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from guidepoint.master_data import (
    CustomerId,
    CustomerNotFoundError,
    CustomerRecord,
    DealerId,
    DealerNotFoundError,
    DealerRecord,
    JsonFilePaths,
    Location,
    VehicleNotFoundError,
    VehicleRecord,
    VehicleVin,
    build_json_master_data_repository,
)


def _customer() -> CustomerRecord:
    return CustomerRecord(
        id=CustomerId("c"),
        first_name="A",
        last_name="B",
        phone="5550000",
    )


def _dealer() -> DealerRecord:
    return DealerRecord(
        id=DealerId("d"),
        name="x",
        phone="5550000",
        address="1 Main St",
        ride_radius_miles=10,
    )


def _vehicle() -> VehicleRecord:
    return VehicleRecord(
        vin=VehicleVin("1C4RJFBG5NC123456"),
        owner_id=CustomerId("c"),
        year=2025,
        make="Jeep",
        model="GC",
        odometer_miles=100,
        current_location=Location(latitude=42.0, longitude=-83.0, description="here"),
    )


class TestJsonFilePaths:
    def test_layout_under_root(self, tmp_path: Path) -> None:
        paths = JsonFilePaths.for_root(tmp_path)
        assert paths.customers_dir == (tmp_path / "fixtures" / "customers").resolve()
        assert paths.dealers_dir == (tmp_path / "fixtures" / "dealers").resolve()
        assert paths.vehicles_dir == (tmp_path / "fixtures" / "vehicles").resolve()


class TestRoundTrip:
    def test_customer(self, tmp_path: Path) -> None:
        repo = build_json_master_data_repository(paths=JsonFilePaths.for_root(tmp_path))
        repo.save_customer(_customer())
        assert repo.get_customer(CustomerId("c")) == _customer()
        assert tuple(repo.list_customers()) == (_customer(),)

    def test_dealer(self, tmp_path: Path) -> None:
        repo = build_json_master_data_repository(paths=JsonFilePaths.for_root(tmp_path))
        repo.save_dealer(_dealer())
        assert repo.get_dealer(DealerId("d")) == _dealer()

    def test_vehicle(self, tmp_path: Path) -> None:
        repo = build_json_master_data_repository(paths=JsonFilePaths.for_root(tmp_path))
        repo.save_vehicle(_vehicle())
        assert repo.get_vehicle(VehicleVin("1C4RJFBG5NC123456")) == _vehicle()


class TestNotFound:
    def test_customer(self, tmp_path: Path) -> None:
        repo = build_json_master_data_repository(paths=JsonFilePaths.for_root(tmp_path))
        with pytest.raises(CustomerNotFoundError):
            repo.get_customer(CustomerId("nope"))

    def test_dealer(self, tmp_path: Path) -> None:
        repo = build_json_master_data_repository(paths=JsonFilePaths.for_root(tmp_path))
        with pytest.raises(DealerNotFoundError):
            repo.get_dealer(DealerId("nope"))

    def test_vehicle(self, tmp_path: Path) -> None:
        repo = build_json_master_data_repository(paths=JsonFilePaths.for_root(tmp_path))
        with pytest.raises(VehicleNotFoundError):
            repo.get_vehicle(VehicleVin("1C4RJFBG5NC000000"))


class TestEmptyDirectories:
    def test_lists_return_empty(self, tmp_path: Path) -> None:
        repo = build_json_master_data_repository(paths=JsonFilePaths.for_root(tmp_path))
        assert tuple(repo.list_customers()) == ()
        assert tuple(repo.list_dealers()) == ()
        assert tuple(repo.list_vehicles()) == ()


class TestOverwrite:
    def test_save_customer_overwrites(self, tmp_path: Path) -> None:
        repo = build_json_master_data_repository(paths=JsonFilePaths.for_root(tmp_path))
        repo.save_customer(_customer())
        repo.save_customer(_customer().model_copy(update={"first_name": "Changed"}))
        assert repo.get_customer(CustomerId("c")).first_name == "Changed"
