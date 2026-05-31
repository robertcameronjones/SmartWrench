"""Build the SMS dispatcher (and routing store reference) from env + disk.

Composes the three things the SMS path needs at runtime:

1. :class:`SmsMessageComposer` — the LLM-driven message author. One
   instance per process, stateless across calls.
2. :class:`LiveSmsDispatcher` — wraps the composer + the supplied
   queued sender so the :class:`CaseDriver` can dispatch one outbound
   SMS reply at a time through the existing
   :class:`guidepoint.case.SmsDispatcher` Protocol.
3. The :class:`RoutingStore` — returned alongside so the inbound
   webhook handler can translate ``phone -> case_id`` for inbound
   texts before handing them to the driver.

Returns ``(None, None)`` if no queued sender was supplied (SMS
disabled). The simulator boots fine for voice-only operators; the
Fire route surfaces a 503 if the operator picks ``channel=sms`` in
that state.

The queued :class:`TwilioSend` is constructed in
:mod:`simulator._outbound_setup` and passed in here. This module
never reaches Twilio directly — keeping the live Twilio client and
the outbound queue worker out of this file makes the SMS dispatcher
test setup small (fake LLM + fake queue + in-memory routing).

Required env vars for SMS (already present in ``sms/.env`` +
``llm/.env`` from the existing standalone tools):

- ``OPENROUTER_API_KEY``  (or whatever provider env LiteLLM needs
                           for the chosen model)

Optional:

- ``LLM_MODEL``      LiteLLM model string. Defaults to
                     ``openrouter/openai/gpt-oss-20b:free`` (the model
                     the operator already verified works).
- ``SMS_DATA_DIR``   Defaults to ``<project_root>/data/sms``.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

from guidepoint.case import CaseEvent, CaseRepository
from guidepoint.clock import Clock
from guidepoint.events import EventBus
from prompt_composer import PromptPaths
from sms_adapter import (
    LiveSmsDispatcher,
    RoutingStore,
    TwilioSend,
    build_json_history_store,
    build_json_routing_store,
    build_litellm_completer,
    build_sms_dispatcher,
    build_sms_message_composer,
)

_log = structlog.get_logger(__name__)


def build_sms_session(
    *,
    project_root: Path,
    case_repo: CaseRepository,
    bus: EventBus[CaseEvent],
    clock: Clock,
    twilio_send: TwilioSend | None,
) -> tuple[LiveSmsDispatcher | None, RoutingStore | None]:
    """Compose the live :class:`LiveSmsDispatcher` + routing store.

    Returns ``(dispatcher, routing)`` when SMS is enabled, or
    ``(None, None)`` when no queued sender was provided. The
    dispatcher satisfies :class:`guidepoint.case.SmsDispatcher` and
    is intended to be passed straight into the :class:`CaseDriver`'s
    ``sms_dispatcher`` constructor argument.

    The routing store is returned alongside so the inbound webhook
    handler can translate ``phone -> case_id`` without reaching into
    the dispatcher's internals; the webhook then calls
    ``CaseDriver.on_inbound_sms`` with the resolved case id.

    Pass ``twilio_send=None`` (or omit at the caller) to disable SMS
    — the function returns ``(None, None)`` and the Fire route 503s
    on channel=sms.
    """
    if twilio_send is None:
        _log.warning("simulator.sms.disabled", reason="no twilio_send supplied")
        return None, None

    # Default to the model the operator already round-tripped via the
    # llm/ chat CLI. Overridable with LLM_MODEL=... .
    model = (os.environ.get("LLM_MODEL") or "openrouter/openai/gpt-oss-20b:free").strip()

    data_dir = Path(os.environ.get("SMS_DATA_DIR") or (project_root / "data" / "sms"))
    history_dir = data_dir / "history"
    routing_path = data_dir / "routing.json"
    event_log_path = data_dir / "sms.log"
    history_dir.mkdir(parents=True, exist_ok=True)
    routing_path.parent.mkdir(parents=True, exist_ok=True)

    workspace_root = project_root.parent
    prompt_paths = PromptPaths(
        system=project_root / "config" / "system-prompt.md",
        post_booking=project_root / "config" / "prompt-post-booking.md",
        voice=project_root / "config" / "voice.md",
        sms=workspace_root / "sms_adapter" / "config" / "sms.md",
    )

    history = build_json_history_store(root=history_dir)
    routing = build_json_routing_store(path=routing_path)
    composer = build_sms_message_composer(
        llm_complete=build_litellm_completer(model=model, event_log_path=event_log_path),
        history=history,
        case_repo=case_repo,
        clock=clock,
        prompt_paths=prompt_paths,
        bus=bus,
        event_log_path=event_log_path,
    )
    dispatcher = build_sms_dispatcher(
        composer=composer,
        sender=twilio_send,
        routing=routing,
        case_repo=case_repo,
    )
    _log.info(
        "simulator.sms.enabled",
        model=model,
        history_dir=str(history_dir),
        routing_path=str(routing_path),
        event_log_path=str(event_log_path),
    )
    return dispatcher, routing


__all__ = ["build_sms_session"]
