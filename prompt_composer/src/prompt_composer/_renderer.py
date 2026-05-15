"""Substitution engine. Pure. No I/O.

Finds every ``{{key}}`` in the concatenated template, looks ``key`` up in
the variables dict, and substitutes. Raises ``MissingPlaceholderError`` if
any placeholder is unresolved — silent fallback would let the LLM speak
"{{customer_first_name}}" out loud.
"""

from __future__ import annotations

import re
from typing import final

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


@final
class MissingPlaceholderError(KeyError):
    """A ``{{placeholder}}`` in the template has no matching key in variables."""

    def __init__(self, missing: frozenset[str]) -> None:
        self.missing = missing
        ordered = sorted(missing)
        super().__init__(
            f"Prompt template references {len(missing)} placeholder(s) "
            f"not present in case.to_variables(): {ordered}"
        )


def find_placeholders(text: str) -> frozenset[str]:
    """Return every ``{{key}}`` referenced in ``text`` (key only, not braces)."""
    return frozenset(_PLACEHOLDER_RE.findall(text))


def substitute(text: str, variables: dict[str, str]) -> tuple[str, frozenset[str]]:
    """Replace every ``{{key}}`` with ``variables[key]``.

    Returns the substituted text and the frozenset of placeholders actually
    used. Raises :class:`MissingPlaceholderError` if any placeholder is
    referenced but missing from ``variables``.
    """
    used = find_placeholders(text)
    missing = used - variables.keys()
    if missing:
        raise MissingPlaceholderError(frozenset(missing))

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return variables[key]

    return _PLACEHOLDER_RE.sub(_replace, text), used


__all__ = [
    "MissingPlaceholderError",
    "find_placeholders",
    "substitute",
]
