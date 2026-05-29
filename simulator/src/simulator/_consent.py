"""SMS consent lookup across per-user master-data directories."""

from __future__ import annotations

from pathlib import Path
from typing import final

from guidepoint.master_data import CustomerRecord

from simulator._users import UserPaths, UserRegistry


@final
class ProjectSmsConsentChecker:
    """Scan user customer fixtures on disk for ``sms_consent`` by phone."""

    def __init__(self, *, project_root: Path, user_registry: UserRegistry) -> None:
        self._project_root = project_root
        self._user_registry = user_registry

    def sms_consent_for_phone(self, phone: str) -> bool:
        record = _find_customer_by_phone(
            project_root=self._project_root,
            user_registry=self._user_registry,
            phone=phone,
        )
        if record is None:
            return True
        return record.sms_consent


def _find_customer_by_phone(
    *,
    project_root: Path,
    user_registry: UserRegistry,
    phone: str,
) -> CustomerRecord | None:
    for user_id in user_registry.list_ids():
        paths = UserPaths.for_user(project_root=project_root, user_id=user_id)
        if not paths.customers_dir.exists():
            continue
        for path in sorted(paths.customers_dir.glob("*.json")):
            record = CustomerRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if record.phone == phone:
                return record
    return None


__all__ = [
    "ProjectSmsConsentChecker",
]
