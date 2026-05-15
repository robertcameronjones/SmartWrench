"""CLI launcher: ``python -m simulator``.

Loads ``.env`` so ``ELEVENLABS_API_KEY`` etc. are visible to the probe,
configures structured logging, and hands the composed app off to
uvicorn. Pure orchestration -- no business logic here.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from guidepoint.observability import bind_context, configure_logging
from simulator._app import build_app

# Each sibling tool keeps its own .env (sms/.env, llm/.env, 11Labs/.env)
# so the CLI inside that folder works standalone. The simulator process
# needs all three because it runs all three tools in one process. We
# look in the project_root itself (e.g. 11Labs/.env when --project-root
# is the 11Labs folder) and at every sibling tool one level up
# (newmaintenance/sms/.env, newmaintenance/llm/.env, ...). Files that
# don't exist are silently skipped.
_SIBLING_TOOL_DIRS = ("11Labs", "sms", "llm")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="simulator")
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8000, help="bind port")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="directory containing config/ and fixtures/",
    )
    parser.add_argument(
        "--log-json",
        action="store_true",
        help="emit JSON log lines (default: human-readable)",
    )
    return parser.parse_args(argv)


def _load_env(project_root: Path) -> None:
    """Load .env from project_root and from every sibling tool one level up."""
    workspace_root = project_root.parent
    candidates = [project_root / ".env", workspace_root / ".env"]
    for sibling in _SIBLING_TOOL_DIRS:
        candidates.append(workspace_root / sibling / ".env")
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve() if candidate.exists() else candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def main(argv: list[str] | None = None) -> None:
    """Entry point used by ``python -m simulator``."""
    args = _parse_args(argv)
    _load_env(args.project_root)
    configure_logging(level=logging.INFO, json=args.log_json)
    bind_context(component="simulator")
    app = build_app(project_root=args.project_root)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
