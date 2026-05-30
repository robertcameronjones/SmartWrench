"""Persistence backends for case storage.

``build_case_repository(backend=...)`` selects JSON files (default,
tests) or SQLite (simulator / production pilot). Master data stays
JSON per-user for now.
"""

from __future__ import annotations

import os
from pathlib import Path

from guidepoint.case._repository import (
    CaseRepository,
    JsonCasePaths,
    build_json_case_repository,
)
from guidepoint.persistence._migrate_from_json import migrate_cases_json_to_sqlite
from guidepoint.persistence._outbound import (
    OutboundItem,
    OutboundKind,
    OutboundQueue,
    OutboundState,
)
from guidepoint.persistence.sqlite._case_repo import build_sqlite_case_repository
from guidepoint.persistence.sqlite._outbound_queue import build_sqlite_outbound_queue

PersistenceBackend = str


def build_case_repository(
    *,
    backend: PersistenceBackend | None = None,
    project_root: Path,
    db_path: Path | None = None,
    migrate_json: bool = True,
) -> CaseRepository:
    """Construct a ``CaseRepository`` for ``backend`` (``json`` or ``sqlite``).

    When ``backend=sqlite`` and the database file is new / empty, any
    existing JSON cases under ``fixtures/cases/`` are copied in once.
    """
    resolved = (backend or os.environ.get("PERSISTENCE") or "json").strip().lower()
    if resolved == "sqlite":
        path = (db_path or (project_root / "data" / "guidepoint.db")).resolve()
        repo = build_sqlite_case_repository(db_path=path)
        if migrate_json and _sqlite_is_empty(path):
            json_paths = JsonCasePaths.for_root(project_root)
            migrated = migrate_cases_json_to_sqlite(json_paths=json_paths, db_path=path)
            if migrated:
                import structlog

                structlog.get_logger(__name__).info(
                    "persistence.migrated_json_cases",
                    count=migrated,
                    db_path=str(path),
                )
        return repo
    if resolved == "json":
        return build_json_case_repository(paths=JsonCasePaths.for_root(project_root))
    raise ValueError(f"unknown PERSISTENCE backend {resolved!r}; expected 'json' or 'sqlite'")


def _sqlite_is_empty(db_path: Path) -> bool:
    if not db_path.exists():
        return True
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM cases").fetchone()
    return row is not None and row[0] == 0


__all__ = [
    "OutboundItem",
    "OutboundKind",
    "OutboundQueue",
    "OutboundState",
    "PersistenceBackend",
    "build_case_repository",
    "build_sqlite_outbound_queue",
    "migrate_cases_json_to_sqlite",
]
