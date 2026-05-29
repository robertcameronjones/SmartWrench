"""SMS opt-out / opt-in keyword detection (FCC-style).

Shared by the active-session loop and the inbound webhook so STOP /
START handling stays consistent whether or not a conversation is
currently running.
"""

from __future__ import annotations

_STOP_KEYWORDS: frozenset[str] = frozenset(
    {"STOP", "STOPALL", "UNSUBSCRIBE", "END", "QUIT"}
)
_START_KEYWORDS: frozenset[str] = frozenset({"START", "UNSTOP"})


def normalize_sms_body(body: str) -> str:
    """Trim and upper-case for keyword matching."""
    return body.strip().upper()


def is_opt_out_keyword(normalized: str) -> bool:
    """Return True when ``normalized`` is a STOP-style keyword."""
    return normalized in _STOP_KEYWORDS


def is_opt_in_keyword(normalized: str) -> bool:
    """Return True when ``normalized`` is a START / UNSTOP keyword."""
    return normalized in _START_KEYWORDS


__all__ = [
    "is_opt_in_keyword",
    "is_opt_out_keyword",
    "normalize_sms_body",
]
