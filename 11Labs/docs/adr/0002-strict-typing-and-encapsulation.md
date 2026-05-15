# ADR 0002: Strict typing and enforced encapsulation

- **Status:** Accepted
- **Date:** 2026-05-09
- **Owner:** Rob Jones
- **Reviewers:** —

## Context

Python has no language-level access control (`private`, `protected`) and no
compile step that enforces type contracts. For a system with multiple modules,
external integrations (telephony, LLM, dealer DMS), and an LLM-in-the-loop
trust boundary, "consenting adults" naming conventions are not enough. We need
discipline that fails the build, not just the code review.

## Decision

We adopt the following stack and rules:

1. **Pyright in strict mode** is the type-correctness gate. CI fails if
   `pyright --strict src/` reports any error.
2. **`reportPrivateUsage = "error"`** in Pyright config. A consumer that
   imports `_underscore` names from another module fails CI.
3. **Each module exposes only its `Protocol` and a factory** through its
   `__init__.py`. Implementation files are `_underscore`-prefixed.
4. **`__all__` is mandatory** in every `__init__.py`. Wildcard re-exports are
   banned.
5. **Boundary data is Pydantic v2 with `frozen=True, extra="forbid"`.**
   Internal data is `@dataclass(frozen=True, slots=True)`.
6. **No `from x import *`** (Ruff `F403`).
7. **No relative imports across packages** (Ruff `flake8-tidy-imports`).
8. **`@typing.final`** on classes that should not be subclassed.

## Alternatives considered

- **mypy instead of pyright.** Rejected: pyright is faster, has better
  inference, ships with the Pylance LSP many of us already use, and supports
  `reportPrivateUsage` natively.
- **`__double_underscore` name mangling for "private."** Rejected: trivially
  bypassed, hostile to debugging, and doesn't catch errors in CI.
- **Convention-only with code review.** Rejected: humans miss things; AI
  contributors miss things constantly. Tooling enforces what humans can't.
- **Cython / compiled extensions for true encapsulation.** Rejected: massive
  build complexity, slows iteration, gains aren't worth it for a service
  layer.
- **Switch primary language to Rust/Go.** Rejected for the orchestration
  layer (where iteration speed matters); reconsidered for any future hot path
  (real-time audio, etc.).

## Consequences

- Positive: encapsulation violations caught at PR-time, before merge.
- Positive: refactors are safer because the type checker catches downstream
  breakage.
- Positive: AI-generated code is held to the same standard as human-written
  code.
- Negative: stricter typing means more upfront ceremony (Protocols, factories,
  generic types). We accept this in exchange for production reliability.
- Negative: third-party libraries with weak types occasionally need `# type:
  ignore[...]` with a justification comment.

## References

- `docs/ARCHITECTURE.md`
- [Pyright configuration](https://github.com/microsoft/pyright/blob/main/docs/configuration.md)
- [PEP 544 — Protocols](https://peps.python.org/pep-0544/)
