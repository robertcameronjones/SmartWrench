"""HTTP and WebSocket route smoke tests for the operator console.

The simulator is a thin UI over master data + slots + a ``CaseManager``.
Triggers are synthesized at fire time from the saved master data plus the
form payload — there is no trigger picker and no trigger fixture file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from guidepoint.case import CaseEvent
from guidepoint.events import build_event_bus
from simulator import build_app
from tests.case._helpers import FakeBookedCallSession
from tests.simulator._helpers import (
    FixedClock,
    StubProbe,
    healthy_status,
    seed_master_data,
)


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


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(_build_test_app(tmp_path))


class TestIndex:
    def test_renders_html_shell(self, client: TestClient) -> None:
        res = client.get("/")
        assert res.status_code == 200
        assert "Guidepoint" in res.text
        assert 'id="trigger-form"' in res.text
        assert "trigger-picker" not in res.text


class TestMasterDataSnapshot:
    def test_returns_first_of_each_entity_plus_slots(self, client: TestClient) -> None:
        body = client.get("/api/master-data").json()
        assert body["customer"]["id"] == "cust_test"
        assert body["dealer"]["id"] == "dealer_test"
        assert body["vehicle"]["vin"] == "1C4RJFBG5NC123456"
        assert {s["id"] for s in body["slots"]} == {"slot_a", "slot_b"}


class TestMasterDataApi:
    def test_get_customer(self, client: TestClient) -> None:
        body = client.get("/api/customers/cust_test").json()
        assert body["first_name"] == "Test"

    def test_get_dealer(self, client: TestClient) -> None:
        body = client.get("/api/dealers/dealer_test").json()
        assert body["ride_radius_miles"] == 10

    def test_get_vehicle(self, client: TestClient) -> None:
        body = client.get("/api/vehicles/1C4RJFBG5NC123456").json()
        assert body["make"] == "Jeep"

    def test_put_customer_updates_disk(self, client: TestClient, tmp_path: Path) -> None:
        body = client.get("/api/customers/cust_test").json()
        body["first_name"] = "Renamed"
        res = client.put("/api/customers/cust_test", json=body)
        assert res.status_code == 200
        on_disk = json.loads(
            (tmp_path / "fixtures" / "customers" / "cust_test.json").read_text("utf-8")
        )
        assert on_disk["first_name"] == "Renamed"

    def test_put_id_mismatch_400s(self, client: TestClient) -> None:
        body = client.get("/api/customers/cust_test").json()
        res = client.put("/api/customers/other_id", json=body)
        assert res.status_code == 400


class TestSlotsApi:
    def test_get_returns_seeded_slots(self, client: TestClient) -> None:
        body = client.get("/api/slots").json()
        assert {s["id"] for s in body} == {"slot_a", "slot_b"}

    def test_put_overwrites_disk(self, client: TestClient, tmp_path: Path) -> None:
        new_slots = [
            {
                "id": "slot_x",
                "starts_at": "2026-05-14T15:00:00Z",
                "display": "Thursday, May 14 - 11:00 AM",
            }
        ]
        res = client.put("/api/slots", json=new_slots)
        assert res.status_code == 200
        on_disk = json.loads((tmp_path / "fixtures" / "slots.json").read_text("utf-8"))
        assert [s["id"] for s in on_disk] == ["slot_x"]


class TestConnection:
    def test_returns_status(self, client: TestClient) -> None:
        body = client.get("/api/connection").json()
        assert body["healthy"] is True


class TestFireEndpoint:
    def test_synthesizes_trigger_and_returns_case_ids(self, client: TestClient) -> None:
        res = client.post(
            "/api/fire",
            json={"service_type": "maintenance", "service_summary": "30k mile service"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["case_id"]
        assert body["correlation_id"]

    def test_fire_uses_saved_slots(self, client: TestClient) -> None:
        client.post(
            "/api/fire",
            json={"service_type": "maintenance", "service_summary": "30k mile service"},
        )
        cases = client.get("/api/cases").json()
        case_id = cases[0]["case_id"]
        full = client.get(f"/api/cases/{case_id}").json()
        assert {s["id"] for s in full["offered_slots"]} == {"slot_a", "slot_b"}

    def test_invalid_payload_422s(self, client: TestClient) -> None:
        # Missing service_summary.
        res = client.post("/api/fire", json={"service_type": "maintenance"})
        assert res.status_code == 422

    def test_unknown_service_type_422s(self, client: TestClient) -> None:
        res = client.post(
            "/api/fire",
            json={"service_type": "weird", "service_summary": "x"},
        )
        assert res.status_code == 422


class TestRecentCasesApi:
    def test_lists_after_fire(self, client: TestClient) -> None:
        client.post(
            "/api/fire",
            json={"service_type": "maintenance", "service_summary": "oil change"},
        )
        cases = client.get("/api/cases").json()
        assert len(cases) == 1
        assert cases[0]["state"] in {"booked", "unreachable", "declined", "escalated"}

    def test_get_missing_case_404s(self, client: TestClient) -> None:
        assert client.get("/api/cases/nope").status_code == 404


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
        client = TestClient(app)
        with client.websocket_connect("/ws/log") as ws:
            res = client.post(
                "/api/fire",
                json={"service_type": "maintenance", "service_summary": "30k mile service"},
            )
            assert res.status_code == 200
            first = json.loads(ws.receive_text())
            assert "case_id" in first
            assert "event" in first
            assert first["correlation_id"] == res.json()["correlation_id"]
