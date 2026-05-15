"""Agent — public surface.

The only names a consumer may import from this package. Implementation
lives in private modules (``_models``, ``_io``, ``_client``, ``_service``)
and is unimportable across package boundaries (Pyright
``reportPrivateUsage`` is set to ``error``).

Per ADR 0006 the per-call ``Case`` payload, master data records, and
the case lifecycle live in their own modules (``guidepoint.case``,
``guidepoint.master_data``). The agent module owns only the ElevenLabs
agent + tool configuration shape and the prompt-variable audit.

Typical use::

    from pathlib import Path
    from elevenlabs.client import ElevenLabs
    from guidepoint.agent import (
        AgentId,
        ConfigPaths,
        build_agent_manager,
        build_elevenlabs_client,
    )

    sdk = ElevenLabs(api_key=...)
    manager = build_agent_manager(
        client=build_elevenlabs_client(sdk=sdk),
        paths=ConfigPaths.for_root(Path.cwd()),
    )
    report = manager.pull(agent_id=AgentId("agent_..."))
"""

from guidepoint.agent._client import build_elevenlabs_client
from guidepoint.agent._io import ConfigPaths
from guidepoint.agent._models import (
    AgentConfig,
    AgentConfigInvalidError,
    AgentError,
    AgentId,
    AgentNotFoundError,
    ElevenLabsClient,
    PhoneNumberId,
    ToolDef,
    ToolHttpMethod,
    ToolId,
    ToolMock,
    ToolParameter,
)
from guidepoint.agent._protocol import AgentManager, PullReport
from guidepoint.agent._service import build_agent_manager, validate_config
from guidepoint.agent._variable_audit import (
    AuditIssue,
    AuditReport,
    IssueLevel,
    audit_files,
    audit_prompt_variables,
)

__all__ = [
    "AgentConfig",
    "AgentConfigInvalidError",
    "AgentError",
    "AgentId",
    "AgentManager",
    "AgentNotFoundError",
    "AuditIssue",
    "AuditReport",
    "ConfigPaths",
    "ElevenLabsClient",
    "IssueLevel",
    "PhoneNumberId",
    "PullReport",
    "ToolDef",
    "ToolHttpMethod",
    "ToolId",
    "ToolMock",
    "ToolParameter",
    "audit_files",
    "audit_prompt_variables",
    "build_agent_manager",
    "build_elevenlabs_client",
    "validate_config",
]
