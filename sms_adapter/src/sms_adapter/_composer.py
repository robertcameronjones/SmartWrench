"""SMS message composer — turn a case + stage into one assistant reply.

The composer is the SMS-channel "what should Kate say next" function.
It is the only place the LLM is invoked for an SMS reply. The driver
calls :meth:`SmsMessageComposer.compose` whenever the reducer emits a
:class:`PlaceCall` action for an SMS case (opener, slot list, ack,
free-text answer, initial reminder, final reminder, …); the composer
loads the current case from the repository, renders the stage-aware
system prompt via :mod:`prompt_composer`, loads the SMS message
history, and asks the LLM for one assistant turn.

What the composer does **not** do:

- Send anything. It returns the body string only — the driver does
  the Twilio send via the queued sender.
- Persist the assistant turn. The driver appends to the history store
  after the send succeeds, so a failed send doesn't leave an orphan
  assistant message in the transcript.
- Classify inbound replies. That lives in the reducer
  (``_on_inbound_sms_received``); the composer only produces text.
- Hold per-case state. The composer is one instance per process and is
  fully stateless across calls — every invocation re-reads the case
  and history fresh.

Boundary
========
The composer lives in :mod:`sms_adapter` because it depends on the
adapter-level :class:`LlmComplete` callable, the SMS history store,
and the SMS-channel prompt template paths. It is invoked from the
:mod:`guidepoint.case` driver but does not import driver internals;
the call shape is a single ``compose(case_id, stage)`` method.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import final

import structlog

from guidepoint.case import (
    CallStage,
    CaseEvent,
    CaseId,
    CaseRepository,
)
from guidepoint.clock import Clock
from guidepoint.events import EventBus
from prompt_composer import Channel, PromptPaths, PromptStage, build_prompt

from sms_adapter import HistoryStore, LlmComplete, Turn, TurnRole
from sms_adapter._event_log import log_event

_CaseEventBus = EventBus[CaseEvent]

_log = structlog.get_logger(__name__)


@final
@dataclass(frozen=True, slots=True)
class ComposedMessage:
    """The output of one :meth:`SmsMessageComposer.compose` call.

    Just the text body. Carried as a small dataclass rather than a bare
    string so future fields (token usage, prompt id, etc.) can be added
    without changing the call signature.
    """

    body: str


@final
class SmsMessageComposer:
    """Stateless LLM driver for outbound SMS replies.

    One instance per process. Inject the :class:`LlmComplete`,
    :class:`HistoryStore`, :class:`CaseRepository`, :class:`Clock`,
    and the :class:`PromptPaths` bundle the SMS spot prompts live
    under. The simulator wires this up alongside the outbound queue
    sender.
    """

    def __init__(
        self,
        *,
        llm_complete: LlmComplete,
        history: HistoryStore,
        case_repo: CaseRepository,
        clock: Clock,
        prompt_paths: PromptPaths,
        bus: _CaseEventBus,
        event_log_path: Path | None = None,
    ) -> None:
        self._llm_complete = llm_complete
        self._history = history
        self._case_repo = case_repo
        self._clock = clock
        self._prompt_paths = prompt_paths
        self._bus = bus
        self._event_log_path = event_log_path

    # -- Public API ---------------------------------------------------------

    def compose(self, *, case_id: CaseId, stage: CallStage) -> ComposedMessage:
        """Compose one assistant reply for ``case_id`` at ``stage``.

        Synchronous; the underlying LLM call (LiteLLM) is sync today.
        The driver wraps the call in ``asyncio.to_thread`` so the
        event loop stays free during the round-trip.

        Behaviour:

        - The case is re-read from the repository so the prompt reflects
          the current persisted state, not a stale snapshot.
        - The system prompt is selected by ``stage`` via
          :func:`prompt_composer.build_prompt` (outreach vs post-booking
          vs etc.).
        - The full SMS history for the case is loaded and passed to the
          LLM. When the history is empty (very first turn of the
          conversation), a synthetic ``(open conversation)`` user turn
          is injected so chat models that require at least one user
          message will proceed with the opener.
        """

        case = self._case_repo.get(case_id)
        rendered = build_prompt(
            case=case,
            channel=Channel.SMS,
            stage=PromptStage(stage.value),
            paths=self._prompt_paths,
        )
        history = self._history.load(case_id)
        if not history:
            history = (
                Turn(
                    role=TurnRole.USER,
                    text="(open conversation)",
                    timestamp=self._clock.now(),
                    twilio_sid="",
                ),
            )
        body = self._llm_complete(system=rendered.text, history=history)
        return ComposedMessage(body=body)

    # -- Helpers for the driver --------------------------------------------

    def record_outbound(
        self,
        *,
        case_id: CaseId,
        body: str,
        twilio_sid: str,
        to_phone: str,
    ) -> None:
        """Append the assistant turn to history and tail the SMS event log.

        Called by the driver after :meth:`compose` and after the
        outbound sender returns an ``item_id`` / twilio sid. Splitting
        this from :meth:`compose` keeps the LLM call free of side
        effects so a failed send doesn't leave a phantom assistant
        turn in the transcript.
        """

        self._history.append(
            case_id,
            Turn(
                role=TurnRole.ASSISTANT,
                text=body,
                timestamp=self._clock.now(),
                twilio_sid=twilio_sid,
            ),
        )
        log_event(
            self._event_log_path,
            f"us->sms to={to_phone} conv={case_id} sid={twilio_sid}",
            body,
        )

    def record_inbound(
        self,
        *,
        case_id: CaseId,
        from_phone: str,
        body: str,
        message_sid: str,
    ) -> None:
        """Append the customer turn to history and tail the SMS event log.

        Called by the driver as soon as the webhook delivers an inbound
        message, before the reducer is asked to classify it. Doing the
        append here keeps the LLM call (on whichever reply turn the
        reducer eventually triggers) consistent with the conversation
        the customer actually saw.
        """

        self._history.append(
            case_id,
            Turn(
                role=TurnRole.USER,
                text=body,
                timestamp=self._clock.now(),
                twilio_sid=message_sid,
            ),
        )
        log_event(
            self._event_log_path,
            f"sms->us from={from_phone} conv={case_id} sid={message_sid}",
            body,
        )

    async def emit_event(
        self,
        *,
        case_id: CaseId,
        event: str,
        detail: str,
        level: str = "info",
    ) -> None:
        """Append a :class:`CaseEvent` to the case audit log + publish.

        Used by the driver to keep the SMS audit trail (``sms.opened``,
        ``sms.outbound``, ``sms.inbound``) recorded against the same
        ``CaseEvent`` stream the reducer uses for ``RecordEvent``
        actions, without forcing the driver to know about
        :class:`CaseEvent` plumbing.
        """

        try:
            case = self._case_repo.get(case_id)
        except Exception:
            _log.warning("sms_composer.emit_event.case_missing", case_id=case_id)
            return
        evt = CaseEvent(
            event_id=f"evt_{secrets.token_hex(6)}",
            case_id=case_id,
            correlation_id=case.correlation_id,
            attempt_number=case.attempt_count or None,
            timestamp=self._clock.now(),
            source="sms",
            level=level,  # type: ignore[arg-type]
            event=event,
            detail=detail,
        )
        self._case_repo.append_event(case_id, evt)
        await self._bus.publish(evt)


def build_sms_message_composer(
    *,
    llm_complete: LlmComplete,
    history: HistoryStore,
    case_repo: CaseRepository,
    clock: Clock,
    prompt_paths: PromptPaths,
    bus: _CaseEventBus,
    event_log_path: Path | None = None,
) -> SmsMessageComposer:
    """Construct the process-wide :class:`SmsMessageComposer`."""

    return SmsMessageComposer(
        llm_complete=llm_complete,
        history=history,
        case_repo=case_repo,
        clock=clock,
        prompt_paths=prompt_paths,
        bus=bus,
        event_log_path=event_log_path,
    )


__all__ = [
    "ComposedMessage",
    "SmsMessageComposer",
    "build_sms_message_composer",
]
