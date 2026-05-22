"""SMS conversation adapter — public surface.

The adapter provides one ``CallSession`` implementation (the SMS
counterpart to ElevenLabs's voice ``_LiveCallSession``) plus the
adapter-layer factories that build its dependencies from real
infrastructure (Twilio, LiteLLM, JSON on disk).

Channel session
===============
- ``build_sms_call_session(...)``  — long-running per-case loop.
  Satisfies the ``guidepoint.case.CallSession`` Protocol so
  ``CaseManager`` can dispatch SMS cases through the same code path
  it uses for voice. The session exposes ``deliver_inbound(...)`` so
  the global Twilio webhook handler can enqueue inbound turns onto
  the right per-case queue.

OUTPUTS — adapter-layer Protocols every implementation satisfies
================================================================
- ``TwilioSend``    : send one SMS to the customer
- ``LlmComplete``   : call the LLM with [system, ...history] -> next reply
- ``HistoryStore``  : persist message history per conversation
- ``RoutingStore``  : map customer phone -> ``RoutingEntry``
                     (so inbound finds the right case)

LIVE FACTORIES (used by simulator wiring)
=========================================
- ``build_twilio_sender(...)``
- ``build_litellm_completer(...)``
- ``build_json_history_store(...)``
- ``build_json_routing_store(...)``
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, final

from prompt_composer import PromptPaths

__all__: list[str] = []


# ---------------------------------------------------------------------------
# Value objects (boundary types)
# ---------------------------------------------------------------------------


class TurnRole(StrEnum):
    """Who said this turn.

    ``system`` is reserved; we never store the system prompt in
    history because it's regenerated per turn from the spot md.
    """

    USER = "user"
    ASSISTANT = "assistant"


@final
@dataclass(frozen=True, slots=True)
class Turn:
    """One message in the conversation. Immutable."""

    role: TurnRole
    text: str
    timestamp: datetime
    twilio_sid: str = ""  # outbound: Twilio message SID; inbound: same


# ---------------------------------------------------------------------------
# OUTPUT contracts (injected; live implementations live in
# _twilio.py, _llm.py, _history.py, _routing.py)
# ---------------------------------------------------------------------------


class TwilioSend(Protocol):
    """Send one outbound SMS. Returns the Twilio message SID."""

    def __call__(self, *, to: str, body: str) -> str: ...


class LlmComplete(Protocol):
    """One non-streaming LLM call. Returns the assistant text."""

    def __call__(
        self,
        *,
        system: str,
        history: tuple[Turn, ...],
    ) -> str: ...


class HistoryStore(Protocol):
    """Per-conversation message history (append-only, durable)."""

    def append(self, conversation_id: str, turn: Turn) -> None: ...
    def load(self, conversation_id: str) -> tuple[Turn, ...]: ...


@final
@dataclass(frozen=True, slots=True)
class RoutingEntry:
    """One row of the phone -> conversation routing table.

    ``conversation_id`` is the active case id the inbound webhook
    should hand the turn to. ``user_id`` is the operator who owns
    the conversation — empty string when the binding was written by
    a code path with no operator identity (production monitor task,
    or older simulator code that didn't plumb the auth user
    through). Today the inbound webhook only logs ``user_id``; once
    the case repository becomes per-user, it will route on it too.
    ``channel`` is the channel literal so a future MMS / voice
    routing store can distinguish entries.
    """

    conversation_id: str
    user_id: str = ""
    channel: str = "sms"


class RoutingStore(Protocol):
    """Phone number -> routing entry so inbound finds its thread.

    ``bind`` takes ``conversation_id`` (= ``case_id``) and optional
    ``user_id`` / ``channel`` for audit + future per-user routing.
    ``find_conversation_id`` is the legacy single-string accessor;
    ``find_entry`` returns the full :class:`RoutingEntry` so callers
    can read ``user_id`` and ``channel`` too.
    """

    def bind(
        self,
        *,
        phone: str,
        conversation_id: str,
        user_id: str = "",
        channel: str = "sms",
    ) -> None: ...
    def unbind(self, *, phone: str) -> None: ...
    def find_conversation_id(self, phone: str) -> str | None: ...
    def find_entry(self, phone: str) -> RoutingEntry | None: ...


# ---------------------------------------------------------------------------
# Channel session + factories (re-exported from the impl modules)
# ---------------------------------------------------------------------------

from sms_adapter._call_session import (  # noqa: E402
    DEFAULT_INACTIVITY_TIMEOUT,
    SmsCallSession,
    build_sms_call_session,
)
from sms_adapter._history import build_json_history_store  # noqa: E402
from sms_adapter._llm import build_litellm_completer  # noqa: E402
from sms_adapter._routing import build_json_routing_store  # noqa: E402
from sms_adapter._twilio import build_twilio_sender  # noqa: E402

__all__ = [
    # value objects
    "PromptPaths",
    "RoutingEntry",
    "Turn",
    "TurnRole",
    # output protocols
    "HistoryStore",
    "LlmComplete",
    "RoutingStore",
    "TwilioSend",
    # CallSession implementation (satisfies guidepoint.case.CallSession Protocol)
    "DEFAULT_INACTIVITY_TIMEOUT",
    "SmsCallSession",
    "build_sms_call_session",
    # live factories (simulator wiring uses these)
    "build_json_history_store",
    "build_json_routing_store",
    "build_litellm_completer",
    "build_twilio_sender",
]
