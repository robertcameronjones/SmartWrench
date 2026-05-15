"""Boundary model validation for the simulator HTTP/WS surface."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from simulator import (
    ConnectionStatus,
    FireRequest,
)


class TestFireRequest:
    def test_round_trip(self) -> None:
        req = FireRequest(service_type="maintenance", service_summary="oil change")
        assert req.service_type == "maintenance"
        assert req.service_summary == "oil change"
        assert req.narrative == ""

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            FireRequest.model_validate(
                {"service_type": "maintenance", "service_summary": "x", "rogue": True}
            )

    def test_rejects_empty_summary(self) -> None:
        with pytest.raises(ValidationError):
            FireRequest(service_type="maintenance", service_summary="")

    def test_rejects_unknown_service_type(self) -> None:
        with pytest.raises(ValidationError):
            FireRequest.model_validate({"service_type": "weird", "service_summary": "x"})


def test_connection_status_defaults() -> None:
    status = ConnectionStatus(
        api_key_present=True,
        agent_id_present=False,
        last_checked=datetime(2026, 5, 10, tzinfo=UTC),
        healthy=False,
        detail="agent id missing",
    )
    assert status.agent_id == ""
