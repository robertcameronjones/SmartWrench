"""Slots persistence — single ``fixtures/slots.json`` file.

Slots are master data the operator edits and saves like customers /
dealers / vehicles. They live in one flat array because ordering
matters (Kate reads them in order) and there's only ever a small set.

Production replaces this with a dealer-DMS query; the simulator just
reads and writes one file.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import final

from guidepoint.case import OfferedSlot


@final
class SlotsRepository:
    """Read and write the slot list from one JSON file."""

    def __init__(self, *, path: Path) -> None:
        self._path = path

    def list(self) -> tuple[OfferedSlot, ...]:
        if not self._path.exists():
            return ()
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return tuple(OfferedSlot.model_validate(s) for s in raw)

    def save(self, slots: Sequence[OfferedSlot]) -> tuple[OfferedSlot, ...]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [s.model_dump(mode="json") for s in slots]
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        self._path.write_text(text + "\n", encoding="utf-8")
        return tuple(slots)


def build_slots_repository(*, project_root: Path) -> SlotsRepository:
    """Construct the JSON-file slots repository at ``fixtures/slots.json``."""
    return SlotsRepository(path=(project_root / "fixtures" / "slots.json").resolve())


__all__ = ["SlotsRepository", "build_slots_repository"]
