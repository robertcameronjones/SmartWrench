"""Env-var connection probe."""

from __future__ import annotations

import pytest

from simulator import build_env_connection_probe
from tests.simulator._helpers import FixedClock


@pytest.fixture(autouse=True)
def clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_AGENT_ID", raising=False)


def test_no_env_is_unhealthy() -> None:
    status = build_env_connection_probe(clock=FixedClock()).check()
    assert status.healthy is False
    assert status.api_key_present is False
    assert "API_KEY" in status.detail


def test_only_api_key_present_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test")
    status = build_env_connection_probe(clock=FixedClock()).check()
    assert status.api_key_present is True
    assert status.agent_id_present is False
    assert status.healthy is False
    assert "AGENT_ID" in status.detail


def test_both_present_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test")
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent_test")
    status = build_env_connection_probe(clock=FixedClock()).check()
    assert status.healthy is True
    assert status.agent_id == "agent_test"
