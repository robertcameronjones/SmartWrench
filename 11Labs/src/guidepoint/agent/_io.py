"""File I/O for agent configuration.

Read and write the on-disk representation:

* ``config/agent.json`` — JSON, parsed/validated through ``AgentConfig``.
* ``config/system-prompt.md`` — markdown body, referenced from
  ``agent.json``'s ``system_prompt_path``.
* ``config/tools/<name>.json`` — one file per tool, parsed through
  ``ToolDef``.

Per ADR 0006 the per-call ``Case`` payload moved to
``guidepoint.case`` and is no longer hand-authored as a fixture; this
module no longer reads cases. Per ADR 0004, every human-authored config
file is JSON. The system prompt remains markdown — it is literal text,
not a payload. This module is the only place in the agent package that
touches the filesystem for config; the service layer takes a
``ConfigPaths`` and asks this module to read or write — it never opens
a file directly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import final

from guidepoint.agent._models import (
    AgentConfig,
    ToolDef,
)


@final
@dataclass(frozen=True, slots=True)
class ConfigPaths:
    """Resolved on-disk locations for one project.

    All paths are absolute. Construct via ``ConfigPaths.for_root`` so the
    layout stays consistent across the CLI, the tests, and any future
    automation.
    """

    config_dir: Path
    tools_dir: Path
    agent_json: Path
    system_prompt_md: Path

    @staticmethod
    def for_root(project_root: Path) -> ConfigPaths:
        """Build the standard layout under ``project_root``."""
        config_dir = (project_root / "config").resolve()
        return ConfigPaths(
            config_dir=config_dir,
            tools_dir=config_dir / "tools",
            agent_json=config_dir / "agent.json",
            system_prompt_md=config_dir / "system-prompt.md",
        )

    def tool_path(self, *, tool_name: str) -> Path:
        """Path for one tool's config file. Filename is sanitized."""
        return self.tools_dir / f"{_safe_filename(tool_name)}.json"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read_agent_config(*, paths: ConfigPaths) -> AgentConfig:
    """Load ``config/agent.json`` through ``AgentConfig`` validation."""
    return AgentConfig.model_validate_json(_read_text(paths.agent_json))


def read_tool(*, path: Path) -> ToolDef:
    """Load one tool JSON file through ``ToolDef`` validation."""
    return ToolDef.model_validate_json(_read_text(path))


def read_all_tools(*, paths: ConfigPaths) -> tuple[ToolDef, ...]:
    """Load every ``config/tools/*.json`` file, sorted by filename."""
    if not paths.tools_dir.exists():
        return ()
    files = sorted(paths.tools_dir.glob("*.json"))
    return tuple(read_tool(path=p) for p in files)


def read_system_prompt(*, paths: ConfigPaths) -> str:
    """Read the markdown system prompt body."""
    return paths.system_prompt_md.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def write_agent_config(*, paths: ConfigPaths, config: AgentConfig) -> None:
    """Write ``config/agent.json`` from a validated ``AgentConfig``."""
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    _write_json(paths.agent_json, config.model_dump(mode="json"))


def write_system_prompt(*, paths: ConfigPaths, body: str) -> None:
    """Write the markdown system prompt, ensuring trailing newline."""
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    text = body if body.endswith("\n") else body + "\n"
    paths.system_prompt_md.write_text(text, encoding="utf-8")


def write_tool(*, paths: ConfigPaths, tool: ToolDef) -> Path:
    """Write one tool JSON file. Returns the path written."""
    paths.tools_dir.mkdir(parents=True, exist_ok=True)
    target = paths.tool_path(tool_name=tool.name)
    _write_json(target, tool.model_dump(mode="json"))
    return target


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_VARIABLE_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")


def extract_variable_names(*, prompt_body: str) -> frozenset[str]:
    """Return the set of ``{{name}}`` placeholders referenced in the prompt.

    Used by the variable-audit module to confirm every placeholder is
    produced by ``Case.to_variables``.
    """
    return frozenset(_VARIABLE_PATTERN.findall(prompt_body))


def _read_text(path: Path) -> str:
    if not path.exists():
        msg = f"Config file not found: {path}"
        raise FileNotFoundError(msg)
    return path.read_text(encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    """Pretty-print JSON with a trailing newline.

    Comments are not supported by JSON; per ADR 0004 the schema (Pydantic
    models) and the data dictionary carry that context instead. If a
    fixture truly needs a note, add a ``_comment`` string field at the
    top — Pydantic's ``extra="forbid"`` on real schemas means this only
    works for fixtures that explicitly opt in.
    """
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    path.write_text(text + "\n", encoding="utf-8")


def _safe_filename(name: str) -> str:
    """Sanitize a tool name into a filesystem-safe filename stem."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_")
    return cleaned or "unnamed_tool"
