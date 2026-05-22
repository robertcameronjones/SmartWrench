"""Compose the FastAPI application.

The factory wires every dependency exactly once. Tests build a custom
app with fakes; ``__main__`` builds the production app from real
adapters. Either way no module reaches for a global.

Per ADR 0006 the simulator hosts the same ``CaseManager`` the
production system will run; the only thing it swaps is the
per-channel ``CallSession`` (live ElevenLabs for voice, the in-process
``SmsCallSession`` for SMS).

The trigger source is the in-memory ``EphemeralTriggerSource`` — the
operator composes a trigger by typing service type + summary in the
UI, the fire route synthesizes the Trigger from saved master data,
hands it to ``CaseManager.start``, and discards it after.

Per-user data namespacing (v2)
==============================
User-scoped state (master data, slots, per-user CaseManager) is no
longer constructed at app startup. A :class:`UserContextRegistry`
lives on ``app.state.user_contexts`` and lazily builds + caches a
per-user :class:`UserContext` on first request, seeding the user's
directory from ``fixtures/`` if missing.

Each user's :class:`CaseManager` is wired with that user's
``master_data`` but reuses the **global** ``case_repo``,
``trigger_source``, and ``call_sessions`` (voice + sms). That way
customer / dealer / vehicle lookups during ``start()`` hit the
user's namespace, while case files land in the shared ``cases/`` dir
where the live ElevenLabs voice callback and the SMS inbound
webhook can find them.

SMS inbound webhook
===================
Twilio POSTs every inbound SMS to ``/sms``. The handler resolves
``phone -> case_id`` via the SMS routing store, then calls
``SmsCallSession.deliver_inbound(case_id=...)``. The session's per-case
``place()`` coroutine dequeues the turn, runs one LLM exchange, and
sends the reply. Inbounds for phones with no active session are
logged and dropped (Twilio still sees a 200 so it doesn't retry).
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
    CaseId,
    CaseRepository,
    Channel,
    JsonCasePaths,
    RetryPolicy,
    TriggerSource,
    build_json_case_repository,
    build_live_call_session,
)
from guidepoint.case._call_session import CallSession
from guidepoint.clock import Clock, build_system_clock
from guidepoint.events import EventBus, build_event_bus
from sms.server import app as sms_webhook_app
from sms.server import inbound_sms, register_inbound_handler
from sms_adapter import RoutingStore, SmsCallSession

from simulator._basic_auth import BasicAuthMiddleware
from simulator._connection import ConnectionProbe, build_env_connection_probe
from simulator._ephemeral_triggers import EphemeralTriggerSource
from simulator._routes import (
    RouteDeps,
    build_router,
    package_static_dir,
    package_templates_dir,
)
from simulator._sms_setup import build_sms_session
from simulator._users import UserContextRegistry, UserRegistry

_log = structlog.get_logger(__name__)


def build_app(
    *,
    project_root: Path,
    clock: Clock | None = None,
    bus: EventBus[CaseEvent] | None = None,
    case_repo: CaseRepository | None = None,
    trigger_source: TriggerSource | None = None,
    call_session: CallSession | None = None,
    sms_session: SmsCallSession | None = None,
    sms_routing: RoutingStore | None = None,
    probe: ConnectionProbe | None = None,
    retry_policy: RetryPolicy | None = None,
    user_registry: UserRegistry | None = None,
) -> FastAPI:
    """Compose the simulator application.

    Most dependencies have defaults constructed from env / disk;
    tests pass fakes for any that need to be deterministic. The
    default ``call_session`` is the live ElevenLabs adapter — there
    is no stub. Tests that don't want to place real calls inject a
    fake via the ``call_session`` parameter.

    ``sms_session`` + ``sms_routing`` go together: pass both for SMS
    testing with fakes, or pass neither to let the factory build the
    live SMS session from env vars (returns ``(None, None)`` when
    Twilio env vars are missing; the Fire route 503s on channel=sms
    in that state).

    ``user_registry`` defaults to one parsed from the ``USERS`` env
    var; tests can pass a fixed allowlist.
    """
    resolved_clock = clock or build_system_clock()
    resolved_bus: EventBus[CaseEvent] = bus or build_event_bus(payload_type=CaseEvent)
    resolved_probe = probe or build_env_connection_probe(clock=resolved_clock)
    resolved_user_registry = user_registry or UserRegistry(os.environ.get("USERS") or "")
    resolved_case_repo = case_repo or build_json_case_repository(
        paths=JsonCasePaths.for_root(project_root),
    )
    resolved_trigger_source: TriggerSource = trigger_source or EphemeralTriggerSource()
    resolved_call_session = call_session or _build_live_call_session_from_env(
        case_repo=resolved_case_repo,
        bus=resolved_bus,
        clock=resolved_clock,
    )

    # SMS is optional. If the caller didn't pass an explicit session +
    # routing pair, try to build the live one from env vars. The
    # factory returns ``(None, None)`` when required env vars are
    # missing; the Fire route 503s on channel=sms in that state.
    if sms_session is None and sms_routing is None:
        sms_session, sms_routing = build_sms_session(
            project_root=project_root,
            case_repo=resolved_case_repo,
            bus=resolved_bus,
            clock=resolved_clock,
        )

    call_sessions: dict[Channel, CallSession] = {"voice": resolved_call_session}
    if sms_session is not None:
        call_sessions["sms"] = sms_session

    templates = Jinja2Templates(directory=str(package_templates_dir()))
    deps = RouteDeps(
        case_repo=resolved_case_repo,
        trigger_source=resolved_trigger_source,
        bus=resolved_bus,
        probe=resolved_probe,
        clock=resolved_clock,
        templates=templates,
        enabled_channels=frozenset(call_sessions.keys()),
    )

    user_contexts = UserContextRegistry(
        project_root=project_root,
        user_registry=resolved_user_registry,
        case_repo=resolved_case_repo,
        trigger_source=resolved_trigger_source,
        call_sessions=call_sessions,
        bus=resolved_bus,
        clock=resolved_clock,
        retry_policy=retry_policy,
    )

    app = FastAPI(
        title="Guidepoint Simulator",
        version="0.5.0",
        docs_url="/docs",
        redoc_url=None,
    )
    # Stash the per-user registry on app.state so the FastAPI dependency
    # get_user_context can find it from any request. Allowed-users
    # registry tags along for future UIs (user picker, etc.).
    app.state.user_contexts = user_contexts
    app.state.user_registry = resolved_user_registry

    # Browser-facing HTTP Basic Auth. Auth is required whenever USERS
    # is set (default: "demo:demo" for local dev). Exempts /sms and
    # /twilio/* so Twilio webhooks still work without credentials.
    app.add_middleware(BasicAuthMiddleware)
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

    if sms_session is not None and sms_routing is not None:
        register_inbound_handler(
            _make_inbound_handler(session=sms_session, routing=sms_routing)
        )
        _log.info("simulator.sms.handler.registered")
    else:
        _log.warning(
            "simulator.sms.handler.not_registered",
            reason="sms_session not configured (missing env vars?)",
        )

    _log.info(
        "simulator.users.configured",
        allowed=", ".join(resolved_user_registry.list_ids()),
    )

    return app


def _make_inbound_handler(
    *,
    session: SmsCallSession,
    routing: RoutingStore,
):
    """Build the coroutine sms.server calls for every inbound SMS.

    Looks up ``phone -> case_id`` from the routing store, then asks
    the SMS session to enqueue the turn onto the active conversation.
    Errors are logged and swallowed so the webhook still returns 200
    to Twilio (Twilio retries on non-200s, which would compound a
    transient failure).
    """

    async def _handler(*, from_number: str, to_number: str, body: str, message_sid: str) -> None:
        entry = routing.find_entry(from_number)
        if entry is None:
            _log.warning(
                "simulator.sms.inbound.unknown_phone",
                phone=from_number,
                body=body[:80],
            )
            return
        try:
            queued = await session.deliver_inbound(
                case_id=CaseId(entry.conversation_id),
                from_number=from_number,
                body=body,
                message_sid=message_sid,
            )
        except Exception as exc:
            _log.error(
                "simulator.sms.inbound.failed",
                phone=from_number,
                case_id=entry.conversation_id,
                user_id=entry.user_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        if not queued:
            # No active session for this case_id. Common after a
            # process restart or once a case has reached terminal
            # state and the customer keeps texting. Log + drop.
            _log.info(
                "simulator.sms.inbound.no_active_session",
                phone=from_number,
                case_id=entry.conversation_id,
                user_id=entry.user_id,
                body=body[:80],
            )
            return
        _log.info(
            "simulator.sms.inbound.queued",
            phone=from_number,
            case_id=entry.conversation_id,
            user_id=entry.user_id,
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
