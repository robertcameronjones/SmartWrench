"""Structured logging configuration for the Guidepoint backend.

Per ARCHITECTURE rule #15 the project standardizes on ``structlog`` with a
``correlation_id`` threaded through every event. This module is the single
place that configures the library — every other module imports
``structlog.get_logger`` and emits events; nobody else touches processors
or handlers.

The module exposes plain functions rather than a Protocol/factory pair
because logging configuration is a process-level concern (pure side
effect, no per-instance state to inject). The architecture rule about
"Protocol + factory" applies to *services* — runtime behavior we might
swap. Logging configuration has nothing to swap.
"""

from __future__ import annotations

import logging
import sys

import structlog

_DEFAULT_LEVEL = logging.INFO


def configure_logging(*, level: int = _DEFAULT_LEVEL, json: bool = False) -> None:
    """Configure ``structlog`` for the running process.

    Idempotent: calling more than once replaces the previous configuration
    rather than stacking it. Tests that need a clean slate may call this
    again with their own settings.

    Args:
        level: stdlib logging level (e.g. ``logging.DEBUG``).
        json: if ``True`` emit one JSON object per line (good for log
            aggregators); if ``False`` emit human-readable colored output
            (good for an interactive terminal).
    """
    # Bridge structlog through stdlib ``logging`` so anything that captures
    # stderr (pytest's ``capsys``, container log drivers, etc.) sees the
    # same stream. ``force=True`` lets repeated configure_logging calls win.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=level,
        force=True,
    )

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def bind_context(**kwargs: object) -> None:
    """Bind key/value pairs to every subsequent log event in this context.

    Thin wrapper over ``structlog.contextvars.bind_contextvars`` so callers
    don't have to know which structlog submodule the context lives in.
    Typical use is to bind ``correlation_id`` once at the entry point
    (CLI ``main``, FastAPI request middleware, etc.) and let it propagate.
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_context() -> None:
    """Drop all bound context vars. Use between tests."""
    structlog.contextvars.clear_contextvars()
