"""JSON-on-disk phone -> routing entry.

One file at ``{path}`` holding ``{phone: <entry>}``. Rewritten on every
change (the file is tiny — at most one entry per active SMS thread).

Format
======
Per-phone value is either:

- **legacy**: a bare string holding the ``conversation_id`` (left over
  from the original phase-1 design where the routing table only
  needed to thread inbound to a conversation).
- **new**: an object ``{"conversation_id": str, "user_id": str,
  "channel": str}`` so the inbound webhook can resolve the operator
  who owns the conversation without a second hop. ``user_id`` is
  empty when the binding was written by a path without operator
  identity. ``channel`` defaults to ``"sms"``.

Reads accept either; writes always produce the object form. Old
``routing.json`` files written by phase-1 code keep working through
the next process restart, which is when the upgrade-on-read converts
them.

For multi-process safety we use ``fcntl.flock`` to serialize writes;
reads also acquire a shared lock so a partial-write isn't observed.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Any, final

from sms_adapter import RoutingEntry, RoutingStore


@final
class _JsonRoutingStore:
    def __init__(self, *, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("{}", encoding="utf-8")

    def _read_locked(self) -> dict[str, Any]:
        with self._path.open("r", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                content = f.read()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return json.loads(content) if content.strip() else {}

    def _write_locked(self, data: dict[str, Any]) -> None:
        with self._path.open("r+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                f.truncate()
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def bind(
        self,
        *,
        phone: str,
        conversation_id: str,
        user_id: str = "",
        channel: str = "sms",
    ) -> None:
        data = self._read_locked()
        data[phone] = {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "channel": channel,
        }
        self._write_locked(data)

    def unbind(self, *, phone: str) -> None:
        data = self._read_locked()
        if phone in data:
            del data[phone]
            self._write_locked(data)

    def find_conversation_id(self, phone: str) -> str | None:
        entry = self.find_entry(phone)
        return None if entry is None else entry.conversation_id

    def find_entry(self, phone: str) -> RoutingEntry | None:
        raw = self._read_locked().get(phone)
        if raw is None:
            return None
        if isinstance(raw, str):
            return RoutingEntry(conversation_id=raw)
        if isinstance(raw, dict):
            cid = raw.get("conversation_id") or raw.get("case_id") or ""
            if not cid:
                return None
            return RoutingEntry(
                conversation_id=str(cid),
                user_id=str(raw.get("user_id") or ""),
                channel=str(raw.get("channel") or "sms"),
            )
        return None


def build_json_routing_store(*, path: Path) -> RoutingStore:
    """Build a JSON-file routing store at ``path`` (single file)."""
    return _JsonRoutingStore(path=path)


__all__ = ["build_json_routing_store"]
