"""Simulator -- public surface.

Operator console: edit master data (customer / dealer / vehicle / slots),
type a service type + summary, press Fire. The Fire route synthesizes a
``Trigger`` from the saved master data plus the form payload and hands
it to ``CaseManager``. CaseEvents stream back over ``/ws/log``.

Per ADR 0006 the simulator is just a UI plus the master-data repository,
a slots repository, an in-memory trigger source, a case repository, and
a ``CaseManager``. All ElevenLabs traffic flows through the case
manager — the simulator never touches the SDK directly.

Typical use::

    from pathlib import Path
    from simulator import build_app

    app = build_app(project_root=Path.cwd())
    # then run with: uvicorn simulator:app
"""

from simulator._app import build_app
from simulator._connection import ConnectionProbe, build_env_connection_probe
from simulator._models import (
    CaseSummary,
    ConnectionStatus,
    FireRequest,
    FireResponse,
    MasterDataSnapshot,
)

__all__ = [
    "CaseSummary",
    "ConnectionProbe",
    "ConnectionStatus",
    "FireRequest",
    "FireResponse",
    "MasterDataSnapshot",
    "build_app",
    "build_env_connection_probe",
]
