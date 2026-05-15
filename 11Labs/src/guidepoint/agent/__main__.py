"""CLI entry point for the agent module.

Run with::

    python -m guidepoint.agent pull --agent agent_xxx
    python -m guidepoint.agent validate

Output goes to ``sys.stdout`` / ``sys.stderr`` rather than via ``print`` to
respect the project's ban on ``print`` (Ruff ``T20``).

The CLI is structured so the SDK construction is the only thing the entry
point pulls from the outside world. ``main`` accepts an injectable
``manager_factory`` so tests can drive the dispatch logic without touching
network or environment.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import structlog
from dotenv import load_dotenv

from guidepoint.agent._io import ConfigPaths
from guidepoint.agent._models import (
    AgentConfigInvalidError,
    AgentError,
    AgentId,
    ElevenLabsClient,
)
from guidepoint.agent._protocol import AgentManager
from guidepoint.agent._service import build_agent_manager, validate_config
from guidepoint.agent._variable_audit import audit_files
from guidepoint.observability import bind_context, configure_logging

_log = structlog.get_logger(__name__)

_EXIT_OK = 0
_EXIT_USAGE = 2
_EXIT_AGENT_ERROR = 3
_EXIT_AUDIT_FAILURE = 4

ManagerFactory = Callable[[ConfigPaths], AgentManager]


def main(
    argv: tuple[str, ...] | None = None,
    *,
    manager_factory: ManagerFactory | None = None,
) -> int:
    """Parse argv and dispatch a subcommand. Returns the process exit code.

    Args:
        argv: command-line arguments (defaults to ``sys.argv[1:]``).
        manager_factory: optional override that builds an ``AgentManager``
            given a ``ConfigPaths``. Used by tests to inject a fake client.
            Defaults to building a real ElevenLabs-backed manager.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    paths = ConfigPaths.for_root(Path(args.project_root).resolve())

    configure_logging(json=args.log_format == "json")
    bind_context(
        correlation_id=str(uuid.uuid4()),
        command=args.command,
    )
    _log.info("agent.cli.started", project_root=str(paths.config_dir.parent))

    if args.command == "pull":
        return _cmd_pull(
            paths=paths,
            agent_id_arg=args.agent,
            manager_factory=manager_factory,
        )
    if args.command == "validate":
        return _cmd_validate(paths=paths)
    if args.command == "check-prompt":
        return _cmd_check_prompt(paths=paths, strict=args.strict)
    parser.print_help(sys.stderr)
    return _EXIT_USAGE


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m guidepoint.agent",
        description="Manage the local source-of-truth for an ElevenLabs agent.",
    )
    parser.add_argument(
        "--project-root",
        default=str(Path.cwd()),
        help="Project root containing config/ and fixtures/ (default: cwd)",
    )
    parser.add_argument(
        "--log-format",
        choices=("pretty", "json"),
        default="pretty",
        help="Log output format (default: pretty for terminals, json for aggregators).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pull = sub.add_parser("pull", help="Fetch live agent config from ElevenLabs to disk.")
    pull.add_argument(
        "--agent",
        default=None,
        help="Agent id (defaults to ELEVENLABS_AGENT_ID in .env).",
    )

    sub.add_parser("validate", help="Validate the on-disk config without contacting ElevenLabs.")

    check = sub.add_parser(
        "check-prompt",
        help=(
            "Audit the system prompt's variable namespace against agent.toml "
            "defaults and Case.to_variables. Exits non-zero on errors."
        ),
    )
    check.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings (unused defaults, unused Case keys) as failures.",
    )
    return parser


def _cmd_pull(
    *,
    paths: ConfigPaths,
    agent_id_arg: str | None,
    manager_factory: ManagerFactory | None,
) -> int:
    load_dotenv()
    raw_id = agent_id_arg or os.environ.get("ELEVENLABS_AGENT_ID")
    if not raw_id:
        _stderr("Provide --agent or set ELEVENLABS_AGENT_ID in .env.")
        return _EXIT_USAGE
    agent_id = AgentId(raw_id)

    factory = manager_factory or _default_manager_factory
    try:
        manager = factory(paths)
    except _MissingApiKeyError:
        _stderr("ELEVENLABS_API_KEY is not set (check .env).")
        return _EXIT_USAGE

    try:
        report = manager.pull(agent_id=agent_id)
    except AgentError as exc:
        _log.error("agent.pull.failed", agent_id=agent_id, error=str(exc))
        _stderr(f"Pull failed: {exc}")
        return _EXIT_AGENT_ERROR

    _stdout(f"Pulled agent {report.agent_id}")
    _stdout(f"  config:        {report.config_path}")
    _stdout(f"  system prompt: {report.system_prompt_path}")
    _stdout(f"  tools:         {len(report.tools)}")
    for tool in report.tools:
        _stdout(f"    - {tool.name} ({tool.tool_id})")
    return _EXIT_OK


def _cmd_validate(*, paths: ConfigPaths) -> int:
    try:
        validate_config(paths=paths)
    except AgentConfigInvalidError as exc:
        _stderr(str(exc))
        return _EXIT_AGENT_ERROR
    _stdout("Agent config is valid.")
    return _EXIT_OK


def _cmd_check_prompt(*, paths: ConfigPaths, strict: bool) -> int:
    """Run the variable-namespace audit and report findings.

    Always prints both errors and warnings. Process exit code:

    - ``0`` — no errors (and no warnings, when ``--strict``).
    - ``_EXIT_AUDIT_FAILURE`` — at least one error (or any warning when
      ``--strict``).
    """
    report = audit_files(paths=paths)
    for issue in report.issues:
        _stderr(str(issue))
    if not report.issues:
        _stdout("Prompt variables: clean.")
        return _EXIT_OK
    failed = bool(report.errors) or (strict and bool(report.warnings))
    if failed:
        _stderr(f"Audit failed: {len(report.errors)} error(s), {len(report.warnings)} warning(s).")
        return _EXIT_AUDIT_FAILURE
    _stdout(f"Audit passed with {len(report.warnings)} warning(s).")
    return _EXIT_OK


# ---------------------------------------------------------------------------
# Default manager factory (the only place the SDK is constructed)
# ---------------------------------------------------------------------------


class _MissingApiKeyError(RuntimeError):
    """Raised when the SDK cannot be built because the API key is absent."""


def _default_manager_factory(paths: ConfigPaths) -> AgentManager:
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise _MissingApiKeyError
    client = _build_real_client(api_key=api_key)
    return build_agent_manager(client=client, paths=paths)


def _build_real_client(*, api_key: str) -> ElevenLabsClient:
    # Imported lazily so the validate command (which never needs the SDK)
    # does not pay the import cost — and so tests that pass their own
    # ``manager_factory`` can avoid the SDK entirely.
    from elevenlabs.client import ElevenLabs  # noqa: PLC0415

    from guidepoint.agent._client import build_elevenlabs_client  # noqa: PLC0415

    return build_elevenlabs_client(sdk=ElevenLabs(api_key=api_key))


def _stdout(line: str) -> None:
    sys.stdout.write(line + "\n")


def _stderr(line: str) -> None:
    sys.stderr.write(line + "\n")


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
