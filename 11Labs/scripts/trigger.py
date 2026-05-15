"""Fire one trigger from the CLI and place an outbound Kate call.

Composes a ``Trigger`` in memory from the saved master data + the
``--service-type`` and ``--service-summary`` flags, then runs it
through the same ``CaseManager`` the simulator uses. Per ADR 0006
``CaseManager`` is the single point of contact with ElevenLabs.

Triggers are no longer durable JSON files — they're the act of saying
"go." The simulator UI is the usual way to do this; this script is the
headless equivalent.

Usage::

    # Real call (places an outbound call to the customer's phone):
    python scripts/trigger.py \\
        --service-type maintenance \\
        --service-summary "30k mile service"

    # Dry run -- print the dynamic_variables payload that would be sent:
    python scripts/trigger.py \\
        --service-type maintenance \\
        --service-summary "30k mile service" \\
        --dry-run

Required in .env for live mode:
    ELEVENLABS_API_KEY
    ELEVENLABS_AGENT_ID
    ELEVENLABS_AGENT_PHONE_NUMBER_ID
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import cast, get_args

from _client import get_client
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

from guidepoint.case import (
    JsonCasePaths,
    RetryPolicy,
    ServiceEvent,
    ServiceReasonType,
    Trigger,
    TriggerId,
    build_default_case_manager,
    build_json_case_repository,
    build_live_call_session,
    create_case_from_trigger,
)
from guidepoint.case._models import CaseEvent
from guidepoint.clock import build_system_clock
from guidepoint.events import build_event_bus
from guidepoint.master_data import (
    JsonFilePaths,
    build_json_master_data_repository,
)
from guidepoint.observability import bind_context, configure_logging

# The simulator package now lives in ../simulator. To use this CLI from the
# 11Labs venv, install the simulator first:
#     pip install -e ../simulator
from simulator._ephemeral_triggers import EphemeralTriggerSource
from simulator._slots import build_slots_repository


def _first_or_die[R](records: Iterable[R], *, what: str) -> R:
    for record in records:
        return record
    sys.exit(f"No {what} fixtures found.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fire one trigger via the case manager")
    parser.add_argument(
        "--service-type",
        required=True,
        choices=list(get_args(ServiceReasonType)),
        help="Service reason category",
    )
    parser.add_argument(
        "--service-summary",
        required=True,
        help='Short customer-facing summary (e.g. "oil change")',
    )
    parser.add_argument(
        "--narrative",
        default="",
        help="Optional internal context for Kate (not read aloud)",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Directory containing fixtures/ (default: cwd)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the dynamic_variables payload that would be sent and exit",
    )
    args = parser.parse_args()

    load_dotenv(args.project_root / ".env")
    configure_logging()

    project_root = args.project_root.resolve()
    master_data = build_json_master_data_repository(
        paths=JsonFilePaths.for_root(project_root),
    )
    slots_repo = build_slots_repository(project_root=project_root)

    customer = _first_or_die(master_data.list_customers(), what="customer")
    dealer = _first_or_die(master_data.list_dealers(), what="dealer")
    vehicle = _first_or_die(master_data.list_vehicles(), what="vehicle")
    clock = build_system_clock()

    trigger = Trigger(
        id=TriggerId(f"trig_{secrets.token_hex(6)}"),
        vehicle_vin=vehicle.vin,
        dealer_id=dealer.id,
        service_event=ServiceEvent(
            type=cast(ServiceReasonType, args.service_type),
            summary=args.service_summary,
            narrative=args.narrative,
        ),
        channel_preference="voice",
        offered_slots=slots_repo.list(),
        source="operator",
        status="pending",
        created_at=clock.now(),
    )
    bind_context(trigger_id=trigger.id)

    if args.dry_run:
        case = create_case_from_trigger(
            trigger=trigger,
            customer=customer,
            dealer=dealer,
            vehicle=vehicle,
            clock=clock,
        )
        payload = {
            "agent_id": os.getenv("ELEVENLABS_AGENT_ID"),
            "agent_phone_number_id": os.getenv("ELEVENLABS_AGENT_PHONE_NUMBER_ID"),
            "to_number": customer.phone,
            "conversation_initiation_client_data": {
                "dynamic_variables": case.to_variables(),
            },
        }
        print(json.dumps(payload, indent=2))
        return

    agent_id = (os.getenv("ELEVENLABS_AGENT_ID") or "").strip()
    phone_number_id = (os.getenv("ELEVENLABS_AGENT_PHONE_NUMBER_ID") or "").strip()
    if not agent_id:
        sys.exit("Set ELEVENLABS_AGENT_ID in .env")
    if not phone_number_id:
        sys.exit("Set ELEVENLABS_AGENT_PHONE_NUMBER_ID in .env")

    client: ElevenLabs = get_client()
    case_repo = build_json_case_repository(paths=JsonCasePaths.for_root(project_root))
    bus = build_event_bus(payload_type=CaseEvent)
    trigger_source = EphemeralTriggerSource()
    trigger_source.save(trigger)
    call_session = build_live_call_session(
        client=client,
        agent_id=agent_id,
        phone_number_id=phone_number_id,
        case_repo=case_repo,
        bus=bus,
        clock=clock,
    )
    manager = build_default_case_manager(
        master_data=master_data,
        case_repo=case_repo,
        trigger_source=trigger_source,
        call_session=call_session,
        bus=bus,
        clock=clock,
        retry_policy=RetryPolicy(),
    )
    case = asyncio.run(manager.fire(trigger))
    bind_context(case_id=case.case_id, customer_id=case.customer.id)
    print(json.dumps(case.model_dump(), indent=2, default=str))


if __name__ == "__main__":
    main()
