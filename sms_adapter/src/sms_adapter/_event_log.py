"""Flat append-only event log.

Format, one event per write:

    <iso8601 utc timestamp> [<source>] <content>\\n

Sources used today:
    us->llm      — full prompt + history sent to the LLM
    llm->us      — reply received from the LLM
    us->sms      — outbound text we handed to Twilio
    sms->us      — inbound text Twilio handed to us

Multi-line content (e.g. the system prompt) is written as-is with
embedded newlines. ``grep '\\[us->sms\\]'`` still works to pull just
the outbound texts; for multi-line entries grep prints just the header
line and you ``less`` the file for the body.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


def log_event(path: Path | None, source: str, content: str) -> None:
    """Append one event to ``path``. No-op if ``path`` is None."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).isoformat(timespec="milliseconds")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{ts} [{source}] {content}\n")


__all__ = ["log_event"]
