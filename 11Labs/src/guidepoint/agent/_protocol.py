"""The public Protocol for the agent-management subsystem.

Re-exported from ``agent/__init__.py``. Consumers depend on this Protocol;
the concrete implementation lives in ``_service.py`` and is unimportable
across package boundaries (Pyright ``reportPrivateUsage`` is ``error``).
"""

from __future__ import annotations

from typing import Protocol

from guidepoint.agent._models import AgentId, ToolDef


class PullReport(Protocol):
    """Read-only summary of what a ``pull`` wrote to disk.

    Returned by ``AgentManager.pull`` so the CLI can print a confirmation
    without re-reading the files. Implemented as a frozen dataclass in
    ``_service.py``.
    """

    @property
    def agent_id(self) -> AgentId: ...
    @property
    def config_path(self) -> str: ...
    @property
    def system_prompt_path(self) -> str: ...
    @property
    def tools(self) -> tuple[ToolDef, ...]: ...


class AgentManager(Protocol):
    """Bootstrap, validate, and (later) push the local agent configuration.

    The implementation is wired through ``build_agent_manager``. There is
    exactly one implementation; the Protocol exists so test code can swap it
    for an in-memory fake without touching the SDK.
    """

    def pull(self, *, agent_id: AgentId) -> PullReport:
        """Fetch the live agent config + tools from ElevenLabs to disk.

        Writes ``config/agent.toml``, ``config/system-prompt.md``, and one
        ``config/tools/<name>.toml`` per attached tool. Overwrites existing
        files — callers are responsible for staging changes in version
        control before running this.

        Raises:
            AgentNotFoundError: the agent id is unknown to ElevenLabs.
        """
        ...

    def validate(self) -> None:
        """Validate the on-disk config without contacting ElevenLabs.

        Confirms that every ``{{variable}}`` referenced in the system prompt
        either has a default in ``config/agent.toml`` or is produced by
        ``Case.to_variables``. Raises ``AgentConfigInvalidError`` on
        mismatch.
        """
        ...
