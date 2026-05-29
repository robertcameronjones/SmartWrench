"""Tests for gated Twilio sender."""

from __future__ import annotations

from typing import final

import pytest

from sms_adapter import SmsConsentError, build_gated_twilio_sender


@final
class _FakeConsent:
    def __init__(self, *, allowed: bool) -> None:
        self._allowed = allowed
        self.phones: list[str] = []

    def sms_consent_for_phone(self, phone: str) -> bool:
        self.phones.append(phone)
        return self._allowed


def test_gated_sender_passes_when_consent_true() -> None:
    sent: list[tuple[str, str]] = []

    def _inner(*, to: str, body: str) -> str:
        sent.append((to, body))
        return "SM123"

    sender = build_gated_twilio_sender(inner=_inner, consent=_FakeConsent(allowed=True))
    sid = sender(to="+13135550000", body="hello")
    assert sid == "SM123"
    assert sent == [("+13135550000", "hello")]


def test_gated_sender_blocks_when_consent_false() -> None:
    sender = build_gated_twilio_sender(
        inner=lambda *, to, body: "SM123",
        consent=_FakeConsent(allowed=False),
    )
    with pytest.raises(SmsConsentError, match="opted out"):
        sender(to="+13135550000", body="hello")
