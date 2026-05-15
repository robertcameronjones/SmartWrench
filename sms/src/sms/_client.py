"""Thin Twilio outbound wrapper.

One function: send an SMS. No magic, no env-loading, no logging — caller
passes everything explicitly. The CLI in ``sms.cli`` and the conversation
adapter in ``sms_adapter`` both call this.
"""

from __future__ import annotations

from twilio.rest import Client


def send_sms(
    *,
    to: str,
    body: str,
    account_sid: str,
    auth_token: str,
    from_number: str,
) -> str:
    """Send one SMS via Twilio. Returns the message SID.

    Raises ``twilio.base.exceptions.TwilioRestException`` on API failure;
    callers decide whether to retry.
    """
    if not to:
        raise ValueError("to is required")
    if not body:
        raise ValueError("body is required (Twilio rejects empty messages)")
    if not account_sid or not auth_token:
        raise ValueError("account_sid and auth_token are required")
    if not from_number:
        raise ValueError("from_number is required")

    client = Client(account_sid, auth_token)
    msg = client.messages.create(to=to, from_=from_number, body=body)
    return msg.sid


__all__ = ["send_sms"]
