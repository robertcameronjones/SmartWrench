"""In-memory fakes for the agent Protocols.

Lives in ``tests/`` because production code must never import a fake.
"""

from __future__ import annotations

from typing import final, override

from guidepoint.agent import (
    AgentConfig,
    AgentId,
    AgentNotFoundError,
    ElevenLabsClient,
    ToolDef,
    ToolId,
)


@final
class InMemoryElevenLabsClient(ElevenLabsClient):
    """A canned-response ``ElevenLabsClient`` for the service-layer tests."""

    def __init__(
        self,
        *,
        agents: dict[AgentId, tuple[AgentConfig, str]],
        tools: dict[ToolId, ToolDef],
    ) -> None:
        self._agents = dict(agents)
        self._tools = dict(tools)
        self._fetch_calls: list[AgentId] = []
        self._tool_calls: list[ToolId] = []

    @override
    def fetch_agent_config(self, *, agent_id: AgentId) -> tuple[AgentConfig, str]:
        self._fetch_calls.append(agent_id)
        if agent_id not in self._agents:
            raise AgentNotFoundError(agent_id)
        return self._agents[agent_id]

    @override
    def fetch_tool(self, *, tool_id: ToolId) -> ToolDef:
        self._tool_calls.append(tool_id)
        return self._tools[tool_id]

    @property
    def fetch_calls(self) -> tuple[AgentId, ...]:
        return tuple(self._fetch_calls)

    @property
    def tool_calls(self) -> tuple[ToolId, ...]:
        return tuple(self._tool_calls)
