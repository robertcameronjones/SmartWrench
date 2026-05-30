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
- **Post-booking** (``CallStage.INITIAL_REMINDER`` /
  ``FINAL_REMINDER``) — the customer's inbound text is classified
  into ``confirmed`` / ``rescheduled`` / ``cancelled`` (matching the
  numbered options in ``prompt-post-booking.md``). That
  ``business_outcome`` is returned on the ``CallOutcome`` for the v2
  ``CaseDriver`` to consume via ``CallEnded``.
- **Declined** — during outreach the customer sends ``CANCEL`` (not
  STOP — see opt-out below). During post-booking, ``CANCEL`` means
  cancel the appointment (``business_outcome="cancelled"``).
- **Opted out** — the customer sends a stop keyword (``STOP``,
  ``UNSUBSCRIBE``, ``END``, ``QUIT``) at any stage. Maps to
  ``business_outcome="opted_out"`` and terminal state ``OPTED_OUT``.
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
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import final

import structlog

from guidepoint.case import (
    BusinessOutcome,
    CallManager,
    CallOutcome,
    CallResult,
    CallStage,
    Case,
    CaseEvent,
    CaseId,
    CaseRepository,
    SlotId,
    lookup_slot_display,
)
from guidepoint.clock import Clock
from guidepoint.events import EventBus
from prompt_composer import Channel, PromptPaths, PromptStage, build_prompt

from sms_adapter import (
    HistoryStore,
    LlmComplete,
    RoutingStore,
    Turn,
    TurnRole,
    TwilioSend,
)
from sms_adapter._event_log import log_event
from sms_adapter._optout import is_opt_out_keyword, normalize_sms_body

_CaseEventBus = EventBus[CaseEvent]

_log = structlog.get_logger(__name__)

# Customer side: STOP-style keywords at any stage → opted_out.
# Outreach ``CANCEL`` is handled separately below.
# Post-booking inbound (INITIAL_REMINDER / FINAL_REMINDER): maps the
# customer reply → ``CallOutcome.business_outcome``. Aligns with
# prompt-post-booking.md options 1 Confirmed / 2 Reschedule / 3 Cancel.
_POST_BOOKING_CONFIRMED: frozenset[str] = frozenset(
    {"1", "CONFIRMED", "CONFIRM", "YES", "Y", "OK", "OKAY", "YEP", "YUP"}
)
_POST_BOOKING_RESCHEDULE: frozenset[str] = frozenset(
    {"2", "RESCHEDULE", "RESCHED", "RESCHEDULED", "RESCHEDULING", "NEW TIME"}
)
_POST_BOOKING_CANCEL: frozenset[str] = frozenset(
    {"3", "CANCEL", "CANCELLED", "CANCELED", "NO", "N"}
)

_POST_BOOKING_STAGES: frozenset[CallStage] = frozenset(
    {CallStage.INITIAL_REMINDER, CallStage.FINAL_REMINDER}
)

# Default inactivity window before we declare an SMS case
# UNREACHABLE. 24h matches the operator's mental model: if the
# customer hasn't replied by the next day, Kate moves on.
DEFAULT_INACTIVITY_TIMEOUT = timedelta(hours=24)

# Maximum inbound turns we will buffer per active conversation. A
# healthy SMS exchange is 4-12 turns; 32 is comfortable headroom that
# still catches runaway senders (spammers, stuck retry loops). When
# full, additional inbound is dropped and a ``queue.overflow.sms_inbound``
# structured warning is logged. The conversation continues with the
# turns it already has; the inactivity timer (above) will eventually
# wind the case down to its terminal if the customer-side problem
# persists.
SMS_INBOUND_QUEUE_MAX = 32


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
    the queue itself is async-safe. The queue is bounded at
    ``SMS_INBOUND_QUEUE_MAX`` so a runaway sender cannot grow process
    memory unbounded — see ``deliver_inbound`` for the overflow path.
    """

    case_id: CaseId
    customer_phone: str
    queue: "asyncio.Queue[_InboundTurn]" = field(
        default_factory=lambda: asyncio.Queue(maxsize=SMS_INBOUND_QUEUE_MAX)
    )


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
    """Build the long-running SMS ``CallSession`` (v1 Protocol).

    The returned instance is shared across every SMS case (one
    instance per process, like ``_LiveCallSession`` for voice). It
    holds an internal registry of active conversations so the Twilio
    inbound webhook can route turns back to the right ``start()``
    / ``place()`` loop via :meth:`SmsCallSession.deliver_inbound`.

    The returned instance also satisfies the v2 ``CallManager`` Protocol;
    callers wiring the Phase 4 ``CaseDriver`` should use
    :func:`build_sms_call_manager`, which returns the same instance
    typed as ``CallManager``. Both ``deliver_inbound`` and
    ``has_active`` remain accessible via the concrete ``SmsCallSession``
    handle the webhook holds.
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


