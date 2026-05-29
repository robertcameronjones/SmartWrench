"""Tests for the simulator slot helpers.

The on-disk shape (full ``OfferedSlot``) is unchanged; what's new is
:func:`derive_offered_slot`, which lets the UI send a single
local-naive datetime per row and have the server fill in everything
else. These tests pin that behaviour so future changes don't quietly
break the editing flow.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from simulator._slots import derive_offered_slot


class TestDeriveOfferedSlot:
    def test_summer_edt_offset_to_utc(self) -> None:
        """8:30 AM in Detroit during EDT (June) → 12:30Z."""
        slot = derive_offered_slot(
            starts_at_local="2026-06-09T08:30", tz_name="America/Detroit"
        )
        assert slot.starts_at == datetime(2026, 6, 9, 12, 30, tzinfo=UTC)

    def test_winter_est_offset_to_utc(self) -> None:
        """8:30 AM in Detroit during EST (January) → 13:30Z."""
        slot = derive_offered_slot(
            starts_at_local="2026-01-15T08:30", tz_name="America/Detroit"
        )
        assert slot.starts_at == datetime(2026, 1, 15, 13, 30, tzinfo=UTC)

    def test_slot_id_is_local_yyyy_mm_dd_hhmm(self) -> None:
        slot = derive_offered_slot(
            starts_at_local="2026-06-09T08:30", tz_name="America/Detroit"
        )
        assert slot.id == "slot_2026_06_09_0830"

    def test_display_string_human_format(self) -> None:
        slot = derive_offered_slot(
            starts_at_local="2026-06-09T08:30", tz_name="America/Detroit"
        )
        assert slot.display == "Tuesday, June 9, 2026 - 8:30 AM"

    def test_display_pm_hours(self) -> None:
        slot = derive_offered_slot(
            starts_at_local="2026-06-09T13:30", tz_name="America/Detroit"
        )
        assert slot.display == "Tuesday, June 9, 2026 - 1:30 PM"

    def test_display_noon_and_midnight(self) -> None:
        noon = derive_offered_slot(
            starts_at_local="2026-06-09T12:00", tz_name="America/Detroit"
        )
        midnight = derive_offered_slot(
            starts_at_local="2026-06-09T00:00", tz_name="America/Detroit"
        )
        assert noon.display.endswith("12:00 PM")
        assert midnight.display.endswith("12:00 AM")

    def test_accepts_seconds_in_input(self) -> None:
        # datetime-local pickers sometimes emit seconds; tolerate them.
        slot = derive_offered_slot(
            starts_at_local="2026-06-09T08:30:00", tz_name="America/Detroit"
        )
        assert slot.id == "slot_2026_06_09_0830"

    def test_other_timezone_resolves_correctly(self) -> None:
        # 8:30 AM Pacific in June (PDT, UTC-7) → 15:30Z.
        slot = derive_offered_slot(
            starts_at_local="2026-06-09T08:30", tz_name="America/Los_Angeles"
        )
        assert slot.starts_at == datetime(2026, 6, 9, 15, 30, tzinfo=UTC)

    def test_malformed_local_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="starts_at_local"):
            derive_offered_slot(
                starts_at_local="not a datetime", tz_name="America/Detroit"
            )

    def test_unknown_timezone_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            derive_offered_slot(
                starts_at_local="2026-06-09T08:30", tz_name="Mars/Olympus_Mons"
            )
