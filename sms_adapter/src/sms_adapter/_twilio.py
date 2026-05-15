"""Twilio sender — adapter onto ``sms.send_sms``.

Closes over credentials so the rest of the adapter calls
``deps.twilio_send(to=..., body=...)`` without dragging .env-aware code
into the orchestrators.
"""

from __future__ import annotations

from sms import send_sms

from sms_adapter import TwilioSend


def build_twilio_sender(
    *,
    account_sid: str,
    auth_token: str,
    from_number: str,
) -> TwilioSend:
    """Return a callable that sends one SMS and returns the message SID."""

    def _send(*, to: str, body: str) -> str:
        return send_sms(
            to=to,
            body=body,
            account_sid=account_sid,
            auth_token=auth_token,
            from_number=from_number,
        )

    return _send


__all__ = ["build_twilio_sender"]
