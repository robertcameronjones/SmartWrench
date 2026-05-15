"""SMS conversation adapter — public surface.

Everything that flows IN, everything that flows OUT, and the one
function that does the business is declared here. Nothing else.

INPUTS  — events the adapter consumes
=====================================
- ``open_conversation(ctx, deps)``  : a Fire-button trigger with channel=sms
- ``handle_inbound(...)``           : a Twilio webhook delivered an SMS
- ``close_conversation(...)``       : the case is over (decline / book / opt-out)

OUTPUTS — the side effects the adapter produces, all behind injected Protocols
=============================================================================
- ``TwilioSend``    : send one SMS to the customer
- ``LlmComplete``   : call the LLM with [system, ...history] -> next reply
- ``HistoryStore``  : persist message history per conversation
- ``RoutingStore``  : map customer phone -> conversation_id (so inbound finds the right thread)

THE BUSINESS FUNCTION
=====================
- ``take_turn(...)`` : the only place a prompt is composed and the LLM is called.
  Both ``open_conversation`` and ``handle_inbound`` route through it.
  Nothing else may call the LLM. Nothing else may compose the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, final

from prompt_composer import PromptPaths

# Re-export so callers don't need to import from prompt_composer separately.
__all__: list[str] = []


# ---------------------------------------------------------------------------
# Value objects (boundary types)
# ---------------------------------------------------------------------------


class TurnRole(StrEnum):
    """Who said this turn. ``system`` is reserved; we never store the system
    prompt in history because it's regenerated per turn from the spot md."""

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


@final
@dataclass(frozen=True, slots=True)
class SmsContext:
    """Just enough to start an SMS conversation.

    Lightweight stand-in for a future ``Case``. The simulator's Fire route
    builds one of these from saved master data and hands it to
    ``open_conversation``. When real Case files exist later, we add a
    ``from_case(case) -> SmsContext`` adapter and the rest of this module
    doesn't change.
    """

    conversation_id: str            # unique routing/history key (any opaque string)
    customer_phone: str             # E.164, e.g. "+13135551212"
    variables: dict[str, str]       # for {{placeholder}} substitution in the prompt


# ---------------------------------------------------------------------------
# OUTPUT contracts (injected; see _twilio.py, _llm.py, _history.py, _routing.py
# for the live implementations)
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


class RoutingStore(Protocol):
    """Phone number -> conversation_id lookup so inbound finds its thread."""

    def bind(self, *, phone: str, conversation_id: str) -> None: ...
    def unbind(self, *, phone: str) -> None: ...
    def find_conversation_id(self, phone: str) -> str | None: ...


# ---------------------------------------------------------------------------
# Dependency bundle
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class SmsDeps:
    """Everything the adapter needs to do its job. Built once at startup."""

    twilio_send: TwilioSend
    llm_complete: LlmComplete
    history: HistoryStore
    routing: RoutingStore
    prompt_paths: PromptPaths
    event_log_path: Path | None = None


# ---------------------------------------------------------------------------
# THE BUSINESS FUNCTION (re-exported from _take_turn)
# ---------------------------------------------------------------------------

from sms_adapter._take_turn import take_turn  # noqa: E402

# ---------------------------------------------------------------------------
# Orchestrators (re-exported from _orchestrators)
# ---------------------------------------------------------------------------

from sms_adapter._orchestrators import (  # noqa: E402
    ContextLookup,
    InboundForUnknownPhoneError,
    close_conversation,
    handle_inbound,
    open_conversation,
)

# ---------------------------------------------------------------------------
# Live factories (re-exported from _twilio, _llm, _history, _routing)
# ---------------------------------------------------------------------------

from sms_adapter._history import build_json_history_store  # noqa: E402
from sms_adapter._llm import build_litellm_completer  # noqa: E402
from sms_adapter._routing import build_json_routing_store  # noqa: E402
from sms_adapter._twilio import build_twilio_sender  # noqa: E402

__all__ = [
    # value objects
    "PromptPaths",
    "SmsContext",
    "SmsDeps",
    "Turn",
    "TurnRole",
    # output protocols
    "HistoryStore",
    "LlmComplete",
    "RoutingStore",
    "TwilioSend",
    # business
    "take_turn",
    # orchestrators
    "ContextLookup",
    "InboundForUnknownPhoneError",
    "close_conversation",
    "handle_inbound",
    "open_conversation",
    # factories
    "build_json_history_store",
    "build_json_routing_store",
    "build_litellm_completer",
    "build_twilio_sender",
]
