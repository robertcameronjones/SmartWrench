"""JSON-on-disk phone -> conversation_id routing.

One file at ``{path}`` holding ``{phone: conversation_id}``. Rewritten on
every change (the file is tiny — at most one entry per active SMS thread).

For multi-process safety we use ``fcntl.flock`` to serialize writes; reads
also acquire a shared lock so a partial-write isn't observed.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import final

from sms_adapter import RoutingStore


@final
class _JsonRoutingStore:
    def __init__(self, *, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("{}", encoding="utf-8")

    def _read_locked(self) -> dict[str, str]:
        with self._path.open("r", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                content = f.read()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return json.loads(content) if content.strip() else {}

    def _write_locked(self, data: dict[str, str]) -> None:
        with self._path.open("r+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                f.truncate()
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def bind(self, *, phone: str, conversation_id: str) -> None:
        data = self._read_locked()
        data[phone] = conversation_id
        self._write_locked(data)

    def unbind(self, *, phone: str) -> None:
        data = self._read_locked()
        if phone in data:
            del data[phone]
            self._write_locked(data)

    def find_conversation_id(self, phone: str) -> str | None:
        return self._read_locked().get(phone)


def build_json_routing_store(*, path: Path) -> RoutingStore:
    """Build a JSON-file routing store at ``path`` (single file)."""
    return _JsonRoutingStore(path=path)


__all__ = ["build_json_routing_store"]
