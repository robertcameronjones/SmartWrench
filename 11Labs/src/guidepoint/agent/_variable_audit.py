"""Static audit of the prompt-variable namespace.

This module exists for **one** purpose: prove that every ``{{variable}}``
referenced in ``config/system-prompt.md`` is resolvable from
``Case.to_variables()`` (per-call payload), and surface the inverse —
Case keys we generate that the prompt never reads.

The rule lives here once. ``validate_config`` and the ``check-prompt``
CLI subcommand both delegate to ``audit_files``. If you need to change
the rule, change it here; nowhere else.

Per ADR 0004 there is no separate "dashboard defaults" set; the Case
fixture is the single source of truth for runtime variable values.

The core function ``audit_prompt_variables`` is pure: it takes two
``frozenset``s and returns an ``AuditReport``. ``audit_files`` is the
file-system orchestrator that loads the inputs from disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, final, override

from guidepoint.agent._io import (
    ConfigPaths,
    extract_variable_names,
    read_system_prompt,
)
from guidepoint.case import Case

IssueLevel = Literal["error", "warning"]


@final
@dataclass(frozen=True, slots=True)
class AuditIssue:
    """One finding from the variable audit.

    ``error``: the prompt will leak ``{{var}}`` text at runtime — must fix.
    ``warning``: hygiene problem (dead default, unused Case key) — should fix.
    """

    level: IssueLevel
    message: str

    @override
    def __str__(self) -> str:
        return f"[{self.level}] {self.message}"


@final
@dataclass(frozen=True, slots=True)
class AuditReport:
    """Result of a single audit run.

    ``errors`` and ``warnings`` are pre-split for callers that want to
    branch on severity without re-filtering.
    """

    issues: tuple[AuditIssue, ...]

    @property
    def errors(self) -> tuple[AuditIssue, ...]:
        return tuple(i for i in self.issues if i.level == "error")

    @property
    def warnings(self) -> tuple[AuditIssue, ...]:
        return tuple(i for i in self.issues if i.level == "warning")

    @property
    def ok(self) -> bool:
        """True when there are no errors. Warnings do not flip this."""
        return not self.errors


def audit_prompt_variables(
    *,
    prompt_body: str,
    case_keys: frozenset[str],
) -> AuditReport:
    """Pure variable-namespace audit. No I/O.

    Two checks:

    1. **error** — every ``{{var}}`` in the prompt resolves to a Case
       key. Anything else would leak literal ``{{var}}`` text into the
       conversation at runtime.
    2. **warning** — every Case key is referenced by the prompt.
       Sometimes intentional (e.g. ``case_id`` is for logs, not Kate),
       so this is a warning, not an error.

    Args:
        prompt_body: The full system prompt as written.
        case_keys: ``frozenset`` of keys ``Case.to_variables()`` produces.

    Returns:
        ``AuditReport`` with a sorted, deterministic issue list.
    """
    referenced = extract_variable_names(prompt_body=prompt_body)

    issues: list[AuditIssue] = []
    issues.extend(
        AuditIssue(
            level="error",
            message=(f"prompt references {{{{{name}}}}} but it is not a key in Case.to_variables"),
        )
        for name in sorted(referenced - case_keys)
    )
    issues.extend(
        AuditIssue(
            level="warning",
            message=(f"Case.to_variables produces '{name}' but the prompt does not reference it"),
        )
        for name in sorted(case_keys - referenced)
    )
    return AuditReport(issues=tuple(issues))


def audit_files(*, paths: ConfigPaths) -> AuditReport:
    """File-system orchestrator: load the prompt, run the audit.

    The Case key set comes from ``Case.variable_keys()`` — a property
    of the schema, not of any one fixture, so no fixture is read.
    """
    return audit_prompt_variables(
        prompt_body=read_system_prompt(paths=paths),
        case_keys=Case.variable_keys(),
    )
