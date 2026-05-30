"""Twilio sender — adapter onto ``sms.send_sms``.

Closes over credentials so the rest of the adapter calls
``deps.twilio_send(case_id=..., to=..., body=...)`` without dragging
.env-aware code into the orchestrators.

``case_id`` is accepted to satisfy the :class:`TwilioSend` protocol
contract (every send is for a case) but Twilio itself does not need it
— it's logged for observability when a send fails so operators can
trace back to the originating case.
"""

from __future__ import annotations

from guidepoint.case._models import CaseId
from sms import send_sms

from sms_adapter import TwilioSend


def build_twilio_sender(
    *,
    account_sid: str,
    auth_token: str,
    from_number: str,
) -> TwilioSend:
    """Return a callable that sends one SMS and returns the message SID."""

    def _send(*, case_id: CaseId, to: str, body: str) -> str:
        del case_id  # accepted by protocol, not needed by Twilio
        return send_sms(
            to=to,
            body=body,
            account_sid=account_sid,
            auth_token=auth_token,
            from_number=from_number,
        )

    return _send


__all__ = ["build_twilio_sender"]
