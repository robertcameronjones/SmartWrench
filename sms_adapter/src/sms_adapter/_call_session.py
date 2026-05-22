"""SMS implementation of the :class:`guidepoint.case.CallSession` Protocol.

A single, long-running "call" per case: ``place(case)`` returns when
the conversation reaches a business outcome (booked / declined) or
times out from inactivity. Per the FCC framing the operator endorsed,
an SMS exchange IS a call — same lifecycle, same Protocol, same
terminal outcomes — just one whose connected state can span hours
instead of minutes.

The session owns one ``asyncio.Queue`` per active case. The Twilio
webhook handler (in ``simulator._app``) calls
:meth:`SmsCallSession.deliver_inbound` to enqueue each inbound text;
the per-case loop inside :meth:`place` dequeues, runs one LLM turn,
sends the reply, and persists turn-level :class:`CaseEvent`s onto
the case audit log.

Terminal detection (today, conservative):

- **Booked** — the LLM's outbound reply contains one of the case's
  ``offered_slots[i].display`` strings together with a confirmation
  verb (``confirm`` / ``scheduled`` / ``booked`` / ``set up`` /
  ``see you``). Matches Kate's existing conversational style; no
  prompt change needed to make this work. The matched slot's id is
  returned on the ``CallOutcome``.
- **Declined** — the customer sends a stop keyword (``STOP``,
  ``UNSUBSCRIBE``, ``CANCEL``, ``END``, ``QUIT``).
- **Inactivity timeout** — no inbound for the configured window
  (default 24h). Maps to ``business_outcome="inconclusive"``, which
  the case manager translates to terminal state ``UNREACHABLE``.

Restart behavior: in-flight SMS conversations are lost on process
restart (no different from voice today). The persistent ``Case`` and
the history JSON survive, so we can rebuild active sessions on
startup later; that's out of scope for this commit.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import final

import structlog

from guidepoint.case import (
    BusinessOutcome,
    CallOutcome,
    CallResult,
    Case,
    CaseEvent,
    CaseId,
    CaseRepository,
    SlotId,
)
from guidepoint.clock import Clock
from guidepoint.events import EventBus
from prompt_composer import Channel, PromptPaths, build_prompt

from sms_adapter import (
    HistoryStore,
    LlmComplete,
    RoutingStore,
    Turn,
    TurnRole,
    TwilioSend,
)
from sms_adapter._event_log import log_event

_CaseEventBus = EventBus[CaseEvent]

_log = structlog.get_logger(__name__)

# Customer side: any of these (case-insensitive, trimmed) closes the
# case with business_outcome="declined" -> CaseState.DECLINED.
_STOP_KEYWORDS: frozenset[str] = frozenset(
    {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
)

# Assistant side: presence of any of these in a reply (alongside a
# match of one of the offered slot displays) flips the case to
# business_outcome="booked".
_CONFIRMATION_VERBS: tuple[str, ...] = (
    "confirm",
    "scheduled",
    "booked",
    "set up",
    "set you up",
    "see you then",
    "see you on",
    "all set",
    "you're scheduled",
    "you are scheduled",
    "you're booked",
    "you are booked",
)

# Default inactivity window before we declare an SMS case
# UNREACHABLE. 24h matches the operator's mental model: if the
# customer hasn't replied by the next day, Kate moves on.
DEFAULT_INACTIVITY_TIMEOUT = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Inbound turn (what the webhook hands to deliver_inbound)
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class _InboundTurn:
    """One inbound SMS forwarded from the Twilio webhook to an active session."""

    from_number: str
    body: str
    message_sid: str
    received_at: datetime


@dataclass
class _ActiveConversation:
    """Per-case state held while ``place(case)`` is running.

    The session-level lock guards inserts/removals into the registry;
    the queue itself is async-safe.
    """

    case_id: CaseId
    customer_phone: str
    queue: "asyncio.Queue[_InboundTurn]" = field(default_factory=asyncio.Queue)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_sms_call_session(
    *,
    twilio_send: TwilioSend,
    llm_complete: LlmComplete,
    history: HistoryStore,
    routing: RoutingStore,
    prompt_paths: PromptPaths,
    case_repo: CaseRepository,
    bus: _CaseEventBus,
    clock: Clock,
    event_log_path: Path | None = None,
    inactivity_timeout: timedelta = DEFAULT_INACTIVITY_TIMEOUT,
) -> "SmsCallSession":
    """Build the long-running SMS ``CallSession``.

    The returned instance is shared across every SMS case (one
    instance per process, like ``_LiveCallSession`` for voice). It
    holds an internal registry of active conversations so the Twilio
    inbound webhook can route turns back to the right ``place()``
    loop via :meth:`SmsCallSession.deliver_inbound`.
    """
    return SmsCallSession(
        twilio_send=twilio_send,
        llm_complete=llm_complete,
        history=history,
        routing=routing,
        prompt_paths=prompt_paths,
        case_repo=case_repo,
        bus=bus,
        clock=clock,
        event_log_path=event_log_path,
        inactivity_timeout=inactivity_timeout,
    )


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


@final
class SmsCallSession:
    """One process-wide ``CallSession`` for the SMS channel.

    Public because the webhook handler needs :meth:`deliver_inbound`
    and the route layer needs :meth:`has_active`. The constructor is
    not the intended entry point — use :func:`build_sms_call_session`.
    """

    def __init__(
        self,
        *,
        twilio_send: TwilioSend,
        llm_complete: LlmComplete,
        history: HistoryStore,
        routing: RoutingStore,
        prompt_paths: PromptPaths,
        case_repo: CaseRepository,
        bus: _CaseEventBus,
        clock: Clock,
        event_log_path: Path | None,
        inactivity_timeout: timedelta,
    ) -> None:
        self._twilio_send = twilio_send
        self._llm_complete = llm_complete
        self._history = history
        self._routing = routing
        self._prompt_paths = prompt_paths
        self._case_repo = case_repo
        self._bus = bus
        self._clock = clock
        self._event_log_path = event_log_path
        self._inactivity_timeout = inactivity_timeout
        self._active: dict[CaseId, _ActiveConversation] = {}
        self._lock = asyncio.Lock()

    # -- CallSession Protocol -----------------------------------------------

    async def place(self, case: Case) -> CallOutcome:
        """Run the full SMS conversation for ``case`` until terminal.

        Routing: the inbound webhook resolves ``customer_phone`` to
        ``case_id`` via the routing store, then calls
        :meth:`deliver_inbound` which enqueues onto this session's
        per-case queue. We loop on that queue.

        Returns a ``CallOutcome`` that the ``CaseManager`` reads to
        decide the case's terminal state.
        """
        attempt_number = case.attempt_count + 1
        started_at = self._clock.now()

        await self._register_active(case)
        try:
            await self._send_opening(case=case, attempt_number=attempt_number)
            business_outcome, booked_slot_id, error_detail = await self._run_loop(
                case=case, attempt_number=attempt_number
            )
        except Exception:
            _log.exception("sms.session.place.errored", case_id=case.case_id)
            raise
        finally:
            await self._deregister_active(case.case_id)
            # Unbind routing so a stale inbound to this phone doesn't
            # land on a terminated session.
            self._routing.unbind(phone=case.customer.phone)

        ended_at = self._clock.now()
        transcript_text = _format_history(self._history.load(case.case_id))
        # SMS always "answers" in the telephony sense: we got the
        # opening message out the door (otherwise we'd have raised
        # before reaching this point). The CallResult literal is for
        # voice telephony failure modes (no_answer / busy / etc.) and
        # has no direct SMS analog.
        result: CallResult = "answered" if not error_detail else "error"
        return CallOutcome(
            result=result,
            business_outcome=business_outcome,
            booked_slot_id=booked_slot_id,
            elevenlabs_conversation_id="",
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=max((ended_at - started_at).total_seconds(), 0.0),
            transcript=transcript_text,
            recording_url="",
            error_detail=error_detail,
        )

    # -- Webhook entry point ------------------------------------------------

    async def deliver_inbound(
        self,
        *,
        case_id: CaseId,
        from_number: str,
        body: str,
        message_sid: str,
    ) -> bool:
        """Enqueue an inbound turn for the active session.

        Returns ``True`` if a session is running for ``case_id`` and
        the turn was queued, ``False`` otherwise (caller logs + drops).
        """
        async with self._lock:
            active = self._active.get(case_id)
        if active is None:
            return False
        await active.queue.put(
            _InboundTurn(
                from_number=from_number,
                body=body,
                message_sid=message_sid,
                received_at=self._clock.now(),
            )
        )
        return True

    def has_active(self, case_id: CaseId) -> bool:
        """Synchronous probe — useful for the webhook to check before queueing."""
        return case_id in self._active

    # -- Internals ----------------------------------------------------------

    async def _register_active(self, case: Case) -> None:
        async with self._lock:
            if case.case_id in self._active:
                _log.warning(
                    "sms.session.duplicate_register",
                    case_id=case.case_id,
                )
            self._active[case.case_id] = _ActiveConversation(
                case_id=case.case_id,
                customer_phone=case.customer.phone,
            )
        # Bind routing AFTER registering so an inbound that races with
        # the bind always finds an active queue. Pass ``user_id`` and
        # ``channel`` so the inbound webhook can audit-log the owner
        # without a second hop, and so future per-user case repos can
        # route on it.
        self._routing.bind(
            phone=case.customer.phone,
            conversation_id=case.case_id,
            user_id=case.user_id,
            channel=case.channel,
        )

    async def _deregister_active(self, case_id: CaseId) -> None:
        async with self._lock:
            self._active.pop(case_id, None)

    async def _send_opening(self, *, case: Case, attempt_number: int) -> None:
        """Compose the opening prompt, ask the LLM, send via Twilio, persist."""
        reply = await asyncio.to_thread(self._take_turn, case)
        sid = await asyncio.to_thread(
            self._twilio_send, to=case.customer.phone, body=reply
        )
        log_event(
            self._event_log_path,
            f"us->sms to={case.customer.phone} conv={case.case_id} sid={sid}",
            reply,
        )
        self._history.append(
            case.case_id,
            Turn(
                role=TurnRole.ASSISTANT,
                text=reply,
                timestamp=self._clock.now(),
                twilio_sid=sid,
            ),
        )
        await self._emit(
            case=case,
            attempt_number=attempt_number,
            event="sms.opened",
            detail=f"to={case.customer.phone} sid={sid}",
        )

    async def _run_loop(
        self,
        *,
        case: Case,
        attempt_number: int,
    ) -> tuple[BusinessOutcome, SlotId | None, str]:
        """Loop on the inbound queue until terminal. Returns (outcome, slot, error_detail)."""
        active = self._active[case.case_id]
        timeout_seconds = self._inactivity_timeout.total_seconds()
        while True:
            try:
                inbound = await asyncio.wait_for(
                    active.queue.get(), timeout=timeout_seconds
                )
            except TimeoutError:
                await self._emit(
                    case=case,
                    attempt_number=attempt_number,
                    event="sms.timeout",
                    detail=f"no inbound within {self._inactivity_timeout}",
                )
                return "inconclusive", None, ""

            normalized = inbound.body.strip().upper()
            self._history.append(
                case.case_id,
                Turn(
                    role=TurnRole.USER,
                    text=inbound.body,
                    timestamp=inbound.received_at,
                    twilio_sid=inbound.message_sid,
                ),
            )
            log_event(
                self._event_log_path,
                f"sms->us from={inbound.from_number} conv={case.case_id} "
                f"sid={inbound.message_sid}",
                inbound.body,
            )
            await self._emit(
                case=case,
                attempt_number=attempt_number,
                event="sms.inbound",
                detail=_summarize(inbound.body, prefix=f"from={inbound.from_number}"),
            )

            if normalized in _STOP_KEYWORDS:
                await self._emit(
                    case=case,
                    attempt_number=attempt_number,
                    event="sms.declined",
                    detail=f"stop keyword: {normalized}",
                )
                return "declined", None, ""

            # One assistant turn.
            try:
                reply = await asyncio.to_thread(self._take_turn, case)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                await self._emit(
                    case=case,
                    attempt_number=attempt_number,
                    event="sms.llm_failed",
                    detail=detail,
                )
                return "inconclusive", None, detail
            try:
                sid = await asyncio.to_thread(
                    self._twilio_send, to=case.customer.phone, body=reply
                )
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                await self._emit(
                    case=case,
                    attempt_number=attempt_number,
                    event="sms.send_failed",
                    detail=detail,
                )
                return "inconclusive", None, detail
            log_event(
                self._event_log_path,
                f"us->sms to={case.customer.phone} conv={case.case_id} sid={sid}",
                reply,
            )
            self._history.append(
                case.case_id,
                Turn(
                    role=TurnRole.ASSISTANT,
                    text=reply,
                    timestamp=self._clock.now(),
                    twilio_sid=sid,
                ),
            )
            await self._emit(
                case=case,
                attempt_number=attempt_number,
                event="sms.outbound",
                detail=_summarize(reply, prefix=f"sid={sid}"),
            )

            booked_slot = _detect_booking(reply, case=case)
            if booked_slot is not None:
                await self._emit(
                    case=case,
                    attempt_number=attempt_number,
                    event="sms.booked",
                    detail=f"slot={booked_slot}",
                )
                return "booked", booked_slot, ""

    def _take_turn(self, case: Case) -> str:
        """Compose system prompt + history, call the LLM, return the reply.

        Synchronous because the existing ``LlmComplete`` callable is
        sync (LiteLLM under the hood). We wrap in ``to_thread`` from
        the caller so the event loop isn't blocked.
        """
        rendered = build_prompt(
            case=case,
            channel=Channel.SMS,
            paths=self._prompt_paths,
        )
        history = self._history.load(case.case_id)
        # On the very first turn (open conversation) there is no
        # history yet AND we are about to send the opener. Chat models
        # need a user message to respond to; inject a synthetic
        # kickoff so the model proceeds with the opening line the
        # prompt defines. Mirrors the prior take_turn convention so
        # the SMS prompt itself doesn't have to change.
        if not history:
            history = (
                Turn(
                    role=TurnRole.USER,
                    text="(open conversation)",
                    timestamp=self._clock.now(),
                    twilio_sid="",
                ),
            )
        return self._llm_complete(system=rendered.text, history=history)

    async def _emit(
        self,
        *,
        case: Case,
        attempt_number: int,
        event: str,
        detail: str,
    ) -> None:
        evt = CaseEvent(
            event_id=f"evt_{secrets.token_hex(6)}",
            case_id=case.case_id,
            correlation_id=case.correlation_id,
            attempt_number=attempt_number,
            timestamp=self._clock.now(),
            source="sms",
            level="info",
            event=event,
            detail=detail,
        )
        self._case_repo.append_event(case.case_id, evt)
        await self._bus.publish(evt)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _detect_booking(reply: str, *, case: Case) -> SlotId | None:
    """Heuristic: which offered slot (if any) did the LLM just confirm?

    Returns the matching ``SlotId`` if the reply contains both:
    - One of the slot ``display`` strings (case-insensitive, with
      lenient whitespace), AND
    - A confirmation verb from ``_CONFIRMATION_VERBS``.

    Returns ``None`` if no slot matches or no confirmation verb is
    present. Conservative on purpose — false positives mark cases
    BOOKED that aren't, which is worse than not detecting and falling
    through to the inactivity timeout.
    """
    reply_lower = reply.lower()
    if not any(verb in reply_lower for verb in _CONFIRMATION_VERBS):
        return None
    for slot in case.offered_slots:
        if _phrase_appears(needle=slot.display.lower(), haystack=reply_lower):
            return slot.id
    return None


_WS = re.compile(r"\s+")


def _phrase_appears(*, needle: str, haystack: str) -> bool:
    """Whitespace-insensitive substring match.

    The LLM sometimes rewords a slot display ("Tuesday, May 12, 2026
    - 8:30 AM" vs "Tuesday, May 12, 2026 at 8:30 AM"). Normalize
    runs of whitespace and the common ``-`` / ``at`` separator before
    comparing.
    """
    normalized_needle = _WS.sub(" ", needle).strip().replace(" - ", " at ")
    normalized_haystack = _WS.sub(" ", haystack).strip().replace(" - ", " at ")
    return normalized_needle in normalized_haystack


def _format_history(turns: tuple[Turn, ...]) -> str:
    """Render the full SMS exchange into one auditable transcript string.

    Format matches ``_format_transcript`` in voice's ``_post_call``
    closely enough that downstream consumers don't have to branch on
    channel — both use ``Speaker: text`` lines, just SMS lacks the
    in-call timestamp (we use UTC wallclock instead).
    """
    if not turns:
        return ""
    lines: list[str] = []
    for turn in turns:
        speaker = "Kate" if turn.role == TurnRole.ASSISTANT else "Customer"
        ts = turn.timestamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        lines.append(f"[{ts}] {speaker}: {turn.text}")
    return "\n".join(lines)


def _summarize(text: str, *, prefix: str, limit: int = 160) -> str:
    """Truncate ``text`` and prefix it for the audit log."""
    clipped = text.strip().replace("\n", " ")
    if len(clipped) > limit:
        clipped = clipped[: limit - 1] + "\u2026"
    return f"{prefix} body={clipped!r}"


__all__ = [
    "DEFAULT_INACTIVITY_TIMEOUT",
    "SmsCallSession",
    "build_sms_call_session",
]
