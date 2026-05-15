"""Boundary models for the agent module.

These represent everything that crosses the agent's file or HTTP
boundary: the local JSON/Markdown config files and the ElevenLabs
agent/tool payloads.

Per ADR 0006 the per-call ``Case`` payload moved to
``guidepoint.case`` — the agent module only owns the ElevenLabs
configuration shape (system prompt, tool defs, voice settings).

All models are Pydantic v2 with ``frozen=True`` and ``extra="forbid"``
per the boundary-validation rule. Nothing here mutates after
construction; nothing here silently swallows unknown fields.
"""

from __future__ import annotations

from typing import Literal, NewType, Protocol, final

from pydantic import BaseModel, ConfigDict, Field

# NOTE: Pydantic v2 models are intentionally not decorated with ``@final``.
# Combining ``@final`` with ``BaseModel`` trips Pyright's
# ``reportUninitializedInstanceVariable`` against Pydantic's own internal
# slots. Frozen-ness + ``extra="forbid"`` already give us the immutability
# and closure properties ``@final`` would add at the dataclass level.

# Phantom-typed ids for the agent domain.
AgentId = NewType("AgentId", str)
ToolId = NewType("ToolId", str)
PhoneNumberId = NewType("PhoneNumberId", str)

ToolHttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


def _frozen_strict() -> ConfigDict:
    """Standard model config: immutable, no unknown fields."""
    return ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Agent configuration (config/agent.json + config/system-prompt.md)
# ---------------------------------------------------------------------------


class AgentConfig(BaseModel):
    """The local source-of-truth representation of an ElevenLabs agent.

    Mirrors the subset of the ElevenLabs ``GetAgentResponseModel`` that we
    actively manage from this codebase. Fields not present here (RAG, custom
    LLM, knowledge base, branches, etc.) are owned by the dashboard until
    promoted into this schema with an ADR.

    Per ADR 0004, this config carries no ``variables`` block: per-call
    variable values are owned by the ``Case`` snapshot (single point of
    truth). The ElevenLabs dashboard "Test agent" panel is supplied with
    values by the human pasting from a Case's ``to_variables`` output.
    """

    model_config = _frozen_strict()

    agent_id: AgentId
    name: str = Field(min_length=1)
    language: str = Field(min_length=2, max_length=8)
    llm: str = Field(min_length=1)
    temperature: float = Field(ge=0.0, le=2.0)
    first_message: str
    voice_id: str = Field(min_length=1)
    tts_model_id: str = Field(min_length=1)
    system_prompt_path: str = Field(min_length=1)
    tool_ids: tuple[ToolId, ...] = ()


# ---------------------------------------------------------------------------
# Tool configuration (config/tools/<name>.json)
# ---------------------------------------------------------------------------


class ToolParameter(BaseModel):
    """One parameter the LLM is allowed to send to a tool."""

    model_config = _frozen_strict()

    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    description: str
    required: bool = False


class ToolMock(BaseModel):
    """A canned response used by the dashboard "Mock tool" feature."""

    model_config = _frozen_strict()

    name: str = Field(min_length=1)
    status_code: int = Field(ge=100, le=599)
    body: str = ""
    condition: str = ""


class ToolDef(BaseModel):
    """A webhook tool definition."""

    model_config = _frozen_strict()

    tool_id: ToolId
    name: str = Field(min_length=1)
    description: str
    method: ToolHttpMethod = "POST"
    url: str = ""
    parameters: tuple[ToolParameter, ...] = ()
    mocks: tuple[ToolMock, ...] = ()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AgentError(Exception):
    """Base class for all expected agent-management failures."""


@final
class AgentNotFoundError(AgentError):
    """The requested agent id does not exist (or is not visible to our key)."""

    def __init__(self, agent_id: AgentId) -> None:
        super().__init__(f"Agent {agent_id!r} not found")
        self.agent_id = agent_id


@final
class AgentConfigInvalidError(AgentError):
    """Local config files failed validation."""

    def __init__(self, issues: tuple[str, ...]) -> None:
        super().__init__("Agent config invalid:\n  - " + "\n  - ".join(issues))
        self.issues = issues


# ---------------------------------------------------------------------------
# Ports (Protocols implemented by adapters)
# ---------------------------------------------------------------------------


class ElevenLabsClient(Protocol):
    """Port for whatever adapter talks to ElevenLabs.

    Production wires in a real SDK adapter; tests substitute an in-memory
    fake. The agent service depends on this Protocol, never on the SDK.
    """

    def fetch_agent_config(self, *, agent_id: AgentId) -> tuple[AgentConfig, str]:
        """Return ``(config, system_prompt_text)`` for the given agent.

        Splitting the prompt out here keeps the long markdown body out of
        the JSON config file — see ``write_agent_config``.

        Raises:
            AgentNotFoundError: the agent id does not exist.
        """
        ...

    def fetch_tool(self, *, tool_id: ToolId) -> ToolDef:
        """Return one tool's full definition."""
        ...
