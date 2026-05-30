"""Outbound dispatch — worker + gate ports.

The outbound subsystem sits between the queue (persistence) and the
channel adapters (Twilio for SMS, ElevenLabs for voice in a later
phase). It owns:

- The **worker** that drains :class:`OutboundQueue` for ready items and
  dispatches them to the right channel. The worker is edge-triggered
  (``asyncio.Event``) — it sleeps until woken by the sender (on
  enqueue) or the business-hours service (on open). No polling.
- The **gate ports** the worker consults before each send: SMS consent
  and business hours.
- The **dispatcher callback** the worker invokes to actually send an
  item via a concrete channel.

Nothing in the case state machine, reducer, or driver imports anything
from this package. Conversely, this package imports nothing from
``sms_adapter`` — it depends only on small protocols that adapters
implement. That keeps the layering one-way: state machine → driver →
queue → worker → channel adapter. Result reporting flows the other
way as a typed ``OutboundDispatched`` signal pushed back into the
driver's existing signal queue.
"""

from __future__ import annotations

from guidepoint.outbound._ports import (
    BusinessHoursPort,
    OutboundDispatcher,
    SmsConsentPort,
)
from guidepoint.outbound._worker import (
    OnDispatchedCallback,
    OutboundWorker,
    OutboundWorkerConfig,
    PermanentDispatchError,
    TransientDispatchError,
)

__all__ = [
    "BusinessHoursPort",
    "OnDispatchedCallback",
    "OutboundDispatcher",
    "OutboundWorker",
    "OutboundWorkerConfig",
    "PermanentDispatchError",
    "SmsConsentPort",
    "TransientDispatchError",
]
