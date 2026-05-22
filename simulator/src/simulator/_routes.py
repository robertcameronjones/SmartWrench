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

Truly global state (event bus, connection probe, system clock,
templates) is held in :class:`RouteDeps`, captured once at app build
time.

Per the operator's mental model (2026-05-10):

- The simulator is a **read/write UI over the master data**: customer,
  dealer, vehicle, slots. The operator edits these and saves them.
- A **trigger is the act of saying "go"** — the operator types a
  service type + summary and presses Fire. The trigger is synthesized
  on the server from the saved master data + the form input. It has
  no durable existence and no picker.
- Every Fire goes through ``CaseManager.start`` regardless of channel.
  The manager picks the right ``CallSession`` (voice → ElevenLabs,
  sms → ``SmsCallSession``) by ``trigger.channel_preference``. The
  route layer never imports any ``CallSession`` directly.
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

from simulator._connection import ConnectionProbe
from simulator._models import (
    CaseSummary,
    ConnectionStatus,
    FireRequest,
    FireResponse,
    MasterDataSnapshot,
)
from simulator._users import UserContext, get_user_context

_log = structlog.get_logger(__name__)


@final
@dataclass(frozen=True, slots=True)
class RouteDeps:
    """Global dependencies for the route layer.

    Per-user master data + slots are pulled per-request via
    ``Depends(get_user_context)``. The case repo, trigger source,
    and call sessions are global — the per-user ``CaseManager``
    inside ``UserContext`` is wired against them but reuses the
    singleton instances built in ``build_app``.
    """

    case_repo: CaseRepository
    trigger_source: TriggerSource
    bus: EventBus[CaseEvent]
    probe: ConnectionProbe
    clock: Clock
    templates: Jinja2Templates
    # Set of channels the manager actually has a CallSession for. Used
    # by the Fire route to 503 cleanly when an operator picks a channel
    # that isn't wired (typically SMS when its env vars are missing).
    enabled_channels: frozenset[str] = frozenset({"voice"})


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
    """Synthesize a trigger from saved master data + the form, then start the case.

    The browser sends only ``service_type`` + ``service_summary`` (+
    optional ``narrative``) and ``channel``. Everything else — which
    customer, which vehicle, which dealer, which slots — comes from
    the saved fixtures in the operator's user namespace.

    Both voice and SMS go through ``case_manager.start(trigger)``;
    the manager picks the channel's ``CallSession`` from the trigger
    and spawns the call attempt as a background task. We return the
    case in ``state=CALLING``; terminal transitions stream to the UI
    via the ``/ws/log`` WebSocket.
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

        if request.channel not in deps.enabled_channels:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"channel {request.channel!r} is not configured on this "
                    f"deployment (enabled: {sorted(deps.enabled_channels)}). "
                    "For SMS, set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
                    "TWILIO_FROM_NUMBER and restart."
                ),
            )

        if request.channel == "sms" and not customer.phone:
            raise HTTPException(
                status_code=409,
                detail=f"customer {customer.id!r} has no phone number; cannot send SMS",
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
            channel_preference=request.channel,
            offered_slots=ctx.slots_repo.list(),
            source="operator",
            status="pending",
            created_at=deps.clock.now(),
            user_id=ctx.user.id,
        )
        # Register the synthesized trigger so CaseManager.mark_fired /
        # mark_failed have something to update. The ephemeral source
        # no-ops if it isn't registered. The trigger_source is global
        # but each user's CaseManager carries its own master_data so
        # customer/vehicle/dealer lookups hit the right namespace.
        deps.trigger_source.save(trigger)

        try:
            case = await ctx.case_manager.start(trigger)
        except (CaseError, MasterDataError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        _log.info(
            "simulator.fire.accepted",
            user_id=ctx.user.id,
            trigger_id=trigger.id,
            case_id=case.case_id,
            correlation_id=case.correlation_id,
            customer_id=customer.id,
            channel=request.channel,
        )
        return FireResponse(
            case_id=case.case_id,
            correlation_id=case.correlation_id,
            accepted_at=datetime.now(UTC),
        )

    return handler


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
