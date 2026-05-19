"""HTTP and WebSocket route handlers.

Pure I/O glue: parse the request, call into the injected services,
return a model. No business logic lives here — if a handler grows past
trivial, it belongs in a service module.

Per-user state
==============
User-scoped data (customers, dealers, vehicles, slots) is injected
per-request via :func:`simulator._users.get_user_context`, which
reads the validated user id off ``request.state.user_id`` (set by
:class:`BasicAuthMiddleware`) and returns a cached
:class:`UserContext` for that user.

Truly global state (event bus, connection probe, system clock, the
shared SMS adapter, templates) is held in :class:`RouteDeps`, captured
once at app build time.

Per the operator's mental model (2026-05-10):

- The simulator is a **read/write UI over the master data**: customer,
  dealer, vehicle, slots. The operator edits these and saves them.
- A **trigger is the act of saying "go"** — the operator types a
  service type + summary and presses Fire. The trigger is synthesized
  on the server from the saved master data + the form input. It has
  no durable existence and no picker.
- All ElevenLabs traffic flows through ``CaseManager.fire``. The route
  layer never imports ``CallSession``.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from guidepoint.case import (
    Case,
    CaseError,
    CaseEvent,
    CaseId,
    CaseRepository,
    OfferedSlot,
    ServiceEvent,
    Trigger,
    TriggerId,
    TriggerSource,
)
from guidepoint.clock import Clock
from guidepoint.events import EventBus
from guidepoint.master_data import (
    CustomerId,
    CustomerNotFoundError,
    CustomerRecord,
    DealerId,
    DealerNotFoundError,
    DealerRecord,
    MasterDataError,
    VehicleNotFoundError,
    VehicleRecord,
    VehicleVin,
)
from sms_adapter import SmsContext, SmsDeps, open_conversation

from simulator._connection import ConnectionProbe
from simulator._models import (
    CaseSummary,
    ConnectionStatus,
    FireRequest,
    FireResponse,
    MasterDataSnapshot,
)
from simulator._sms_context_registry import SmsContextRegistry
from simulator._users import UserContext, get_user_context

_log = structlog.get_logger(__name__)


@final
@dataclass(frozen=True, slots=True)
class RouteDeps:
    """Global dependencies for the route layer.

    Per-user master data + slots are pulled per-request via
    ``Depends(get_user_context)``. Cases stay global for now (Commit
    1) so the live ``CallSession`` keeps writing to a single
    ``case_repo``; per-user case filtering will land in the next
    commit via an ownership manifest.
    """

    case_repo: CaseRepository
    trigger_source: TriggerSource
    bus: EventBus[CaseEvent]
    probe: ConnectionProbe
    clock: Clock
    templates: Jinja2Templates
    # SMS dispatch — None means SMS dispatch isn't configured (env vars
    # missing). The Fire route surfaces a 503 if channel=sms but sms_deps
    # is None, so voice still works regardless.
    sms_deps: SmsDeps | None = None
    sms_contexts: SmsContextRegistry | None = None


def build_router(*, deps: RouteDeps) -> APIRouter:
    """Construct the FastAPI router with handlers bound to ``deps``."""
    router = APIRouter()
    router.add_api_route("/", _index(deps), response_class=HTMLResponse)
    router.add_api_route("/health", _health, methods=["GET", "HEAD"])

    router.add_api_route(
        "/api/master-data",
        _master_data_snapshot,
        response_model=MasterDataSnapshot,
    )

    router.add_api_route("/api/customers/{customer_id}", _get_customer, response_model=CustomerRecord)
    router.add_api_route(
        "/api/customers/{customer_id}",
        _put_customer,
        methods=["PUT"],
        response_model=CustomerRecord,
    )

    router.add_api_route("/api/dealers/{dealer_id}", _get_dealer, response_model=DealerRecord)
    router.add_api_route(
        "/api/dealers/{dealer_id}",
        _put_dealer,
        methods=["PUT"],
        response_model=DealerRecord,
    )

    router.add_api_route("/api/vehicles/{vin}", _get_vehicle, response_model=VehicleRecord)
    router.add_api_route(
        "/api/vehicles/{vin}",
        _put_vehicle,
        methods=["PUT"],
        response_model=VehicleRecord,
    )

    router.add_api_route("/api/slots", _get_slots, response_model=list[OfferedSlot])
    router.add_api_route(
        "/api/slots",
        _put_slots,
        methods=["PUT"],
        response_model=list[OfferedSlot],
    )

    router.add_api_route("/api/cases", _list_recent_cases(deps), response_model=list[CaseSummary])
    router.add_api_route("/api/cases/{case_id}", _get_case(deps), response_model=Case)

    router.add_api_route("/api/connection", _connection(deps), response_model=ConnectionStatus)
    router.add_api_route("/api/fire", _fire(deps), methods=["POST"], response_model=FireResponse)
    router.add_api_websocket_route("/ws/log", _log_socket(deps))
    return router


# --------------------------------------------------------------------------- #
# Handlers                                                                    #
# --------------------------------------------------------------------------- #


def _health() -> dict[str, str]:
    """Liveness endpoint for Render's healthcheck. Always returns 200.

    Exempt from BasicAuthMiddleware (see _EXEMPT_PREFIXES) so Render's
    healthcheck can succeed without credentials.
    """
    return {"status": "ok"}


def _index(deps: RouteDeps) -> Callable[[Request], Coroutine[Any, Any, HTMLResponse]]:
    async def handler(request: Request) -> HTMLResponse:
        # Middleware has already validated Basic Auth by the time we
        # get here, so request.state.user_id is always set.
        return deps.templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "deployment_label": os.environ.get("DEPLOYMENT_LABEL", "").strip(),
                "user_id": getattr(request.state, "user_id", ""),
            },
        )

    return handler


def _master_data_snapshot(
    ctx: UserContext = Depends(get_user_context),
) -> MasterDataSnapshot:
    """One-shot loader for the page boot.

    Picks the first record of each entity by sort order. The simulator
    today operates on one of each; the snapshot endpoint is the single
    place that "primary record" assumption lives.
    """
    try:
        customer = _first_or_404(
            ctx.master_data.list_customers(),
            detail=f"no customer fixtures for user {ctx.user.id!r}",
        )
        dealer = _first_or_404(
            ctx.master_data.list_dealers(),
            detail=f"no dealer fixtures for user {ctx.user.id!r}",
        )
        vehicle = _first_or_404(
            ctx.master_data.list_vehicles(),
            detail=f"no vehicle fixtures for user {ctx.user.id!r}",
        )
    except MasterDataError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return MasterDataSnapshot(
        customer=customer,
        dealer=dealer,
        vehicle=vehicle,
        slots=ctx.slots_repo.list(),
    )


def _get_customer(
    customer_id: str,
    ctx: UserContext = Depends(get_user_context),
) -> CustomerRecord:
    try:
        return ctx.master_data.get_customer(CustomerId(customer_id))
    except MasterDataError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _put_customer(
    customer_id: str,
    record: CustomerRecord,
    ctx: UserContext = Depends(get_user_context),
) -> CustomerRecord:
    if record.id != customer_id:
        raise HTTPException(
            status_code=400,
            detail=f"customer id mismatch: url={customer_id!r} body={record.id!r}",
        )
    ctx.master_data.save_customer(record)
    _log.info("simulator.customer.saved", user_id=ctx.user.id, customer_id=customer_id)
    return record


def _get_dealer(
    dealer_id: str,
    ctx: UserContext = Depends(get_user_context),
) -> DealerRecord:
    try:
        return ctx.master_data.get_dealer(DealerId(dealer_id))
    except MasterDataError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _put_dealer(
    dealer_id: str,
    record: DealerRecord,
    ctx: UserContext = Depends(get_user_context),
) -> DealerRecord:
    if record.id != dealer_id:
        raise HTTPException(
            status_code=400,
            detail=f"dealer id mismatch: url={dealer_id!r} body={record.id!r}",
        )
    ctx.master_data.save_dealer(record)
    _log.info("simulator.dealer.saved", user_id=ctx.user.id, dealer_id=dealer_id)
    return record


def _get_vehicle(
    vin: str,
    ctx: UserContext = Depends(get_user_context),
) -> VehicleRecord:
    try:
        return ctx.master_data.get_vehicle(VehicleVin(vin))
    except MasterDataError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _put_vehicle(
    vin: str,
    record: VehicleRecord,
    ctx: UserContext = Depends(get_user_context),
) -> VehicleRecord:
    if record.vin != vin:
        raise HTTPException(
            status_code=400,
            detail=f"vin mismatch: url={vin!r} body={record.vin!r}",
        )
    ctx.master_data.save_vehicle(record)
    _log.info("simulator.vehicle.saved", user_id=ctx.user.id, vin=vin)
    return record


def _get_slots(
    ctx: UserContext = Depends(get_user_context),
) -> list[OfferedSlot]:
    return list(ctx.slots_repo.list())


def _put_slots(
    slots: list[OfferedSlot],
    ctx: UserContext = Depends(get_user_context),
) -> list[OfferedSlot]:
    saved = ctx.slots_repo.save(slots)
    _log.info("simulator.slots.saved", user_id=ctx.user.id, count=len(saved))
    return list(saved)


def _list_recent_cases(deps: RouteDeps) -> Callable[[], list[CaseSummary]]:
    def handler() -> list[CaseSummary]:
        return [
            CaseSummary(
                case_id=c.case_id,
                trigger_id=c.trigger_id,
                customer_full_name=c.customer.full_name,
                state=c.state.value,
                created_at=c.created_at,
                closed_at=c.closed_at,
            )
            for c in deps.case_repo.list_recent(limit=20)
        ]

    return handler


def _get_case(deps: RouteDeps) -> Callable[[str], Case]:
    def handler(case_id: str) -> Case:
        try:
            return deps.case_repo.get(CaseId(case_id))
        except CaseError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return handler


def _connection(deps: RouteDeps) -> Callable[[], ConnectionStatus]:
    def handler() -> ConnectionStatus:
        return deps.probe.check()

    return handler


def _fire(
    deps: RouteDeps,
) -> Callable[..., Coroutine[Any, Any, FireResponse]]:
    """Synthesize a trigger from saved master data + the form, then fire.

    The browser sends only ``service_type`` + ``service_summary`` (+
    optional ``narrative``). Everything else — which customer, which
    vehicle, which dealer, which slots — comes from the saved fixtures
    in the operator's user namespace.
    """

    async def handler(
        request: FireRequest,
        ctx: UserContext = Depends(get_user_context),
    ) -> FireResponse:
        try:
            customer = _first_or_404(
                ctx.master_data.list_customers(),
                detail=f"no customer fixtures for user {ctx.user.id!r}",
            )
            dealer = _first_or_404(
                ctx.master_data.list_dealers(),
                detail=f"no dealer fixtures for user {ctx.user.id!r}",
            )
            vehicle = _first_or_404(
                ctx.master_data.list_vehicles(),
                detail=f"no vehicle fixtures for user {ctx.user.id!r}",
            )
        except (CustomerNotFoundError, DealerNotFoundError, VehicleNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if request.channel == "sms":
            return await _fire_sms(
                deps=deps,
                ctx=ctx,
                customer=customer,
                dealer=dealer,
                vehicle=vehicle,
                request=request,
            )

        trigger = Trigger(
            id=TriggerId(f"trig_{secrets.token_hex(6)}"),
            vehicle_vin=vehicle.vin,
            dealer_id=dealer.id,
            service_event=ServiceEvent(
                type=request.service_type,
                summary=request.service_summary,
                narrative=request.narrative,
            ),
            channel_preference="voice",
            offered_slots=ctx.slots_repo.list(),
            source="operator",
            status="pending",
            created_at=deps.clock.now(),
        )
        # Register the synthesized trigger so CaseManager.mark_fired/_failed
        # have something to update. The ephemeral source no-ops if it isn't
        # registered. The trigger_source is global (single in-memory dict)
        # but each user's CaseManager carries its own master_data so
        # customer/vehicle/dealer lookups hit the right namespace.
        deps.trigger_source.save(trigger)

        try:
            case = await ctx.case_manager.fire(trigger)
        except (CaseError, MasterDataError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        _log.info(
            "simulator.fire.accepted",
            user_id=ctx.user.id,
            trigger_id=trigger.id,
            case_id=case.case_id,
            correlation_id=case.correlation_id,
            customer_id=customer.id,
            channel="voice",
        )
        return FireResponse(
            case_id=case.case_id,
            correlation_id=case.correlation_id,
            accepted_at=datetime.now(UTC),
        )

    return handler


async def _fire_sms(
    *,
    deps: RouteDeps,
    ctx: UserContext,
    customer: CustomerRecord,
    dealer: DealerRecord,
    vehicle: VehicleRecord,
    request: FireRequest,
) -> FireResponse:
    """SMS arm of the Fire handler.

    Builds an :class:`SmsContext` from the user's saved master data,
    registers it with the shared in-memory context registry so inbound
    replies can recover the variables, then asks
    ``sms_adapter.open_conversation`` to send the opening message.

    NOTE (Phase 1): the SMS adapter is still a single shared instance
    across users — history, routing, and context registry are global.
    Inbound routing today goes by phone number, so two users firing
    against different customers/phones work independently. Two users
    firing to the same phone follow the "replace prior conversation"
    rule below.
    """
    if deps.sms_deps is None or deps.sms_contexts is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "SMS dispatch not configured. Set TWILIO_ACCOUNT_SID, "
                "TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, LLM_MODEL in .env "
                "and restart the simulator."
            ),
        )

    if not customer.phone:
        raise HTTPException(
            status_code=409,
            detail=f"customer {customer.id!r} has no phone number; cannot send SMS",
        )

    conversation_id = f"sms_{secrets.token_hex(6)}"
    correlation_id = f"corr_{secrets.token_hex(6)}"
    variables = _build_sms_variables(
        customer=customer,
        dealer=dealer,
        vehicle=vehicle,
        slots=ctx.slots_repo.list(),
        channel=request.channel,
        service_type=request.service_type,
        service_summary=request.service_summary,
        service_narrative=request.narrative,
        conversation_id=conversation_id,
    )

    sms_ctx = SmsContext(
        conversation_id=conversation_id,
        customer_phone=customer.phone,
        variables=variables,
    )

    # Pressing Fire in the simulator means "start a new conversation
    # against this phone, replacing any prior one." History for prior
    # conversations stays on disk under their old conversation_ids; we
    # just drop the routing binding so this phone now points at the
    # new conversation. Without this, the second Fire press always
    # 502s with "phone already bound."
    prior_conversation_id = deps.sms_deps.routing.find_conversation_id(customer.phone)
    if prior_conversation_id is not None and prior_conversation_id != conversation_id:
        deps.sms_deps.routing.unbind(phone=customer.phone)
        deps.sms_contexts.forget(prior_conversation_id)
        _log.info(
            "simulator.fire.sms.replaced_prior_conversation",
            phone=customer.phone,
            prior_conversation_id=prior_conversation_id,
            new_conversation_id=conversation_id,
        )

    deps.sms_contexts.register(sms_ctx)

    try:
        opening = await open_conversation(sms_ctx, deps=deps.sms_deps)
    except Exception as exc:
        deps.sms_contexts.forget(conversation_id)
        deps.sms_deps.routing.unbind(phone=customer.phone)
        raise HTTPException(
            status_code=502,
            detail=f"SMS open failed: {type(exc).__name__}: {exc}",
        ) from exc

    _log.info(
        "simulator.fire.accepted",
        user_id=ctx.user.id,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        customer_id=customer.id,
        phone=customer.phone,
        channel="sms",
        opening_chars=len(opening),
    )
    return FireResponse(
        case_id=conversation_id,
        correlation_id=correlation_id,
        accepted_at=datetime.now(UTC),
    )


def _build_sms_variables(
    *,
    customer: CustomerRecord,
    dealer: DealerRecord,
    vehicle: VehicleRecord,
    slots: Iterable[OfferedSlot],
    channel: str,
    service_type: str,
    service_summary: str,
    service_narrative: str,
    conversation_id: str,
) -> dict[str, str]:
    """Flatten master data + form into the variables dict the prompt
    composer substitutes.

    Mirrors ``Case.to_variables()`` for the keys the system prompt
    actually references. If a placeholder is present in
    ``system-prompt.md`` or ``sms.md`` that we don't supply here, the
    composer raises ``MissingPlaceholderError`` and the Fire route
    surfaces it as a 502 — that's the contract.
    """
    slots_tuple = tuple(slots)
    return {
        "channel": channel,
        "case_id": conversation_id,
        "trigger_id": conversation_id,
        "customer_id": customer.id,
        "customer_first_name": customer.first_name,
        "customer_last_name": customer.last_name,
        "customer_full_name": customer.full_name,
        "customer_phone": customer.phone,
        "customer_opt_status": customer.opt_status,
        "customer_preferred_channel": customer.preferred_channel,
        "customer_timezone": customer.timezone,
        "dealer_id": dealer.id,
        "dealer_name": dealer.name,
        "dealer_phone": dealer.phone,
        "dealer_address": dealer.address,
        "ride_radius_miles": str(dealer.ride_radius_miles),
        "vehicle_year": str(vehicle.year),
        "vehicle_make": vehicle.make,
        "vehicle_model": vehicle.model,
        "vehicle_vin": vehicle.vin,
        "vehicle_odometer_miles": str(vehicle.odometer_miles),
        "vehicle_location_lat": f"{vehicle.current_location.latitude:.6f}",
        "vehicle_location_lon": f"{vehicle.current_location.longitude:.6f}",
        "vehicle_location_description": vehicle.current_location.description,
        "service_reason_type": service_type,
        "service_reason_summary": service_summary,
        "service_reason_narrative": service_narrative,
        "slot_count": str(len(slots_tuple)),
        "slot_options": "; ".join(s.display for s in slots_tuple),
    }


def _log_socket(deps: RouteDeps) -> Callable[[WebSocket], Coroutine[Any, Any, None]]:
    """WebSocket feed for case events.

    Phase 1: every connected client sees every event regardless of
    which user they're operating as. Filtering by user_id will land
    in Phase 2 when ``CaseEvent`` learns about the user dimension.
    """

    async def handler(socket: WebSocket) -> None:
        await socket.accept()
        try:
            async for event in deps.bus.subscribe():
                await socket.send_text(event.model_dump_json())
        except WebSocketDisconnect:
            return

    return handler


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _first_or_404[R](records: Iterable[R], *, detail: str) -> R:
    """Return the first record of an iterable, raise 404 if empty.

    The master-data repository's ``list_*`` methods are ``Iterable``
    so the iterator may not have a length; this helper consumes one.
    """
    for record in records:
        return record
    raise HTTPException(status_code=404, detail=detail)


def package_static_dir() -> Path:
    """Absolute path to the packaged ``static/`` directory."""
    return Path(__file__).parent / "static"


def package_templates_dir() -> Path:
    """Absolute path to the packaged ``templates/`` directory."""
    return Path(__file__).parent / "templates"


__all__ = [
    "RouteDeps",
    "build_router",
    "package_static_dir",
    "package_templates_dir",
]
