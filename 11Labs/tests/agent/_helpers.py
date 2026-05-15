"""Shared test helpers for the agent suite.

Public within this private package (``_helpers``-prefixed module). Both
``test_service`` and ``test_variable_audit`` build agents/configs/tool
fixtures the same way; this is the one place that lives.
"""

from __future__ import annotations

from guidepoint.agent import (
    AgentConfig,
    AgentId,
    ConfigPaths,
    ToolDef,
    ToolId,
    ToolParameter,
)
from guidepoint.agent._io import write_agent_config, write_system_prompt, write_tool


def minimal_agent_config(
    *,
    tool_ids: tuple[ToolId, ...] = (),
) -> AgentConfig:
    """Build a minimal valid AgentConfig for tests.

    Per ADR 0004 there is no ``variables`` field — runtime variable
    values live exclusively in the Case fixture.
    """
    return AgentConfig(
        agent_id=AgentId("agent_x"),
        name="Kate",
        language="en",
        llm="gpt-4o-mini",
        temperature=0.5,
        first_message="Hi.",
        voice_id="v1",
        tts_model_id="m1",
        system_prompt_path="system-prompt.md",
        tool_ids=tool_ids,
    )


def sample_tool(tool_id: ToolId, name: str) -> ToolDef:
    return ToolDef(
        tool_id=tool_id,
        name=name,
        description="d",
        url="https://example.com/" + name,
        method="POST",
        parameters=(ToolParameter(name="x", type="string", description="d", required=False),),
    )


def seed(
    *,
    paths: ConfigPaths,
    config: AgentConfig,
    prompt: str,
    tools: tuple[ToolDef, ...],
) -> None:
    write_agent_config(paths=paths, config=config)
    write_system_prompt(paths=paths, body=prompt)
    for tool in tools:
        write_tool(paths=paths, tool=tool)
