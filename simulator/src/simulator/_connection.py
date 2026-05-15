"""Connection-status probe for the ElevenLabs link.

Mockup phase: reports whether ``ELEVENLABS_API_KEY`` and
``ELEVENLABS_AGENT_ID`` are set in the environment. No network probe.

Wire-up phase: a second implementation will call ``client.user.get()``
or similar to confirm the key is live; the Protocol stays the same.
"""

from __future__ import annotations

import os
from typing import Protocol, final

from guidepoint.clock import Clock
from simulator._models import ConnectionStatus


class ConnectionProbe(Protocol):
    """Returns the current health of the ElevenLabs connection."""

    def check(self) -> ConnectionStatus:
        """Synchronously read the current state."""
        ...


def build_env_connection_probe(*, clock: Clock) -> ConnectionProbe:
    """Mockup probe -- reads env vars only, no network call."""
    return _EnvConnectionProbe(clock=clock)


@final
class _EnvConnectionProbe:
    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock

    def check(self) -> ConnectionStatus:
        api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
        agent_id = (os.environ.get("ELEVENLABS_AGENT_ID") or "").strip()
        api_key_present = bool(api_key)
        agent_id_present = bool(agent_id)
        healthy = api_key_present and agent_id_present
        if healthy:
            detail = "env vars present (no live probe yet)"
        elif not api_key_present:
            detail = "ELEVENLABS_API_KEY missing"
        else:
            detail = "ELEVENLABS_AGENT_ID missing"
        return ConnectionStatus(
            api_key_present=api_key_present,
            agent_id_present=agent_id_present,
            agent_id=agent_id,
            last_checked=self._clock.now(),
            healthy=healthy,
            detail=detail,
        )


__all__ = ["ConnectionProbe", "build_env_connection_probe"]
