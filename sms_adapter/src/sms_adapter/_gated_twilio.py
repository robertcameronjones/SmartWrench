"""Twilio sender wrapper that enforces SMS consent before send."""

from __future__ import annotations

from typing import Protocol, final

from guidepoint.case._models import CaseId

from sms_adapter import TwilioSend


class SmsConsentChecker(Protocol):
    """Return whether outbound SMS is allowed for ``phone``."""

    def sms_consent_for_phone(self, phone: str) -> bool: ...


@final
class SmsConsentError(RuntimeError):
    """Raised when outbound SMS is blocked because the customer opted out."""


def build_gated_twilio_sender(
    *,
    inner: TwilioSend,
    consent: SmsConsentChecker,
) -> TwilioSend:
    """Wrap ``inner`` so sends are rejected when consent is withdrawn."""

    def _send(*, case_id: CaseId, to: str, body: str) -> str:
        if not consent.sms_consent_for_phone(to):
            raise SmsConsentError(f"SMS blocked: customer {to!r} has opted out")
        return inner(case_id=case_id, to=to, body=body)

    return _send


__all__ = [
    "SmsConsentChecker",
    "SmsConsentError",
    "build_gated_twilio_sender",
]
