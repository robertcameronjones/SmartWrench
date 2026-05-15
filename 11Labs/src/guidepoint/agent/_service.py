"""``AgentManager`` implementation and its factory.

The implementation is unimportable across package boundaries (Pyright
``reportPrivateUsage`` is ``error``); construct via ``build_agent_manager``.

The service is a thin orchestrator over the injected ``ElevenLabsClient``
and ``ConfigPaths`` — all I/O lives at those two seams.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from guidepoint.agent._io import (
    ConfigPaths,
    read_agent_config,
    read_all_tools,
    write_agent_config,
    write_system_prompt,
    write_tool,
)
from guidepoint.agent._models import (
    AgentConfig,
    AgentConfigInvalidError,
    AgentId,
    ElevenLabsClient,
    ToolDef,
)
from guidepoint.agent._protocol import AgentManager, PullReport
from guidepoint.agent._variable_audit import AuditIssue, audit_files


@final
@dataclass(frozen=True, slots=True)
class _PullReport:
    """Concrete ``PullReport`` returned by ``_AgentManagerImpl.pull``."""

    agent_id: AgentId
    config_path: str
    system_prompt_path: str
    tools: tuple[ToolDef, ...]


@final
@dataclass(frozen=True, slots=True)
class _AgentManagerImpl:
    """Default ``AgentManager`` implementation.

    Pure orchestration: reads from the ElevenLabs adapter, writes to the
    config files. No global state, no module-level singletons.
    """

    _client: ElevenLabsClient
    _paths: ConfigPaths

    def pull(self, *, agent_id: AgentId) -> PullReport:
        config, prompt_body = self._client.fetch_agent_config(agent_id=agent_id)
        write_agent_config(paths=self._paths, config=config)
        write_system_prompt(paths=self._paths, body=prompt_body)
        tools = tuple(self._client.fetch_tool(tool_id=tid) for tid in config.tool_ids)
        for tool in tools:
            write_tool(paths=self._paths, tool=tool)
        return _PullReport(
            agent_id=config.agent_id,
            config_path=str(self._paths.agent_json),
            system_prompt_path=str(self._paths.system_prompt_md),
            tools=tools,
        )

    def validate(self) -> None:
        validate_config(paths=self._paths)


def build_agent_manager(
    *,
    client: ElevenLabsClient,
    paths: ConfigPaths,
) -> AgentManager:
    """Construct the canonical ``AgentManager``.

    Args:
        client: The adapter to ElevenLabs (real SDK in production, fake in
            tests).
        paths: Resolved on-disk locations for the project's config and
            fixtures. See ``ConfigPaths.for_root``.
    """
    return _AgentManagerImpl(_client=client, _paths=paths)


def validate_config(*, paths: ConfigPaths) -> None:
    """Validate the on-disk config without contacting ElevenLabs.

    Combines the prompt-variable audit (``_variable_audit.audit_files``)
    with cross-file consistency checks (e.g. tool-count parity). The
    variable-audit rule lives in one place; this function does not
    duplicate it.

    Only **errors** from the audit are promoted to validation failures;
    audit warnings are intentionally not raised here so ``validate`` stays
    a build gate. The dedicated ``check-prompt`` CLI surfaces both.

    Raises:
        AgentConfigInvalidError: one or more rules failed.
    """
    config = read_agent_config(paths=paths)
    tools = read_all_tools(paths=paths)
    audit_errors = audit_files(paths=paths).errors
    issues = _collect_issues(config=config, tools=tools, audit_errors=audit_errors)
    if issues:
        raise AgentConfigInvalidError(issues)


# ---------------------------------------------------------------------------
# Cross-file consistency rules (variable audit lives in _variable_audit)
# ---------------------------------------------------------------------------


def _collect_issues(
    *,
    config: AgentConfig,
    tools: tuple[ToolDef, ...],
    audit_errors: tuple[AuditIssue, ...],
) -> tuple[str, ...]:
    """Return human-readable issues found in the on-disk config."""
    issues: list[str] = [e.message for e in audit_errors]

    declared_tool_count = len(config.tool_ids)
    on_disk_tool_count = len(tools)
    if declared_tool_count != on_disk_tool_count:
        issues.append(
            f"agent.toml lists {declared_tool_count} tool_ids but "
            f"{on_disk_tool_count} tool files are present in config/tools/"
        )

    return tuple(issues)