def build_sms_call_manager(
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
) -> CallManager:
    """Build the SMS ``CallManager`` (v2 Protocol).

    Same underlying ``SmsCallSession`` instance as
    :func:`build_sms_call_session`; returned typed as ``CallManager``
    so the Phase 4 ``CaseDriver`` can wire it without a runtime cast.
    Both Protocols are satisfied by one class — kept that way so
    SMS-side behaviour never drifts between v1 and v2 entry points.

    Note: the webhook handler still needs the concrete
    ``SmsCallSession`` to call ``deliver_inbound`` / ``has_active``.
    Build via :func:`build_sms_call_session` for that path, then pass
    the same object to the driver typed as ``CallManager`` via this
    builder (or just type-narrow at the call site).
    """
    return build_sms_call_session(
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
        """v1 ``CallSession`` Protocol entry. Delegates to ``start``.

        Preserves the historical attempt-number convention so the v1
        ``CaseManager`` loop sees no behavioural change. The v2
        ``CaseDriver`` should call ``start`` directly with an explicit
        ``stage``.
        """
        return await self.start(
            case=case,
            stage=CallStage.OUTREACH,
            attempt_number=case.attempt_count + 1,
        )

    async def start(
        self,
        *,
        case: Case,
        stage: CallStage,
        attempt_number: int,
    ) -> CallOutcome:
        """v2 ``CallManager`` Protocol entry. Run the full SMS conversation
        for ``case`` until terminal.

        Routing: the inbound webhook resolves ``customer_phone`` to
        ``case_id`` via the routing store, then calls
        :meth:`deliver_inbound` which enqueues onto this session's
        per-case queue. We loop on that queue.

        ``stage`` is logged on every line and recorded on the per-turn
        ``CaseEvent`` audit trail. Phase 5 keeps the conversation
        behaviour identical across stages — the configured SMS prompt
        runs unchanged. Phase 7 hooks stage-aware prompt selection into
        ``prompt_composer``; passing the parameter now keeps the driver
        path single-shot when that lands.
        """
        started_at = self._clock.now()
        _log.info(
            "sms_call_manager.start",
            case_id=case.case_id,
            correlation_id=case.correlation_id,
            stage=stage.value,
            attempt_number=attempt_number,
            to=case.customer.phone,
        )

        await self._register_active(case)
        try:
            await self._send_opening(
                case=case, attempt_number=attempt_number, stage=stage
            )
            business_outcome, booked_slot_id, error_detail = await self._run_loop(
                case=case, attempt_number=attempt_number, stage=stage
            )
        except Exception:
            _log.exception(
                "sms_call_manager.start.errored",
                case_id=case.case_id,
                stage=stage.value,
                attempt_number=attempt_number,
            )
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
        booked_display = ""
        if booked_slot_id is not None:
            booked_display = lookup_slot_display(
                offered_slots=case.offered_slots, slot_id=booked_slot_id
            )
        outcome = CallOutcome(
            result=result,
            business_outcome=business_outcome,
            booked_slot_id=booked_slot_id,
            booked_slot_display=booked_display,
            elevenlabs_conversation_id="",
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=max((ended_at - started_at).total_seconds(), 0.0),
            transcript=transcript_text,
            recording_url="",
            error_detail=error_detail,
        )
        _log.info(
            "sms_call_manager.completed",
            case_id=case.case_id,
            stage=stage.value,
            attempt_number=attempt_number,
            duration_seconds=outcome.duration_seconds,
            business_outcome=outcome.business_outcome,
            result=outcome.result,
        )
        return outcome

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
        the turn was queued, ``False`` otherwise. Two failure modes
        produce ``False``:

        - **no active session** for ``case_id`` (case already terminal
          or never started here): caller logs + drops silently.
        - **inbound queue full** (``SMS_INBOUND_QUEUE_MAX`` reached
          without the LLM loop draining): we emit a structured
          ``queue.overflow.sms_inbound`` warning so log scrapers can
          alert, and the caller drops the turn. The conversation's
          inactivity timer (24h) is the safety net that eventually
          winds the case down if overflow keeps happening.
        """
        async with self._lock:
            active = self._active.get(case_id)
        if active is None:
            return False
        try:
            active.queue.put_nowait(
                _InboundTurn(
                    from_number=from_number,
                    body=body,
                    message_sid=message_sid,
                    received_at=self._clock.now(),
                )
            )
        except asyncio.QueueFull:
            _log.warning(
                "queue.overflow.sms_inbound",
                case_id=case_id,
                from_number=from_number,
                message_sid=message_sid,
                current_depth=active.queue.qsize(),
                max_depth=SMS_INBOUND_QUEUE_MAX,
            )
            return False
        return True

    def has_active(self, case_id: CaseId) -> bool:
        """Synchronous probe — useful for the webhook to check before queueing."""
        return case_id in self._active

    def queue_depths(self) -> dict[CaseId, tuple[int, int]]:
        """Snapshot of every active conversation's inbound-queue depth.

        Returns ``{case_id: (current_depth, max_depth)}``. Intended for
        a health / debug endpoint so operators can spot a queue that's
        creeping toward overflow without waiting for the structured
        warning. Synchronous and non-blocking; safe to call from
        anywhere.
        """
        return {
            case_id: (active.queue.qsize(), SMS_INBOUND_QUEUE_MAX)
            for case_id, active in self._active.items()
        }

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
            channel=case.initial_channel,
        )

    async def _deregister_active(self, case_id: CaseId) -> None:
        async with self._lock:
            self._active.pop(case_id, None)

    async def _send_opening(
        self, *, case: Case, attempt_number: int, stage: CallStage
    ) -> None:
        """Compose the opening prompt, ask the LLM, send via Twilio, persist."""
        reply = await asyncio.to_thread(self._take_turn, case, stage)
        sid = await asyncio.to_thread(
            self._twilio_send,
            case_id=case.case_id,
            to=case.customer.phone,
            body=reply,
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
            detail=f"stage={stage.value} to={case.customer.phone} sid={sid}",
        )

    async def _run_loop(
        self,
        *,
        case: Case,
        attempt_number: int,
        stage: CallStage,
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
                    detail=f"stage={stage.value} no inbound within {self._inactivity_timeout}",
                )
                return "inconclusive", None, ""

            normalized = normalize_sms_body(inbound.body)
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

            if stage in _POST_BOOKING_STAGES:
                post_booking = _detect_post_booking_response(normalized)
                if post_booking is not None:
                    await self._send_post_booking_ack(
                        case=case,
                        attempt_number=attempt_number,
                        stage=stage,
                        business_outcome=post_booking,
                    )
                    await self._emit(
                        case=case,
                        attempt_number=attempt_number,
                        event=f"sms.{post_booking}",
                        detail=f"stage={stage.value} customer reply={normalized!r}",
                    )
                    return post_booking, None, ""

            if is_opt_out_keyword(normalized):
                await self._emit(
                    case=case,
                    attempt_number=attempt_number,
                    event="sms.opted_out",
                    detail=f"stop keyword: {normalized}",
                )
                return "opted_out", None, ""

            if stage == CallStage.OUTREACH and normalized in {
                "CANCEL",
                "CANCELLED",
                "CANCELED",
            }:
                await self._emit(
                    case=case,
                    attempt_number=attempt_number,
                    event="sms.declined",
                    detail=f"outreach cancel: {normalized}",
                )
                return "declined", None, ""

            # The SMS opener presents offered slots as a numbered list, with
            # the last index being "None of those work". The customer's digit
            # IS the booking decision; Kate's reply is just a verbal
            # acknowledgement. Decide the outcome here and let the LLM produce
            # the closing message in the same turn.
            outreach_digit_outcome: BusinessOutcome | None = None
            outreach_digit_slot: SlotId | None = None
            if stage == CallStage.OUTREACH:
                kind, picked = _interpret_digit_selection(
                    inbound.body, case=case
                )
                if kind == "book":
                    outreach_digit_outcome = "booked"
                    outreach_digit_slot = picked
                elif kind == "decline":
                    outreach_digit_outcome = "declined"

            # One assistant turn.
            try:
                reply = await asyncio.to_thread(self._take_turn, case, stage)
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
                    self._twilio_send,
                    case_id=case.case_id,
                    to=case.customer.phone,
                    body=reply,
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

            if outreach_digit_outcome == "booked":
                await self._emit(
                    case=case,
                    attempt_number=attempt_number,
                    event="sms.booked",
                    detail=f"slot={outreach_digit_slot}",
                )
                return "booked", outreach_digit_slot, ""
            if outreach_digit_outcome == "declined":
                await self._emit(
                    case=case,
                    attempt_number=attempt_number,
                    event="sms.declined",
                    detail="customer picked none-of-those-work option",
                )
                return "declined", None, ""

    async def _send_post_booking_ack(
        self,
        *,
        case: Case,
        attempt_number: int,
        stage: CallStage,
        business_outcome: BusinessOutcome,
    ) -> None:
        """One LLM turn + Twilio send after the customer picks a post-booking option.

        Kate's acknowledgment goes out before we return the classified
        ``business_outcome`` on the ``CallOutcome``. State transitions
        are driven by that outcome via ``CallEnded``, not by prompt text.
        """
        try:
            reply = await asyncio.to_thread(self._take_turn, case, stage)
        except Exception as exc:
            _log.warning(
                "sms.post_booking.ack_skipped",
                case_id=case.case_id,
                stage=stage.value,
                business_outcome=business_outcome,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        try:
            sid = await asyncio.to_thread(
                self._twilio_send,
                case_id=case.case_id,
                to=case.customer.phone,
                body=reply,
            )
        except Exception as exc:
            _log.warning(
                "sms.post_booking.send_failed",
                case_id=case.case_id,
                stage=stage.value,
                business_outcome=business_outcome,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
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
            detail=_summarize(
                reply,
                prefix=f"stage={stage.value} ack={business_outcome} sid={sid}",
            ),
        )

    def _take_turn(self, case: Case, stage: CallStage) -> str:
        """Compose system prompt + history, call the LLM, return the reply.

        Synchronous because the existing ``LlmComplete`` callable is
        sync (LiteLLM under the hood). We wrap in ``to_thread`` from
        the caller so the event loop isn't blocked.

        ``stage`` selects the outreach vs post-booking system prompt
        via ``prompt_composer.build_prompt`` (Phase 7). One prompt is
        bound for the entire conversation attempt.
        """
        # Re-read the case from the repository on every turn so the prompt
        # always reflects the current persisted state (opt-status changes,
        # signals, etc.) rather than the snapshot captured at fire time.
        current_case = self._case_repo.get(case.case_id)
        rendered = build_prompt(
            case=current_case,
            channel=Channel.SMS,
            stage=PromptStage(stage.value),
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


def _detect_post_booking_response(normalized: str) -> BusinessOutcome | None:
    """Classify a customer reply during ``INITIAL_REMINDER`` /
    ``FINAL_REMINDER``.

    Returns a Decided ``business_outcome`` when the inbound text
    clearly matches one of the three post-booking options. Returns
    ``None`` when ambiguous so the conversation can continue.
    """
    if normalized in _POST_BOOKING_CONFIRMED:
        return "confirmed"
    if normalized in _POST_BOOKING_RESCHEDULE:
        return "rescheduled"
    if normalized in _POST_BOOKING_CANCEL:
        return "cancelled"
    return None


_DigitOutcome = tuple[str, SlotId | None]  # ("book"|"decline"|"none", slot)


def _interpret_digit_selection(text: str, *, case: Case) -> _DigitOutcome:
    """Map a customer's digit-only reply to a booking decision.

    The SMS opener presents offered slots numbered ``1..N`` with
    ``N+1 = "None of those work"``. A plain-digit reply IS the
    customer's decision; the LLM's prose acknowledgement of it
    does not need to be parsed.

    - ``"1".."N"`` → ``("book", SlotId)`` for that index.
    - ``"N+1"``  → ``("decline", None)`` (none of those work).
    - anything else → ``("none", None)`` (let the conversation continue).
    """
    cleaned = text.strip()
    if not cleaned.isdigit():
        return ("none", None)
    idx = int(cleaned) - 1
    n = len(case.offered_slots)
    if 0 <= idx < n:
        return ("book", case.offered_slots[idx].id)
    if idx == n:
        return ("decline", None)
    return ("none", None)


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
    "build_sms_call_manager",
    "build_sms_call_session",
]
