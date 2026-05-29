"""Slots persistence — single ``fixtures/slots.json`` file.

Slots are master data the operator edits and saves like customers /
dealers / vehicles. They live in one flat array because ordering
matters (Kate reads them in order) and there's only ever a small set.

Production replaces this with a dealer-DMS query; the simulator just
reads and writes one file.

The on-disk shape is the full :class:`OfferedSlot` (``id``,
``starts_at`` in UTC, ``display``) so the reducer / prompt machinery
reads it without translation. The simulator UI lets the operator
edit just a local date/time per slot via a picker; the rest is
server-derived (``derive_offered_slot``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from guidepoint.case import OfferedSlot, SlotId


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


# ---------------------------------------------------------------------------
# Slot derivation — local picker input → canonical OfferedSlot
# ---------------------------------------------------------------------------

# Accepted input from the browser ``<input type="datetime-local">``. Spec
# allows seconds but we don't ask for them; both are tolerated.
_LOCAL_FORMATS: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
)


def _parse_local_naive(text: str) -> datetime:
    """Parse a datetime-local string into a naive ``datetime``.

    Raises ``ValueError`` with a friendly message if neither shape matches.
    """

    for fmt in _LOCAL_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"starts_at_local {text!r} is not a valid YYYY-MM-DDTHH:MM datetime"
    )


def _format_display(local_dt: datetime) -> str:
    """Return ``"Tuesday, June 9, 2026 - 8:30 AM"`` from a local datetime.

    Hand-rolled instead of ``strftime`` because the un-padded ``%-d`` /
    ``%-I`` format specifiers aren't portable across platforms.
    """

    weekday = local_dt.strftime("%A")
    month = local_dt.strftime("%B")
    hour = local_dt.hour
    minute = local_dt.minute
    am_pm = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{weekday}, {month} {local_dt.day}, {local_dt.year} - {display_hour}:{minute:02d} {am_pm}"


def _slot_id_from_local(local_dt: datetime) -> str:
    """Return ``"slot_YYYY_MM_DD_HHMM"`` — stable, sortable, human-recognisable."""

    return f"slot_{local_dt:%Y_%m_%d_%H%M}"


def derive_offered_slot(*, starts_at_local: str, tz_name: str) -> OfferedSlot:
    """Build a full :class:`OfferedSlot` from operator-friendly input.

    Caller supplies the slot's local wall-clock time (as the browser's
    ``datetime-local`` picker emits) plus the dealer's IANA timezone.
    This function derives:

    - ``id`` — ``slot_YYYY_MM_DD_HHMM`` from the local time.
    - ``starts_at`` — absolute UTC instant (the canonical form the
      reducer reads).
    - ``display`` — human string in the dealer's local tz.

    Raises ``ValueError`` if the local-time string can't be parsed or
    the timezone name isn't recognised by ``zoneinfo``.
    """

    naive = _parse_local_naive(starts_at_local)
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone {tz_name!r}") from exc
    aware_local = naive.replace(tzinfo=zone)
    starts_at_utc = aware_local.astimezone(ZoneInfo("UTC"))
    return OfferedSlot(
        id=SlotId(_slot_id_from_local(naive)),
        starts_at=starts_at_utc,
        display=_format_display(naive),
    )


__all__ = [
    "SlotsRepository",
    "build_slots_repository",
    "derive_offered_slot",
]
