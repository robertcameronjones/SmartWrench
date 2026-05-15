"""Compose the FastAPI application.

The factory wires every dependency exactly once. Tests build a custom
app with fakes; ``__main__`` builds the production app from real
adapters. Either way no module reaches for a global.

Per ADR 0006 the simulator hosts the same ``CaseManager`` the
production system will run; the only thing it swaps is the
``CallSession`` (live ElevenLabs adapter is the only implementation).

The trigger source is the in-memory ``EphemeralTriggerSource`` — the
operator composes a trigger by typing service type + summary in the
UI, the fire route synthesizes the Trigger from saved master data,
hands it to ``CaseManager``, and discards it after.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog
from elevenlabs.client import ElevenLabs
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from guidepoint.case import (
    CaseEvent,
    CaseManager,
    CaseRepository,
    JsonCasePaths,
    RetryPolicy,
    TriggerSource,
    build_default_case_manager,
    build_json_case_repository,
    build_live_call_session,
)
from guidepoint.case._call_session import CallSession
from guidepoint.clock import Clock, build_system_clock
from guidepoint.events import EventBus, build_event_bus
from guidepoint.master_data import (
    JsonFilePaths,
    MasterDataRepository,
    build_json_master_data_repository,
)
from sms.server import app as sms_webhook_app
from sms.server import inbound_sms, register_inbound_handler
from sms_adapter import (
    InboundForUnknownPhoneError,
    SmsDeps,
    handle_inbound,
)

from simulator._connection import ConnectionProbe, build_env_connection_probe
from simulator._ephemeral_triggers import EphemeralTriggerSource
from simulator._routes import (
    RouteDeps,
    build_router,
    package_static_dir,
    package_templates_dir,
)
from simulator._slots import SlotsRepository, build_slots_repository
from simulator._sms_context_registry import SmsContextRegistry
from simulator._sms_setup import build_sms_deps

_log = structlog.get_logger(__name__)


def build_app(
    *,
    project_root: Path,
    clock: Clock | None = None,
    bus: EventBus[CaseEvent] | None = None,
    master_data: MasterDataRepository | None = None,
    case_repo: CaseRepository | None = None,
    trigger_source: TriggerSource | None = None,
    case_manager: CaseManager | None = None,
    call_session: CallSession | None = None,
    probe: ConnectionProbe | None = None,
    slots_repo: SlotsRepository | None = None,
    retry_policy: RetryPolicy | None = None,
    sms_deps: SmsDeps | None = None,
    sms_contexts: SmsContextRegistry | None = None,
) -> FastAPI:
    """Compose the simulator application.

    Each dependency has a default constructed from JSON-fixture paths
    under ``project_root``; tests pass fakes for any that need to be
    deterministic. The default ``call_session`` is the live ElevenLabs
    adapter — there is no stub. Tests that don't want to place real
    calls inject a fake via the ``call_session`` parameter.
    """
    resolved_clock = clock or build_system_clock()
    resolved_bus: EventBus[CaseEvent] = bus or build_event_bus(payload_type=CaseEvent)
    resolved_master_data = master_data or build_json_master_data_repository(
        paths=JsonFilePaths.for_root(project_root),
    )
    resolved_case_repo = case_repo or build_json_case_repository(
        paths=JsonCasePaths.for_root(project_root),
    )
    resolved_slots_repo = slots_repo or build_slots_repository(project_root=project_root)
    resolved_trigger_source: TriggerSource = trigger_source or EphemeralTriggerSource()
    resolved_probe = probe or build_env_connection_probe(clock=resolved_clock)
    resolved_call_session = call_session or _build_live_call_session_from_env(
        case_repo=resolved_case_repo,
        bus=resolved_bus,
        clock=resolved_clock,
    )
    resolved_case_manager = case_manager or build_default_case_manager(
        master_data=resolved_master_data,
        case_repo=resolved_case_repo,
        trigger_source=resolved_trigger_source,
        call_session=resolved_call_session,
        bus=resolved_bus,
        clock=resolved_clock,
        retry_policy=retry_policy,
    )

    # SMS dispatch is optional. When env vars are missing, the factory
    # returns (None, None) and the Fire route 503s on channel=sms.
    if sms_deps is None and sms_contexts is None:
        sms_deps, sms_contexts = build_sms_deps(project_root=project_root)

    templates = Jinja2Templates(directory=str(package_templates_dir()))
    deps = RouteDeps(
        master_data=resolved_master_data,
        slots_repo=resolved_slots_repo,
        case_repo=resolved_case_repo,
        trigger_source=resolved_trigger_source,
        case_manager=resolved_case_manager,
        bus=resolved_bus,
        probe=resolved_probe,
        clock=resolved_clock,
        templates=templates,
        sms_deps=sms_deps,
        sms_contexts=sms_contexts,
    )

    app = FastAPI(
        title="Guidepoint Simulator",
        version="0.3.0",
        docs_url="/docs",
        redoc_url=None,
    )
    app.include_router(build_router(deps=deps))
    app.mount(
        "/static",
        StaticFiles(directory=str(package_static_dir())),
        name="static",
    )

    # Mount the webhook's debug pages at /twilio (so /twilio/messages
    # and /twilio/health stay reachable for inspection) and ALSO expose
    # the inbound handler at bare /sms — that's the path Twilio's
    # console is configured for. Root-mounting the webhook app would
    # shadow the simulator's / and /health, so we register the inbound
    # endpoint directly here.
    app.mount("/twilio", sms_webhook_app, name="twilio")
    app.add_api_route("/sms", inbound_sms, methods=["POST"], name="twilio-inbound")

    if sms_deps is not None and sms_contexts is not None:
        register_inbound_handler(
            _make_inbound_handler(deps=sms_deps, contexts=sms_contexts)
        )
        _log.info("simulator.sms.handler.registered")
    else:
        _log.warning(
            "simulator.sms.handler.not_registered",
            reason="sms_deps not configured (missing env vars?)",
        )

    return app


def _make_inbound_handler(
    *,
    deps: SmsDeps,
    contexts: SmsContextRegistry,
):
    """Build the coroutine sms.server calls for every inbound SMS.

    Closes over the SMS deps + context registry so the webhook itself
    stays a dumb pipe. Errors here are logged and swallowed so the
    webhook still returns 200 to Twilio (Twilio retries on non-200s,
    which would compound a transient failure).
    """

    async def _handler(*, from_number: str, to_number: str, body: str, message_sid: str) -> None:
        try:
            await handle_inbound(
                from_number=from_number,
                body=body,
                deps=deps,
                context_lookup=contexts,
                message_sid=message_sid,
                to_number=to_number,
            )
        except InboundForUnknownPhoneError:
            _log.warning(
                "simulator.sms.inbound.unknown_phone",
                phone=from_number,
                body=body[:80],
            )
        except Exception as exc:
            _log.error(
                "simulator.sms.inbound.failed",
                phone=from_number,
                error=f"{type(exc).__name__}: {exc}",
            )

    return _handler


def _build_live_call_session_from_env(
    *,
    case_repo: CaseRepository,
    bus: EventBus[CaseEvent],
    clock: Clock,
) -> CallSession:
    """Construct the live ElevenLabs ``CallSession`` from environment vars.

    Reads ``ELEVENLABS_API_KEY``, ``ELEVENLABS_AGENT_ID``, and
    ``ELEVENLABS_AGENT_PHONE_NUMBER_ID`` from the loaded environment
    (``.env`` is loaded by ``__main__`` before ``build_app`` runs).
    Fails fast on missing values via ``build_live_call_session``.
    """
    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    agent_id = (os.environ.get("ELEVENLABS_AGENT_ID") or "").strip()
    phone_number_id = (os.environ.get("ELEVENLABS_AGENT_PHONE_NUMBER_ID") or "").strip()
    return build_live_call_session(
        client=ElevenLabs(api_key=api_key),
        agent_id=agent_id,
        phone_number_id=phone_number_id,
        case_repo=case_repo,
        bus=bus,
        clock=clock,
    )


__all__ = ["build_app"]
