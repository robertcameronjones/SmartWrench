"""Compose the FastAPI application.

The factory wires every dependency exactly once. Tests build a custom
app with fakes; ``__main__`` builds the production app from real
adapters. Either way no module reaches for a global.

Per-user data namespacing
=========================
A :class:`UserContextRegistry` lives on ``app.state.user_contexts`` and
lazily builds + caches a per-user :class:`UserContext` on first request,
seeding the user's directory from ``fixtures/`` if missing. The context
owns only master data (customer / dealer / vehicle) and slots. Case
execution is handled by the global ``CaseDriver`` on ``app.state``.

SMS inbound webhook
===================
Twilio POSTs every inbound SMS to ``/sms``. The handler resolves
``phone -> case_id`` via the SMS routing store, then calls
``CaseDriver.on_inbound_sms(...)``. The driver records the customer
turn (via the SMS dispatcher) and republishes the body as an
``InboundSmsReceived`` signal; the reducer decides what happens next
(digit pick → state move, free text → new ``PlaceCall`` for an
LLM-composed reply).

STOP / START keywords are handled even when no case is active:
consent is updated in master data and ``CustomerOptedOut`` /
``CustomerOptedIn`` signals fan out to active cases via ``CaseDriver``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from elevenlabs.client import ElevenLabs
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from guidepoint.case import (
    CaseDriver,
    CaseEvent,
    CaseId,
    CaseRepository,
    Channel,
    CustomerOptedIn,
    CustomerOptedOut,
    InMemoryTimerService,
    TriggerSource,
    build_live_call_session,
)
from guidepoint.case._call_session import CallSession
from guidepoint.case._ports import CallManager
from guidepoint.case._world_bridge import timer_name_to_case_signal
from guidepoint.clock import Clock, build_system_clock
from guidepoint.events import EventBus, build_event_bus
from guidepoint.persistence import build_case_repository
from sms.server import app as sms_webhook_app
from sms.server import inbound_sms, register_inbound_handler
from sms_adapter import (
    LiveSmsDispatcher,
    RoutingStore,
    is_opt_in_keyword,
    is_opt_out_keyword,
    normalize_sms_body,
)

from simulator._basic_auth import BasicAuthMiddleware
from simulator._connection import ConnectionProbe, build_env_connection_probe
from simulator._consent import ProjectSmsConsentChecker
from simulator._ephemeral_triggers import EphemeralTriggerSource
from simulator._outbound_setup import OutboundBundle, build_outbound_dispatch
from simulator._routes import (
    RouteDeps,
    build_router,
    package_static_dir,
    package_templates_dir,
)
from simulator._sim_ports import SimulatorDealerSlotPort, SimulatorGeofencePort
from simulator._sim_controls import (
    QueueHealthResponse,
    SimulatorWorldState,
    WorldStateResponse,
    build_geofence_forwarder,
    make_get_queue_health,
    make_get_world_state,
    make_post_case_signal,
    make_put_business_hours,
    make_put_geofence,
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
    sms_dispatcher: LiveSmsDispatcher | None = None,
    sms_routing: RoutingStore | None = None,
    probe: ConnectionProbe | None = None,
    user_registry: UserRegistry | None = None,
) -> FastAPI:
    """Compose the simulator application.

    Most dependencies have defaults constructed from env / disk;
    tests pass fakes for any that need to be deterministic. The
    default ``call_session`` is the live ElevenLabs adapter — there
    is no stub. Tests that don't want to place real calls inject a
    fake via the ``call_session`` parameter.

    ``sms_dispatcher`` + ``sms_routing`` go together: pass both for SMS
    testing with fakes, or pass neither to let the factory build the
    live SMS dispatcher from env vars (returns ``(None, None)`` when
    Twilio env vars are missing; the Fire route 503s on channel=sms
    in that state).

    ``user_registry`` defaults to one parsed from the ``USERS`` env
    var; tests can pass a fixed allowlist.

    Case persistence defaults to JSON files; set ``PERSISTENCE=sqlite``
    to use ``data/guidepoint.db`` instead (JSON cases are migrated once
    on first boot).
    """
    resolved_clock = clock or build_system_clock()
    resolved_bus: EventBus[CaseEvent] = bus or build_event_bus(payload_type=CaseEvent)
    resolved_probe = probe or build_env_connection_probe(clock=resolved_clock)
    resolved_user_registry = user_registry or UserRegistry(os.environ.get("USERS") or "")
    resolved_case_repo = case_repo or build_case_repository(project_root=project_root)
    resolved_trigger_source: TriggerSource = trigger_source or EphemeralTriggerSource()
    resolved_call_session = call_session or _build_live_call_session_from_env(
        case_repo=resolved_case_repo,
        bus=resolved_bus,
        clock=resolved_clock,
    )

    enabled_channels: set[Channel] = {"voice"}

    sms_consent_checker = ProjectSmsConsentChecker(
        project_root=project_root,
        user_registry=resolved_user_registry,
    )

    user_contexts = UserContextRegistry(
        project_root=project_root,
        user_registry=resolved_user_registry,
    )

    world_state = SimulatorWorldState()

    # Outbound dispatch — SQLite queue + drainer worker. Lives one
    # layer below the SMS dispatcher: the queue holds items, the
    # worker applies consent + business-hours gates and calls Twilio,
    # the queued sender (consumed by the dispatcher built in
    # build_sms_session) enqueues and returns immediately. Tests that
    # inject ``sms_dispatcher`` directly skip this whole pipeline.
    outbound_bundle: OutboundBundle | None = None
    if sms_dispatcher is None and sms_routing is None:
        outbound_bundle = build_outbound_dispatch(
            project_root=project_root,
            clock=resolved_clock,
            consent=sms_consent_checker,
            hours=world_state,
        )
        if outbound_bundle is not None:
            sms_dispatcher, sms_routing = build_sms_session(
                project_root=project_root,
                case_repo=resolved_case_repo,
                bus=resolved_bus,
                clock=resolved_clock,
                twilio_send=outbound_bundle.sender,
            )

    # SMS is no longer a CallManager — it's a turn-by-turn dispatcher
    # passed to the driver separately. Voice is still the v1
    # CallManager path until/unless that gets refactored too.
    if sms_dispatcher is not None:
        enabled_channels.add("sms")

    dealer_port = SimulatorDealerSlotPort()
    geofence_port = SimulatorGeofencePort()

    driver_holder: dict[str, CaseDriver | None] = {"driver": None}

    async def _timer_fire(case_id: CaseId, name: str) -> None:
        driver = driver_holder["driver"]
        if driver is None:
            return
        await driver.on_signal(
            timer_name_to_case_signal(
                case_id=case_id,
                name=name,
                timestamp=resolved_clock.now(),
            )
        )

    timer_service = InMemoryTimerService(clock=resolved_clock, fire=_timer_fire)

    call_managers: dict[Channel, CallManager] = {"voice": resolved_call_session}

    case_driver = CaseDriver(
        case_repo=resolved_case_repo,
        call_managers=call_managers,
        sms_dispatcher=sms_dispatcher,
        dealer_port=dealer_port,
        timer_service=timer_service,
        bus=resolved_bus,
        clock=resolved_clock,
    )
    driver_holder["driver"] = case_driver

    # Now that the driver exists, wire the worker's "I sent it"
    # callback back into the driver's signal queue. The reducer
    # records the moment via RecordEvent and does not change state;
    # this is purely the audit join between the queued `item_id`
    # carried on the assistant turn and the real Twilio MessageSid.
    if outbound_bundle is not None:
        outbound_bundle.worker.set_on_dispatched(case_driver.on_signal)

    geofence_forwarder = build_geofence_forwarder(
        case_driver=case_driver,
        clock=resolved_clock,
    )
    geofence_subscribed: set = set()

    templates = Jinja2Templates(directory=str(package_templates_dir()))
    deps = RouteDeps(
        case_repo=resolved_case_repo,
        trigger_source=resolved_trigger_source,
        bus=resolved_bus,
        probe=resolved_probe,
        clock=resolved_clock,
        templates=templates,
        enabled_channels=frozenset(enabled_channels),
        sms_consent_checker=sms_consent_checker,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        count = await case_driver.recover_in_flight()
        _log.info("simulator.startup.recovered", count=count)
        if outbound_bundle is not None:
            await outbound_bundle.worker.start()
            _log.info("simulator.outbound.worker.started")
        try:
            yield
        finally:
            if outbound_bundle is not None:
                await outbound_bundle.worker.stop()
                _log.info("simulator.outbound.worker.stopped")
            await case_driver.shutdown()

    app = FastAPI(
        title="Guidepoint Simulator",
        version="0.5.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.user_contexts = user_contexts
    app.state.user_registry = resolved_user_registry
    app.state.dealer_port = dealer_port
    app.state.geofence_port = geofence_port
    app.state.case_driver = case_driver
    app.state.case_repo = resolved_case_repo
    app.state.event_bus = resolved_bus
    app.state.world_state = world_state
    app.state.geofence_forwarder = geofence_forwarder
    app.state.geofence_subscribed = geofence_subscribed
    app.state.active_vehicle_vin = ""
    app.state.outbound_bundle = outbound_bundle
    app.state.outbound_queue = (
        outbound_bundle.queue if outbound_bundle is not None else None
    )

    app.add_middleware(BasicAuthMiddleware)
    router = build_router(deps=deps)
    router.add_api_route(
        "/api/world/state",
        make_get_world_state(world=world_state, geofence_port=geofence_port),
        response_model=WorldStateResponse,
    )
    router.add_api_route(
        "/api/world/business-hours",
        make_put_business_hours(
            world=world_state,
            case_driver=case_driver,
            clock=resolved_clock,
        ),
        methods=["PUT"],
        response_model=WorldStateResponse,
    )
    router.add_api_route(
        "/api/world/geofence",
        make_put_geofence(
            geofence_port=geofence_port,
            on_event=geofence_forwarder,
            subscribed=geofence_subscribed,
        ),
        methods=["PUT"],
        response_model=WorldStateResponse,
    )
    router.add_api_route(
        "/api/cases/{case_id}/signal",
        make_post_case_signal(
            case_repo=resolved_case_repo,
            case_driver=case_driver,
            clock=resolved_clock,
        ),
        methods=["POST"],
    )
    router.add_api_route(
        "/health/queues",
        make_get_queue_health(case_driver=case_driver),
        response_model=QueueHealthResponse,
    )
    app.include_router(router)
    app.mount(
        "/static",
        StaticFiles(directory=str(package_static_dir())),
        name="static",
    )

    app.mount("/twilio", sms_webhook_app, name="twilio")
    app.add_api_route("/sms", inbound_sms, methods=["POST"], name="twilio-inbound")

    if sms_dispatcher is not None and sms_routing is not None:
        register_inbound_handler(
            _make_inbound_handler(
                routing=sms_routing,
                case_driver=case_driver,
                user_contexts=user_contexts,
                clock=resolved_clock,
            )
        )
        _log.info("simulator.sms.handler.registered")
    else:
        _log.warning(
            "simulator.sms.handler.not_registered",
            reason="sms_dispatcher not configured (missing env vars?)",
        )

    _log.info(
        "simulator.users.configured",
        allowed=", ".join(resolved_user_registry.list_ids()),
    )

    return app


def _make_inbound_handler(
    *,
    routing: RoutingStore,
    case_driver: CaseDriver,
    user_contexts: UserContextRegistry,
    clock: Clock,
):
    """Build the coroutine sms.server calls for every inbound SMS.

    The handler does three things, in order:

    1. Opt-out / opt-in keyword detection. Master-data consent is
       updated regardless of whether the customer's phone matches an
       active case; the corresponding ``CustomerOptedOut`` /
       ``CustomerOptedIn`` signal is fanned out to any case that is
       still open for that phone.
    2. Phone → case lookup via the routing store. Unknown phones are
       logged at ``simulator.sms.inbound.unknown_phone`` and dropped
       (Kate does not reply — there is no case to anchor a reply to).
    3. For known phones, the driver's ``on_inbound_sms`` is invoked.
       It records the customer turn (via the SMS dispatcher) and
       publishes one :class:`InboundSmsReceived` signal; the reducer
       decides whether the case transitions, whether to emit a fresh
       ``PlaceCall`` for an LLM-composed reply, or both.
    """

    async def _handler(*, from_number: str, to_number: str, body: str, message_sid: str) -> None:
        normalized = normalize_sms_body(body)
        entry = routing.find_entry(from_number)
        preferred_user_id = entry.user_id if entry is not None else ""

        if is_opt_out_keyword(normalized):
            updated = user_contexts.set_opt_status_for_phone(
                from_number,
                "opted_out",
                preferred_user_id=preferred_user_id,
            )
            await case_driver.on_signal(
                CustomerOptedOut(timestamp=clock.now(), customer_phone=from_number)
            )
            _log.info(
                "simulator.sms.inbound.opted_out",
                phone=from_number,
                master_data_updated=updated,
                message_sid=message_sid,
            )
            return

        if is_opt_in_keyword(normalized):
            updated = user_contexts.set_opt_status_for_phone(
                from_number,
                "opted_in",
                preferred_user_id=preferred_user_id,
            )
            await case_driver.on_signal(
                CustomerOptedIn(timestamp=clock.now(), customer_phone=from_number)
            )
            _log.info(
                "simulator.sms.inbound.opted_in",
                phone=from_number,
                master_data_updated=updated,
                message_sid=message_sid,
            )
            return

        if entry is None:
            _log.warning(
                "simulator.sms.inbound.unknown_phone",
                phone=from_number,
                body=body[:80],
                message_sid=message_sid,
            )
            return

        case_id = CaseId(entry.conversation_id)
        try:
            await case_driver.on_inbound_sms(
                case_id=case_id,
                from_phone=from_number,
                body=body,
                message_sid=message_sid,
            )
        except Exception as exc:
            _log.error(
                "simulator.sms.inbound.failed",
                phone=from_number,
                case_id=str(case_id),
                user_id=entry.user_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        _log.info(
            "simulator.sms.inbound.delivered",
            phone=from_number,
            case_id=str(case_id),
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
