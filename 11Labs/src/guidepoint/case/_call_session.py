"""ElevenLabs adapter — initiate one outbound call, poll for completion, return outcome.

This is the **only** module in the codebase allowed to import from the
ElevenLabs SDK. Per ADR 0006, every other consumer goes through the
``CaseManager`` facade.

Call-state observability for our setup
======================================

We use the ElevenLabs native Twilio integration, so ElevenLabs holds
the conversation WebSocket on the customer's behalf. We do **not**
see turn-by-turn events while the call is happening. We learn what
was said by polling ``conversations.get(conversation_id)`` after the
call ends.

``CallSession.place`` runs in three phases:

1. Initiate the outbound call. Get back a ``conversation_id``.
2. Poll until ElevenLabs reports the conversation in a terminal
   state (``done`` or ``failed``). Bounded by
   ``LiveCallSessionSettings.max_wait_seconds``.
3. Convert the conversation response into a ``PostCallReport``,
   funnel it through ``case._post_call.ingest_post_call_report``
   (so the case audit log + bus see the closing event), and return
   the resulting ``CallOutcome`` to ``CaseManager``.

There is no stub. Everything that runs through this module places a
real outbound call to a real phone. The simulator and ``CaseManager``
both use the same ``CallSession`` instance — there is no second
implementation that fabricates dialogue.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, final

import structlog
from elevenlabs.client import ElevenLabs

from guidepoint.case._models import (
    BusinessOutcome,
    CallOutcome,
    Case,
    CaseError,
    CaseEvent,
    EventSource,
    PostCallReport,
    PostCallStatus,
    SlotId,
    TranscriptRole,
    TranscriptTurn,
)
from guidepoint.case._post_call import ingest_post_call_report
from guidepoint.case._repository import CaseRepository
from guidepoint.clock import Clock
from guidepoint.events import EventBus

_CaseEventBus = EventBus[CaseEvent]

_log = structlog.get_logger(__name__)


class CallSession(Protocol):
    """Place one call attempt and report the post-call outcome.

    Returns when ElevenLabs declares the call ended. The returned
    ``CallOutcome`` carries the full formatted transcript and is what
    ``CaseManager`` reads to drive the case state machine.
    """

    async def place(self, case: Case) -> CallOutcome:
        """Initiate the call, poll until done, ingest report, return outcome."""
        ...


@final
@dataclass(frozen=True, slots=True)
class LiveCallSessionSettings:
    """Knobs for the live ElevenLabs adapter.

    ``poll_interval_seconds`` — how often to poll the conversation
    status while the call is in progress. 3s is gentle on the API
    while still catching short calls.

    ``max_wait_seconds`` — hard ceiling on how long we'll wait for the
    call to end. 700s = 600s (Kate's max call duration) + 100s buffer
    for ElevenLabs's post-processing pipeline (analysis + transcript
    finalization).
    """

    poll_interval_seconds: float = 3.0
    max_wait_seconds: float = 700.0


def build_live_call_session(
    *,
    client: ElevenLabs,
    agent_id: str,
    phone_number_id: str,
    case_repo: CaseRepository,
    bus: _CaseEventBus,
    clock: Clock,
    settings: LiveCallSessionSettings | None = None,
) -> CallSession:
    """Construct the live ``CallSession``.

    Raises ``CaseError`` immediately if ``agent_id`` or
    ``phone_number_id`` are blank — fail fast at boot rather than at
    the first Fire button press.
    """
    if not agent_id:
        raise CaseError("agent_id required (set ELEVENLABS_AGENT_ID in .env)")
    if not phone_number_id:
        raise CaseError(
            "phone_number_id required (set ELEVENLABS_AGENT_PHONE_NUMBER_ID in .env)"
        )
    return _LiveCallSession(
        client=client,
        agent_id=agent_id,
        phone_number_id=phone_number_id,
        case_repo=case_repo,
        bus=bus,
        clock=clock,
        settings=settings or LiveCallSessionSettings(),
    )


@final
class _LiveCallSession:
    """The one and only ``CallSession`` — wraps the ElevenLabs SDK."""

    def __init__(
        self,
        *,
        client: ElevenLabs,
        agent_id: str,
        phone_number_id: str,
        case_repo: CaseRepository,
        bus: _CaseEventBus,
        clock: Clock,
        settings: LiveCallSessionSettings,
    ) -> None:
        self._client = client
        self._agent_id = agent_id
        self._phone_number_id = phone_number_id
        self._case_repo = case_repo
        self._bus = bus
        self._clock = clock
        self._settings = settings

    async def place(self, case: Case) -> CallOutcome:
        attempt_number = case.attempt_count + 1
        started_at = self._clock.now()

        await self._emit(
            case, attempt_number, "call.dialing", case.customer.phone, "system"
        )

        try:
            response = await asyncio.to_thread(
                self._client.conversational_ai.twilio.outbound_call,
                agent_id=self._agent_id,
                agent_phone_number_id=self._phone_number_id,
                to_number=case.customer.phone,
                conversation_initiation_client_data={
                    "dynamic_variables": case.to_variables(),
                },
            )
        except Exception as exc:
            await self._emit(
                case,
                attempt_number,
                "call.placement_failed",
                f"{type(exc).__name__}: {exc}",
                "elevenlabs",
            )
            raise CaseError(f"ElevenLabs outbound_call failed: {exc}") from exc

        conversation_id = _extract_conversation_id(response)
        if not conversation_id:
            raise CaseError(
                f"ElevenLabs outbound_call returned no conversation_id: {response!r}"
            )

        await self._emit(
            case,
            attempt_number,
            "call.placed",
            f"conversation_id={conversation_id}",
            "elevenlabs",
        )

        conversation = await self._poll_until_terminal(conversation_id)
        ended_at = self._clock.now()

        report = _conversation_to_report(
            conversation=conversation,
            conversation_id=conversation_id,
            started_at=started_at,
            ended_at=ended_at,
        )
        return await ingest_post_call_report(
            case=case,
            attempt_number=attempt_number,
            report=report,
            case_repo=self._case_repo,
            bus=self._bus,
            clock=self._clock,
        )

    async def _poll_until_terminal(self, conversation_id: str) -> Any:
        """Poll until status is ``done`` or ``failed``, bounded by max_wait_seconds."""
        deadline = self._clock.now() + timedelta(seconds=self._settings.max_wait_seconds)
        while self._clock.now() < deadline:
            await asyncio.sleep(self._settings.poll_interval_seconds)
            try:
                conv = await asyncio.to_thread(
                    self._client.conversational_ai.conversations.get,
                    conversation_id=conversation_id,
                )
            except Exception as exc:
                _log.warning(
                    "conversation.poll.error",
                    conversation_id=conversation_id,
                    error=str(exc),
                )
                continue
            status = _extract_status(conv)
            if status in ("done", "failed"):
                return conv
        raise CaseError(
            f"conversation {conversation_id} did not reach terminal state "
            f"within {self._settings.max_wait_seconds}s"
        )

    async def _emit(
        self,
        case: Case,
        attempt_number: int,
        event_name: str,
        detail: str,
        source: EventSource,
    ) -> None:
        event = CaseEvent(
            event_id=f"evt_{secrets.token_hex(6)}",
            case_id=case.case_id,
            correlation_id=case.correlation_id,
            attempt_number=attempt_number,
            timestamp=self._clock.now(),
            source=source,
            level="info",
            event=event_name,
            detail=detail,
        )
        self._case_repo.append_event(case.case_id, event)
        await self._bus.publish(event)


# --------------------------------------------------------------------------- #
# Pure helpers — defensive against SDK shape drift                            #
# --------------------------------------------------------------------------- #


def _extract_conversation_id(response: Any) -> str:
    for attr in ("conversation_id", "conversationId"):
        v = getattr(response, attr, None)
        if v:
            return str(v)
    if isinstance(response, dict):
        for key in ("conversation_id", "conversationId"):
            if response.get(key):
                return str(response[key])
    return ""


def _extract_status(conversation: Any) -> str:
    s = getattr(conversation, "status", None)
    if isinstance(s, str):
        return s
    if hasattr(s, "value"):
        return str(s.value)
    return "unknown"


def _conversation_to_report(
    *,
    conversation: Any,
    conversation_id: str,
    started_at: datetime,
    ended_at: datetime,
) -> PostCallReport:
    raw_status = _extract_status(conversation)
    status: PostCallStatus = "done" if raw_status == "done" else "failed"
    business_outcome, booked_slot_id = _extract_business_outcome(conversation)
    return PostCallReport(
        elevenlabs_conversation_id=conversation_id,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=_extract_duration(conversation, started_at, ended_at),
        transcript=_extract_transcript(conversation),
        business_outcome=business_outcome,
        booked_slot_id=booked_slot_id,
        recording_url=str(getattr(conversation, "audio_url", "") or ""),
        error_detail="" if status == "done" else f"call ended with status={raw_status}",
    )


def _extract_transcript(conversation: Any) -> tuple[TranscriptTurn, ...]:
    raw = getattr(conversation, "transcript", None) or []
    out: list[TranscriptTurn] = []
    for turn in raw:
        role_raw = _get(turn, "role")
        message = _get(turn, "message")
        time_in_call = _get(turn, "time_in_call_secs") or _get(turn, "time_in_call_seconds") or 0.0
        if not message:
            continue
        normalized_role: TranscriptRole = "agent" if role_raw == "agent" else "user"
        out.append(
            TranscriptTurn(
                role=normalized_role,
                message=str(message),
                time_in_call_seconds=float(time_in_call),
            )
        )
    return tuple(out)


def _extract_business_outcome(
    conversation: Any,
) -> tuple[BusinessOutcome, SlotId | None]:
    """Read scheduled_date + scheduled_time from data_collection_results.

    Kate's agent has two configured data-collection fields:
    ``scheduled_date`` (YYYY-MM-DD) and ``scheduled_time`` (HH:MM 24h).
    A booking is recorded **only** when both fields contain a non-empty
    string. The wrapper objects ElevenLabs returns under each key carry
    ``.value=None`` whenever the agent never collected the answer
    (e.g. the call ended early, the agent errored, the customer
    declined). Treating a present-but-empty wrapper as "booked" was
    case ``case_c514b77a212d2999`` — don't repeat that.
    """
    analysis = getattr(conversation, "analysis", None)
    if analysis is None:
        return "inconclusive", None
    data = getattr(analysis, "data_collection_results", None) or {}
    scheduled_date = _data_collection_value(data, "scheduled_date")
    scheduled_time = _data_collection_value(data, "scheduled_time")
    if scheduled_date and scheduled_time:
        normalized = f"{scheduled_date}_{scheduled_time}".replace("-", "").replace(":", "")
        return "booked", SlotId(f"slot_chosen_{normalized}")
    return "inconclusive", None


def _data_collection_value(data: Any, key: str) -> str | None:
    """Pull the actual collected string out of a ``data_collection_results`` entry.

    The conversation API returns one of two shapes per key:

    1. A bare string (older endpoints, our test fakes) — return as-is.
    2. A ``DataCollectionResult`` wrapper with attributes
       ``value``, ``json_schema``, ``rationale`` — return ``.value``.

    In both cases an empty / whitespace-only / ``None`` payload means
    "not collected" and yields ``None`` so the caller can fall back to
    ``inconclusive``.
    """
    if isinstance(data, dict):
        wrapper = data.get(key)
    else:
        wrapper = getattr(data, key, None)
    if wrapper is None:
        return None
    if isinstance(wrapper, str):
        cleaned = wrapper.strip()
        return cleaned or None
    value = getattr(wrapper, "value", None)
    if value is None and isinstance(wrapper, dict):
        value = wrapper.get("value")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_duration(
    conversation: Any, started_at: datetime, ended_at: datetime
) -> float:
    metadata = getattr(conversation, "metadata", None)
    if metadata is not None:
        for attr in ("call_duration_secs", "duration_secs", "call_duration_seconds"):
            v = getattr(metadata, attr, None)
            if v is not None:
                return float(v)
    return max((ended_at - started_at).total_seconds(), 0.0)


def _get(obj: Any, key: str) -> Any:
    """Get ``key`` from ``obj`` whether it's a dict or an attr-style model."""
    if isinstance(obj, dict):
        return obj.get(key)
    inner = getattr(obj, "value", None) if hasattr(obj, "value") else None
    if isinstance(inner, dict):
        return inner.get(key)
    return getattr(obj, key, None)


__all__ = [
    "CallSession",
    "LiveCallSessionSettings",
    "build_live_call_session",
]
