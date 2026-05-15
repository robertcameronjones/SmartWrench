# ADR 0001: Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-05-09
- **Owner:** Rob Jones
- **Reviewers:** —

## Context

Architectural choices made now (framework, error model, encapsulation
strategy, vendor selection) will shape every line of code written later. We
want to:

- Avoid relitigating decided things every few weeks.
- Onboard new contributors (human or AI) quickly with a single source of
  recorded decisions.
- Have a record of *why* we picked X over Y, so when X's trade-offs hurt us
  later we know what we accepted and can revisit deliberately.

## Decision

We use Architecture Decision Records (ADRs) following Michael Nygard's format,
stored as Markdown in `docs/adr/`, numbered `NNNN-kebab-title.md`. New ADRs
copy `docs/adr/0000-template.md`.

A decision is ADR-worthy if it would be expensive to reverse or if a future
contributor would reasonably wonder *"why did they do it this way?"*.
Examples:

- Choice of web framework
- Choice of typing strictness, error model, async runtime
- Encapsulation enforcement strategy
- Vendor selection (telephony provider, LLM provider, telemetry backend)
- Persistence layer
- Auth model

Decisions that are *not* ADR-worthy:

- Code style (covered by ruff)
- Variable names
- Choice of test fixture file format

## Alternatives considered

- **No formal record.** Rejected: relitigation tax compounds; tribal knowledge
  evaporates when contributors rotate.
- **Confluence / Notion.** Rejected: lives outside the repo, drifts from
  reality, requires another login, can't be diffed alongside code changes.
- **Inline comments / commit messages.** Rejected: not discoverable,
  fragmented across hundreds of locations.

## Consequences

- Positive: durable, diffable, reviewable record of architectural intent.
- Positive: PRs that introduce architectural changes must include an ADR,
  enforcing thoughtfulness.
- Negative: small overhead per decision (writing the ADR).
- Neutral: requires team discipline to actually write them; review checklist
  enforces this.

## References

- [Michael Nygard, *Documenting Architecture Decisions*](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
- `docs/ARCHITECTURE.md`
