"""Route tests for simulator world controls and case signals."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from guidepoint.case import CaseEvent
from guidepoint.events import build_event_bus
from simulator import build_app
from tests._helpers import FixedClock, StubProbe, UserClient, healthy_status, seed_master_data
from tests.test_routes import FakeBookedCallSession, TEST_PASSWORD, TEST_USER, _wait_for_state


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
def client(tmp_path: Path) -> UserClient:
    return UserClient(
        TestClient(_build_test_app(tmp_path)),
        user=TEST_USER,
        password=TEST_PASSWORD,
    )


class TestWorldControls:
    def test_world_state_defaults_open(self, client: UserClient) -> None:
        res = client.get("/api/world/state")
        assert res.status_code == 200
        body = res.json()
        assert body["business_hours_open"] is True
        assert body["at_dealer"] is False

    def test_business_hours_toggle(self, client: UserClient) -> None:
        closed = client.put("/api/world/business-hours", json={"open": False})
        assert closed.status_code == 200
        assert closed.json()["business_hours_open"] is False
        opened = client.put("/api/world/business-hours", json={"open": True})
        assert opened.json()["business_hours_open"] is True

    def test_geofence_toggle(self, client: UserClient) -> None:
        snap = client.get("/api/master-data").json()
        vin = snap["vehicle"]["vin"]
        at = client.put(
            "/api/world/geofence",
            json={"vehicle_vin": vin, "at_dealer": True},
        )
        assert at.status_code == 200
        assert at.json()["at_dealer"] is True


class TestCaseSignalRoute:
    def test_reminder_signal_returns_accepted(self, client: UserClient) -> None:
        fire = client.post(
            "/api/fire",
            json={"service_type": "maintenance", "service_summary": "oil change"},
        )
        assert fire.status_code == 200
        case_id = fire.json()["case_id"]
        _wait_for_state(client, case_id, {"initial_reminder_due"})

        sig = client.post(
            f"/api/cases/{case_id}/signal",
            json={"signal_type": "initial_reminder_due"},
        )
        assert sig.status_code == 200
        body = sig.json()
        assert body["status"] == "accepted"
        assert body["signal_type"] == "initial_reminder_due"

    def test_unknown_case_404s(self, client: UserClient) -> None:
        res = client.post(
            "/api/cases/case_nope/signal",
            json={"signal_type": "final_reminder_due"},
        )
        assert res.status_code == 404


class TestQueueHealth:
    def test_health_queues_returns_driver_depths(self, client: UserClient) -> None:
        res = client.get("/health/queues")
        assert res.status_code == 200
        body = res.json()
        assert "case_driver_queues" in body
        assert "case_driver_active_cases" in body
