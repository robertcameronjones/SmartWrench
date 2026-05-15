"""Observability — public surface.

Single owner of ``structlog`` configuration. Other modules import
``structlog.get_logger`` directly to emit events; only this module
configures processors, levels, and renderers.
"""

from guidepoint.observability._logging import (
    bind_context,
    clear_context,
    configure_logging,
)

__all__ = [
    "bind_context",
    "clear_context",
    "configure_logging",
]
