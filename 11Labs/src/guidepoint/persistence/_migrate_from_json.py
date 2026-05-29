"""One-shot migration from JSON case files into SQLite."""

from __future__ import annotations

from pathlib import Path

from guidepoint.case._models import Case
from guidepoint.case._repository import JsonCasePaths, build_json_case_repository
from guidepoint.persistence.sqlite._case_repo import build_sqlite_case_repository


def migrate_cases_json_to_sqlite(
    *,
    json_paths: JsonCasePaths,
    db_path: Path,
) -> int:
    """Copy every JSON case file into ``db_path``. Returns rows migrated."""
    source = build_json_case_repository(paths=json_paths)
    target = build_sqlite_case_repository(db_path=db_path)
    count = 0
    if not json_paths.cases_dir.exists():
        return 0
    for path in sorted(json_paths.cases_dir.glob("*.json")):
        case = Case.model_validate_json(path.read_text(encoding="utf-8"))
        target.save(case)
        count += 1
    return count


__all__ = ["migrate_cases_json_to_sqlite"]
