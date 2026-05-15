"""Boundary models for the simulator HTTP/WebSocket surface.

Everything that crosses the browser ↔ server boundary is validated here.
The simulator never accepts free-form JSON: every payload routes through
``model_validate`` so a malformed request from the browser fails with a
typed 422 instead of an opaque crash.

These are *separate* from ``guidepoint.case`` and
``guidepoint.master_data`` boundary models on purpose. Those describe
the durable domain (cases, customers). These describe the operator's
browser interactions: master-data snapshot, fire request, status pings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from guidepoint.case import (
    CaseId,
    OfferedSlot,
    ServiceReasonType,
    TriggerId,
)
from guidepoint.master_data import (
    CustomerRecord,
    DealerRecord,
    VehicleRecord,
)

ChannelChoice = Literal["voice", "sms"]


def _frozen_strict() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class FireRequest(BaseModel):
    """Operator clicks Fire — the act of saying "go".

    Everything else (customer / vehicle / dealer / slots) is read from
    the saved master data on the server. The operator only chooses
    *what kind of service this is* and *how to describe it briefly*.
    """

    model_config = _frozen_strict()

    service_type: ServiceReasonType
    service_summary: str = Field(min_length=1)
    narrative: str = ""
    channel: ChannelChoice = "voice"


class FireResponse(BaseModel):
    """Acknowledged fire — caller follows the case on the WS feed."""

    model_config = _frozen_strict()

    case_id: CaseId
    correlation_id: str = Field(min_length=1)
    accepted_at: datetime


class CaseSummary(BaseModel):
    """Lightweight case index entry for the recent-cases panel."""

    model_config = _frozen_strict()

    case_id: CaseId
    trigger_id: TriggerId
    customer_full_name: str = Field(min_length=1)
    state: str = Field(min_length=1)
    created_at: datetime
    closed_at: datetime | None = None


class ConnectionStatus(BaseModel):
    """The simulator's view of its connection to ElevenLabs."""

    model_config = _frozen_strict()

    api_key_present: bool
    agent_id_present: bool
    agent_id: str = ""
    last_checked: datetime
    healthy: bool
    detail: str = ""


class MasterDataSnapshot(BaseModel):
    """One-shot snapshot of the editable master data on page boot.

    Returned by ``GET /api/master-data``. The simulator currently
    operates on one customer / one vehicle / one dealer / one slot
    list — the single record of each (the first, deterministically
    sorted by id / vin / id) is what the UI loads.
    """

    model_config = _frozen_strict()

    customer: CustomerRecord
    dealer: DealerRecord
    vehicle: VehicleRecord
    slots: tuple[OfferedSlot, ...]


__all__ = [
    "CaseSummary",
    "ChannelChoice",
    "ConnectionStatus",
    "FireRequest",
    "FireResponse",
    "MasterDataSnapshot",
]
