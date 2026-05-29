"""HTTP and WebSocket route smoke tests for the operator console.

The simulator is a thin UI over master data + slots + a ``CaseManager``.
Triggers are synthesized at fire time from the saved master data plus the
form payload — there is no trigger picker and no trigger fixture file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from guidepoint.case import Case, CallOutcome, CaseEvent, SlotId
from guidepoint.events import build_event_bus
from simulator import build_app
from tests._helpers import (
    FixedClock,
    StubProbe,
    UserClient,
    healthy_status,
    seed_master_data,
)


# In-tree fake voice CallSession so the route tests don't need to
# reach into the 11Labs test helpers package. Deterministic ``booked``
# outcome with the canonical ``slot_a`` so manager assertions are stable.
class FakeBookedCallSession:
    async def place(self, case: Case) -> CallOutcome:
        from datetime import UTC, datetime

        booked = case.offered_slots[0].id if case.offered_slots else SlotId("slot_a")
        now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
        return CallOutcome(
            result="answered",
            business_outcome="booked",
            booked_slot_id=booked,
            elevenlabs_conversation_id="fake_conv_test",
            started_at=now,
            ended_at=now,
            duration_seconds=0.0,
            transcript="(fake test outcome)",
        )

    async def start(self, *, case: Case, stage, attempt_number: int) -> CallOutcome:
        return await self.place(case)

# All API routes are gated by per-user HTTP Basic Auth (v2 phase 1).
# Tests use the default "demo:demo" user; UserClient sends the auth
# header on each request.
TEST_USER = "demo"
TEST_PASSWORD = "demo"


def _build_test_app(tmp_path: Path) -> FastAPI:
    seed_master_data(tmp_path)
    clock = FixedClock()
    return build_app(
        project_root=tmp_path,
        clock=clock,
        bus=build_event_bus(payload_type=CaseEvent),
        probe=StubProbe(status=healthy_status(clock=clock)),
        call_session=FakeBookedCallSession(),
    )


def _wait_for_state(
    client: UserClient,
    case_id: str,
    states: set[str],
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    """Poll ``GET /api/cases/{id}`` until ``state`` is in ``states`` or timeout.

    ``CaseManager.start`` runs the call attempt in a background task,
    so tests that want to assert on the terminal Case have to wait
    for the task to finish. With the ``FakeBookedCallSession`` this
    is sub-millisecond in practice; the timeout is generous in case
    the event loop is slow.
    """
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        res = client.get(f"/api/cases/{case_id}")
        if res.status_code == 200:
            last = res.json()
            if last.get("state") in states:
                return last
        time.sleep(0.01)
    raise AssertionError(
        f"case {case_id!r} did not reach a state in {states} within "
        f"{timeout_seconds}s (last: {last.get('state')!r})"
    )


@pytest.fixture
def client(tmp_path: Path) -> UserClient:
    return UserClient(
        TestClient(_build_test_app(tmp_path)),
        user=TEST_USER,
        password=TEST_PASSWORD,
    )


class TestIndex:
    def test_renders_html_shell_with_user_in_topbar(self, client: UserClient) -> None:
        res = client.get("/")
        assert res.status_code == 200
        assert "Guidepoint" in res.text
        assert 'id="trigger-form"' in res.text
        assert 'id="case-controls-panel"' in res.text
        assert "trigger-picker" not in res.text
        assert TEST_USER in res.text  # topbar pill renders the user id

    def test_index_requires_auth(self, client: UserClient) -> None:
        assert client.raw.get("/").status_code == 401


class TestMasterDataSnapshot:
    def test_returns_first_of_each_entity_plus_slots(self, client: UserClient) -> None:
        body = client.get("/api/master-data").json()
        assert body["customer"]["id"] == "cust_test"
        assert body["dealer"]["id"] == "dealer_test"
        assert body["vehicle"]["vin"] == "1C4RJFBG5NC123456"
        assert {s["id"] for s in body["slots"]} == {"slot_a", "slot_b"}


class TestMasterDataApi:
    def test_get_customer(self, client: UserClient) -> None:
        body = client.get("/api/customers/cust_test").json()
        assert body["first_name"] == "Test"

    def test_get_dealer(self, client: UserClient) -> None:
        body = client.get("/api/dealers/dealer_test").json()
        assert body["ride_radius_miles"] == 10

    def test_get_vehicle(self, client: UserClient) -> None:
        body = client.get("/api/vehicles/1C4RJFBG5NC123456").json()
        assert body["make"] == "Jeep"

    def test_put_customer_updates_disk(self, client: UserClient, tmp_path: Path) -> None:
        body = client.get("/api/customers/cust_test").json()
        body["first_name"] = "Renamed"
        res = client.put("/api/customers/cust_test", json=body)
        assert res.status_code == 200
        # Per-user namespace: PUT writes to the user's data dir, not the
        # shared fixtures/ tree (which stays unchanged as the seed source).
        on_disk = json.loads(
            (
                tmp_path / "data" / "users" / TEST_USER / "customers" / "cust_test.json"
            ).read_text("utf-8")
        )
        assert on_disk["first_name"] == "Renamed"

    def test_put_id_mismatch_400s(self, client: UserClient) -> None:
        body = client.get("/api/customers/cust_test").json()
        res = client.put("/api/customers/other_id", json=body)
        assert res.status_code == 400


class TestSlotsApi:
    def test_get_returns_seeded_slots(self, client: UserClient) -> None:
        body = client.get("/api/slots").json()
        assert {s["id"] for s in body} == {"slot_a", "slot_b"}

    def test_put_overwrites_disk(self, client: UserClient, tmp_path: Path) -> None:
        # New shape: operator only sends local-naive datetimes. The
        # server derives id (slot_YYYY_MM_DD_HHMM), UTC starts_at, and
        # the display string using the dealer's timezone.
        new_slots = [{"starts_at_local": "2026-06-09T08:30"}]
        res = client.put("/api/slots", json=new_slots)
        assert res.status_code == 200, res.text
        on_disk = json.loads(
            (tmp_path / "data" / "users" / TEST_USER / "slots.json").read_text("utf-8")
        )
        assert [s["id"] for s in on_disk] == ["slot_2026_06_09_0830"]
        # Display is server-formatted in dealer-local time.
        assert on_disk[0]["display"] == "Tuesday, June 9, 2026 - 8:30 AM"
        # starts_at is UTC (EDT in June is -04:00 → 12:30Z).
        assert on_disk[0]["starts_at"].startswith("2026-06-09T12:30:00")

    def test_put_rejects_malformed_local_time(self, client: UserClient) -> None:
        res = client.put(
            "/api/slots", json=[{"starts_at_local": "yesterday"}]
        )
        assert res.status_code == 400
        assert "starts_at_local" in res.text


class TestConnection:
    def test_returns_status(self, client: UserClient) -> None:
        body = client.get("/api/connection").json()
        assert body["healthy"] is True


class TestFireEndpoint:
    def test_synthesizes_trigger_and_returns_case_ids(self, client: UserClient) -> None:
        res = client.post(
            "/api/fire",
            json={"service_type": "maintenance", "service_summary": "30k mile service"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["case_id"]
        assert body["correlation_id"]

    def test_fire_uses_saved_slots(self, client: UserClient) -> None:
        client.post(
            "/api/fire",
            json={"service_type": "maintenance", "service_summary": "30k mile service"},
        )
        cases = client.get("/api/cases").json()
        case_id = cases[0]["case_id"]
        full = client.get(f"/api/cases/{case_id}").json()
        assert {s["id"] for s in full["offered_slots"]} == {"slot_a", "slot_b"}

    def test_invalid_payload_422s(self, client: UserClient) -> None:
        res = client.post("/api/fire", json={"service_type": "maintenance"})
        assert res.status_code == 422

    def test_unknown_service_type_422s(self, client: UserClient) -> None:
        res = client.post(
            "/api/fire",
            json={"service_type": "weird", "service_summary": "x"},
        )
        assert res.status_code == 422


class TestRecentCasesApi:
    def test_lists_after_fire(self, client: UserClient) -> None:
        # ``/api/fire`` uses ``CaseDriver.fire`` which runs the v2
        # lifecycle asynchronously. Poll until the fake call session
        # finishes outreach + dealer confirm.
        res = client.post(
            "/api/fire",
            json={"service_type": "maintenance", "service_summary": "oil change"},
        )
        assert res.status_code == 200
        case_id = res.json()["case_id"]
        target_states = {"initial_reminder_due", "booked", "declined", "opted_out"}
        case = _wait_for_state(client, case_id, target_states)
        cases = client.get("/api/cases").json()
        assert len(cases) == 1
        assert case["state"] in target_states

    def test_get_missing_case_404s(self, client: UserClient) -> None:
        assert client.get("/api/cases/nope").status_code == 404


class TestAuth:
    def test_no_credentials_401s(self, client: UserClient) -> None:
        assert client.raw.get("/api/customers/cust_test").status_code == 401

    def test_wrong_password_401s(self, client: UserClient) -> None:
        res = client.raw.get(
            "/api/customers/cust_test", auth=(TEST_USER, "wrong")
        )
        assert res.status_code == 401

    def test_unknown_user_401s(self, client: UserClient) -> None:
        res = client.raw.get(
            "/api/customers/cust_test", auth=("nobody", "anypw")
        )
        assert res.status_code == 401

    def test_health_exempt_from_auth(self, client: UserClient) -> None:
        assert client.raw.get("/health").status_code == 200


class TestWebSocket:
    def test_streams_case_event_after_fire(self, tmp_path: Path) -> None:
        bus = build_event_bus(payload_type=CaseEvent)
        clock = FixedClock()
        seed_master_data(tmp_path)
        app = build_app(
            project_root=tmp_path,
            clock=clock,
            bus=bus,
            probe=StubProbe(status=healthy_status(clock=clock)),
            call_session=FakeBookedCallSession(),
        )
        raw = TestClient(app)
        client = UserClient(raw, user=TEST_USER, password=TEST_PASSWORD)
        with raw.websocket_connect("/ws/log") as ws:
            res = client.post(
                "/api/fire",
                json={"service_type": "maintenance", "service_summary": "30k mile service"},
            )
            assert res.status_code == 200
            first = json.loads(ws.receive_text())
            assert "case_id" in first
            assert "event" in first
            assert first["correlation_id"] == res.json()["correlation_id"]
