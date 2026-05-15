"""Persistence Protocol for master data.

The Protocol is the seam: the simulator gets a JSON-file implementation,
production gets a MySQL/Redshift implementation. Nothing else in the
codebase imports a concrete repository — they ask the factory for
``MasterDataRepository``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, final

from pydantic import BaseModel

from guidepoint.master_data._models import (
    CustomerId,
    CustomerNotFoundError,
    CustomerRecord,
    DealerId,
    DealerNotFoundError,
    DealerRecord,
    VehicleNotFoundError,
    VehicleRecord,
    VehicleVin,
)


class MasterDataRepository(Protocol):
    """Read and write reference records (customers / dealers / vehicles).

    Each ``get_*`` method raises a typed ``*NotFoundError`` if the id is
    unknown. Each ``save_*`` method overwrites unconditionally — caller
    is responsible for any "is this an insert or update" semantics.
    """

    def get_customer(self, customer_id: CustomerId) -> CustomerRecord:
        """Return the customer or raise ``CustomerNotFoundError``."""
        ...

    def list_customers(self) -> Iterable[CustomerRecord]:
        """Return every customer record."""
        ...

    def save_customer(self, record: CustomerRecord) -> None:
        """Persist (overwrite) one customer record."""
        ...

    def get_dealer(self, dealer_id: DealerId) -> DealerRecord:
        """Return the dealer or raise ``DealerNotFoundError``."""
        ...

    def list_dealers(self) -> Iterable[DealerRecord]:
        """Return every dealer record."""
        ...

    def save_dealer(self, record: DealerRecord) -> None:
        """Persist (overwrite) one dealer record."""
        ...

    def get_vehicle(self, vin: VehicleVin) -> VehicleRecord:
        """Return the vehicle or raise ``VehicleNotFoundError``."""
        ...

    def list_vehicles(self) -> Iterable[VehicleRecord]:
        """Return every vehicle record."""
        ...

    def save_vehicle(self, record: VehicleRecord) -> None:
        """Persist (overwrite) one vehicle record."""
        ...


@final
@dataclass(frozen=True, slots=True)
class JsonFilePaths:
    """Resolved on-disk locations for the JSON-file repository.

    Each entity gets its own directory; one file per row. File names use
    the entity's primary key as the stem.
    """

    customers_dir: Path
    dealers_dir: Path
    vehicles_dir: Path

    @staticmethod
    def for_root(project_root: Path) -> JsonFilePaths:
        """Build the standard layout under ``project_root/fixtures/``."""
        base = (project_root / "fixtures").resolve()
        return JsonFilePaths(
            customers_dir=base / "customers",
            dealers_dir=base / "dealers",
            vehicles_dir=base / "vehicles",
        )


def build_json_master_data_repository(*, paths: JsonFilePaths) -> MasterDataRepository:
    """Construct the JSON-file ``MasterDataRepository``.

    Used by the simulator. The production MySQL repository lives in a
    sibling ``_mysql_repository.py`` (not yet written) and constructs
    via its own factory.
    """
    return _JsonFileMasterDataRepository(paths=paths)


@final
class _JsonFileMasterDataRepository:
    """One JSON file per record. No indexes, no caching, full reads."""

    def __init__(self, *, paths: JsonFilePaths) -> None:
        self._paths = paths

    # --------------------------- customers -----------------------------

    def get_customer(self, customer_id: CustomerId) -> CustomerRecord:
        path = self._paths.customers_dir / f"{customer_id}.json"
        if not path.exists():
            raise CustomerNotFoundError(customer_id)
        return CustomerRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_customers(self) -> Iterable[CustomerRecord]:
        return _read_all(self._paths.customers_dir, CustomerRecord)

    def save_customer(self, record: CustomerRecord) -> None:
        _write_one(self._paths.customers_dir, record.id, record)

    # --------------------------- dealers -------------------------------

    def get_dealer(self, dealer_id: DealerId) -> DealerRecord:
        path = self._paths.dealers_dir / f"{dealer_id}.json"
        if not path.exists():
            raise DealerNotFoundError(dealer_id)
        return DealerRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_dealers(self) -> Iterable[DealerRecord]:
        return _read_all(self._paths.dealers_dir, DealerRecord)

    def save_dealer(self, record: DealerRecord) -> None:
        _write_one(self._paths.dealers_dir, record.id, record)

    # --------------------------- vehicles ------------------------------

    def get_vehicle(self, vin: VehicleVin) -> VehicleRecord:
        path = self._paths.vehicles_dir / f"{vin}.json"
        if not path.exists():
            raise VehicleNotFoundError(vin)
        return VehicleRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_vehicles(self) -> Iterable[VehicleRecord]:
        return _read_all(self._paths.vehicles_dir, VehicleRecord)

    def save_vehicle(self, record: VehicleRecord) -> None:
        _write_one(self._paths.vehicles_dir, record.vin, record)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_all[R: BaseModel](directory: Path, model_cls: type[R]) -> tuple[R, ...]:
    if not directory.exists():
        return ()
    return tuple(
        model_cls.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted(directory.glob("*.json"))
    )


def _write_one(directory: Path, key: str, record: BaseModel) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = record.model_dump(mode="json")
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    (directory / f"{key}.json").write_text(text + "\n", encoding="utf-8")


__all__ = [
    "JsonFilePaths",
    "MasterDataRepository",
    "build_json_master_data_repository",
]
