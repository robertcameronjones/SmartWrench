"""Tests for SMS opt-out keyword detection."""

from __future__ import annotations

from sms_adapter import is_opt_in_keyword, is_opt_out_keyword, normalize_sms_body


def test_normalize_sms_body() -> None:
    assert normalize_sms_body("  stop  ") == "STOP"


def test_stop_keywords() -> None:
    assert is_opt_out_keyword("STOP")
    assert is_opt_out_keyword("UNSUBSCRIBE")
    assert not is_opt_out_keyword("CANCEL")


def test_start_keywords() -> None:
    assert is_opt_in_keyword("START")
    assert is_opt_in_keyword("UNSTOP")
    assert not is_opt_in_keyword("STOP")
