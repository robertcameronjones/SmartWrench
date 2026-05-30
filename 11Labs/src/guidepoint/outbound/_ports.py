"""Ports the outbound worker depends on.

Each port is a minimal Protocol — the smallest surface the worker needs
from its environment. Adapters (simulator, sms_adapter) implement them.

Keeping the ports here rather than in the adapter packages means the
worker can be unit-tested without importing anything from those
packages: tests pass in trivial fakes that satisfy the Protocol.
"""

from __future__ import annotations

from typing import Protocol


class BusinessHoursPort(Protocol):
    """The "are we allowed to send right now?" boolean.

    The worker calls :meth:`hours_open` before every dispatch. ``True``
    means "send"; ``False`` means "leave the item on the queue and try
    again on the next tick." There is no scheduling baked in — the
    worker simply re-polls.

    In v1 the simulator backs this with a single global boolean tied to
    the UI slider. In production this would consult the dealer's wall
    clock + holiday calendar. The worker doesn't care which.
    """

    def hours_open(self) -> bool:
        """Return ``True`` if outbound messages may be dispatched now."""
        ...


class SmsConsentPort(Protocol):
    """Per-phone SMS consent check.

    The worker calls :meth:`sms_consent_for_phone` right before
    dispatch. ``False`` is **permanent** — the worker marks the item
    :attr:`OutboundState.BLOCKED` and moves on. Re-opt-in for the same
    phone in the future will cause future sends to succeed; it does not
    revive items that were already blocked.
    """

    def sms_consent_for_phone(self, phone: str) -> bool:
        """Return ``True`` if SMS to ``phone`` is currently allowed."""
        ...


class OutboundDispatcher(Protocol):
    """Channel-specific send callback the worker invokes per item.

    Implementations call the underlying API (Twilio, ElevenLabs, ...)
    and return the channel-assigned identifier (Twilio's MessageSid for
    SMS). Raise :class:`PermanentDispatchError` for non-retryable
    failures (bad number format, billing dead, etc.) and
    :class:`TransientDispatchError` for things worth retrying (HTTP 5xx,
    timeouts, rate limits). Any other exception is treated as transient
    and retried.

    Phase 1 only registers a dispatcher for ``kind="sms_text"``. Voice
    plugs in as a second dispatcher keyed on a different kind later.
    """

    def __call__(self, *, to: str, body: str) -> str:
        """Send and return the channel-side identifier."""
        ...


__all__ = [
    "BusinessHoursPort",
    "OutboundDispatcher",
    "SmsConsentPort",
]
