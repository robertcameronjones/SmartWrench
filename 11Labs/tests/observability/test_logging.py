"""Tests for the observability module."""

from __future__ import annotations

import logging

import pytest
import structlog

from guidepoint.observability import bind_context, clear_context, configure_logging


@pytest.fixture(autouse=True)
def _reset_context() -> None:  # pyright: ignore[reportUnusedFunction]
    """Ensure each test starts with a clean context-var stack."""
    clear_context()


class TestConfigureLogging:
    def test_runs_without_error(self) -> None:
        configure_logging(level=logging.WARNING)
        configure_logging(level=logging.INFO)  # idempotent

    def test_json_mode_emits_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(json=True)
        log = structlog.get_logger("test")
        log.info("hello", agent_id="agent_x", attempt=1)
        captured = capsys.readouterr()
        line = captured.err.strip().splitlines()[-1]
        assert line.startswith("{")
        assert '"event": "hello"' in line
        assert '"agent_id": "agent_x"' in line
        assert '"attempt": 1' in line


class TestBindContext:
    def test_bound_keys_appear_in_captured_events(self) -> None:
        configure_logging()
        bind_context(correlation_id="abc-123")
        with structlog.testing.capture_logs(
            processors=[structlog.contextvars.merge_contextvars],
        ) as events:
            structlog.get_logger("t").info("did_a_thing", widget="x")
        assert events[0]["correlation_id"] == "abc-123"
        assert events[0]["widget"] == "x"

    def test_clear_context_drops_bound_keys(self) -> None:
        configure_logging()
        bind_context(correlation_id="abc-123")
        clear_context()
        with structlog.testing.capture_logs(
            processors=[structlog.contextvars.merge_contextvars],
        ) as events:
            structlog.get_logger("t").info("after_clear")
        assert "correlation_id" not in events[0]
