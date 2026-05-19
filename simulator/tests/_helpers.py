"""Shared simulator-test helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, final

from fastapi.testclient import TestClient

from guidepoint.clock import Clock
from simulator import ConnectionStatus

# Master-data + trigger fixtures the integration tests scaffold under
# ``tmp_path`` to exercise the simulator end-to-end without touching the
# checked-in repository fixtures.

SAMPLE_CUSTOMER: dict[str, object] = {
    "id": "cust_test",
    "first_name": "Test",
    "last_name": "Customer",
    "phone": "+13135550000",
    "opt_status": "opted_in",
    "preferred_channel": "voice",
    "timezone": "America/Detroit",
}

SAMPLE_DEALER: dict[str, object] = {
    "id": "dealer_test",
    "name": "Test Dealer",
    "phone": "+12485550000",
    "address": "1 Test St",
    "ride_radius_miles": 10,
}

SAMPLE_VEHICLE: dict[str, object] = {
    "vin": "1C4RJFBG5NC123456",
    "owner_id": "cust_test",
    "year": 2025,
    "make": "Jeep",
    "model": "Grand Cherokee",
    "odometer_miles": 12000,
    "current_location": {
        "latitude": 42.4895,
        "longitude": -83.1446,
        "description": "test location",
    },
}

SAMPLE_SLOTS: list[dict[str, object]] = [
    {
        "id": "slot_a",
        "starts_at": "2026-05-12T12:30:00Z",
        "display": "Tuesday, May 12 - 8:30 AM",
    },
    {
        "id": "slot_b",
        "starts_at": "2026-05-13T13:00:00Z",
        "display": "Wednesday, May 13 - 9:00 AM",
    },
]


def seed_master_data(project_root: Path) -> None:
    """Write customer / dealer / vehicle / slots JSON fixtures under ``project_root``."""
    _write(project_root / "fixtures" / "customers" / "cust_test.json", SAMPLE_CUSTOMER)
    _write(project_root / "fixtures" / "dealers" / "dealer_test.json", SAMPLE_DEALER)
    _write(project_root / "fixtures" / "vehicles" / "1C4RJFBG5NC123456.json", SAMPLE_VEHICLE)
    seed_slots(project_root)


def seed_slots(project_root: Path, slots: list[dict[str, object]] | None = None) -> Path:
    """Write the slot list JSON fixture; return its path."""
    payload = slots if slots is not None else SAMPLE_SLOTS
    target = project_root / "fixtures" / "slots.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@final
class FixedClock:
    """Deterministic clock for tests."""

    def __init__(self, *, start: datetime | None = None) -> None:
        self._instant = start or datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._instant


@final
@dataclass(frozen=True, slots=True)
class StubProbe:
    """Connection probe that returns a canned ``ConnectionStatus``."""

    status: ConnectionStatus

    def check(self) -> ConnectionStatus:
        return self.status


def healthy_status(*, clock: Clock) -> ConnectionStatus:
    """A canned 'all green' status for tests."""
    return ConnectionStatus(
        api_key_present=True,
        agent_id_present=True,
        agent_id="agent_test",
        last_checked=clock.now(),
        healthy=True,
        detail="ok",
    )


@final
class UserClient:
    """``TestClient`` wrapper that auto-injects HTTP Basic Auth headers.

    Per-user Basic Auth gates every API call in v2: the username IS
    the operator id. Tests configure the simulator with
    ``UserRegistry("demo:demo")`` (the default) and this wrapper
    sends ``Authorization: Basic ...`` on each request.

    ``.raw`` exposes the underlying ``TestClient`` for tests that
    need to exercise auth failure modes or hit exempt routes
    (``/health``, ``/sms``, ``/twilio/*``).
    """

    def __init__(self, client: TestClient, *, user: str, password: str) -> None:
        self.raw = client
        self.auth = (user, password)

    def get(self, url: str, **kw: Any) -> Any:
        return self.raw.get(url, auth=kw.pop("auth", self.auth), **kw)

    def post(self, url: str, **kw: Any) -> Any:
        return self.raw.post(url, auth=kw.pop("auth", self.auth), **kw)

    def put(self, url: str, **kw: Any) -> Any:
        return self.raw.put(url, auth=kw.pop("auth", self.auth), **kw)

    def delete(self, url: str, **kw: Any) -> Any:
        return self.raw.delete(url, auth=kw.pop("auth", self.auth), **kw)


__all__ = [
    "SAMPLE_CUSTOMER",
    "SAMPLE_DEALER",
    "SAMPLE_SLOTS",
    "SAMPLE_VEHICLE",
    "FixedClock",
    "StubProbe",
    "UserClient",
    "healthy_status",
    "seed_master_data",
    "seed_slots",
]
