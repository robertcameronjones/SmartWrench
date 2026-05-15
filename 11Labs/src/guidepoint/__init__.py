"""Guidepoint Systems vehicle service scheduling agent backend.

This package intentionally exposes nothing at the top level. Each subpackage
(scheduling, telematics, agent, dealers, ...) defines its own public Protocol
and factory in its own ``__init__.py``. See ``docs/ARCHITECTURE.md``.
"""

__all__: list[str] = []
