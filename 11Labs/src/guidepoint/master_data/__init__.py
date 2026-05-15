"""Master-data — public surface.

Reference records (customers, dealers, vehicles) and the repository
Protocol that loads / persists them. Today's only implementation is
file-backed (one JSON file per record); the production MySQL/Redshift
implementation will land behind the same Protocol.

See ``docs/adr/0006-case-lifecycle-and-normalized-fixtures.md`` for the
rationale and the relationship to ``guidepoint.case``.

Typical use::

    from pathlib import Path
    from guidepoint.master_data import (
        JsonFilePaths,
        build_json_master_data_repository,
    )

    repo = build_json_master_data_repository(
        paths=JsonFilePaths.for_root(Path.cwd()),
    )
    customer = repo.get_customer(CustomerId("cust_jones_robert"))
"""

from guidepoint.master_data._models import (
    CustomerId,
    CustomerNotFoundError,
    CustomerRecord,
    DealerId,
    DealerNotFoundError,
    DealerRecord,
    Location,
    MasterDataError,
    OptStatus,
    PreferredChannel,
    VehicleNotFoundError,
    VehicleRecord,
    VehicleVin,
)
from guidepoint.master_data._repository import (
    JsonFilePaths,
    MasterDataRepository,
    build_json_master_data_repository,
)

__all__ = [
    "CustomerId",
    "CustomerNotFoundError",
    "CustomerRecord",
    "DealerId",
    "DealerNotFoundError",
    "DealerRecord",
    "JsonFilePaths",
    "Location",
    "MasterDataError",
    "MasterDataRepository",
    "OptStatus",
    "PreferredChannel",
    "VehicleNotFoundError",
    "VehicleRecord",
    "VehicleVin",
    "build_json_master_data_repository",
]
