# Architecture Guidelines

> **Read this before starting any module. Re-read it weekly.**
>
> This is not an AI-slop project. We have rules. We re-read them. We follow them
> religiously. When AI (or anyone) drifts from the rules, the PR fails CI.

---

## Core principles

### 1. Separation of concerns
We don't put code where it's easy. We put it where it belongs. One concept per
file. One responsibility per module. If a file mixes two concerns, split it.

### 2. Single Point of Truth (SPOT)
Every fact has exactly one owner. Calling systems read straight from the source.
*The source of truth must own its writes.* Reads can be cached. Writes never are.

### 3. One locus of control
The system has exactly one event-driven state machine. **Nothing happens unless
it derives from a state-entry function.** State is serializable. Transitions are
logged. Conversation history is replayable from the event log alone.

### 4. Defined and restricted interfaces
Every module exposes a `Protocol` and a factory. Implementations are
unimportable from outside the module — Pyright `reportPrivateUsage = error`
fails the build if you try.

### 5. APIs have abstraction layers
External systems (ElevenLabs, Twilio, dealer DMS, telematics bus) hide behind
adapters that implement our Protocols. We can swap any vendor without changing
core logic.

### 6. Encapsulation is enforced by tooling, not hope
Python doesn't have `private`. We simulate it with: `_underscore` naming +
`__all__` + `pyright --strict` + `reportPrivateUsage = error`. Violations fail
CI, not runtime.

### 7. The LLM is untrusted I/O
Every tool argument the LLM generates is validated against a Pydantic schema
before execution. Every text it emits that becomes an action (booking,
transfer, refund) is validated. Treat Kate's output the way you'd treat a form
submission from the open internet.

### 8. Time is an injected dependency
Never `datetime.now()` directly. The `Clock` Protocol is constructor-injected.
All times are UTC internally. Conversion to local zone happens at the
rendering boundary only.

### 9. Functional core, imperative shell
Business logic is pure functions over immutable data. I/O (HTTP, DB, LLM,
telephony) lives at the edges. Inner code never knows the difference between
a real adapter and a fake one.

### 10. Errors are typed values for expected failures
A `Result[Slot, BookingError]` is honest. A function that "might" throw five
different exceptions because anything could happen is lying. Exceptions are
for bugs, not control flow.

### 11. Configuration is one frozen object validated at startup
One `Settings` class. Loaded once. Frozen. If config is bad, the process
refuses to start with a clear error. No `os.getenv` scattered around.

### 12. No globals. No singletons. No module-level mutable state.
Dependencies are constructor-injected. The `Settings`, the `Clock`, the
`SchedulingService` — all passed in. This is what makes the system testable
without monkey-patching.

### 13. Async correctness is non-optional
Every awaitable has a timeout. Every concurrent group is structured
(`asyncio.TaskGroup` or `anyio`). No fire-and-forget tasks. Background work
goes to a real worker, not `asyncio.create_task` and a prayer.

### 14. Validate at boundaries, simplify in the middle
Pydantic at every external boundary (HTTP in/out, webhook payloads, file
parses, LLM tool args). Inside the system, plain frozen dataclasses — they're
faster and lower ceremony.

### 15. Observability is a feature, not an afterthought
Structured logs (`structlog`) with a `correlation_id` threaded through every
call. One id per Kate conversation, propagated to every tool, every DB write,
every external call. OpenTelemetry traces the same id. `print()` is banned.

### 16. Tests carry the same weight as code
Pytest. Hypothesis for property tests on the state machine. Snapshot tests
(`syrupy`) for prompt files and tool I/O. Coverage gate at ≥90% for `src/`.
No exceptions for "trivial" code.

### 17. One concept per file. Files cap at ~250 lines.
Cyclomatic complexity capped (`ruff C901`). Long files signal collapsed
concerns.

### 18. No magic
No metaclass tricks. No monkey-patching. No "import this and it registers
itself" side effects. Imports at the top. Executable code only inside
functions called explicitly.

### 19. Composition over inheritance
Protocols for capabilities. ABCs only when you genuinely need shared
behavior. Mixins are a smell.

### 20. Decisions are recorded
ADRs (Architecture Decision Records) live in `docs/adr/NNNN-title.md`. We
don't relitigate decided things.

---

## Tooling stack

**Static & style**
- `pyright --strict` — type checking, with `reportPrivateUsage = error`
- `ruff check` — linting (replaces flake8/pylint/isort/pyupgrade)
- `ruff format` — formatting (replaces black)

**Runtime**
- `pydantic` v2 — boundary validation
- `pydantic-settings` — config
- `structlog` — structured logging
- `opentelemetry-api` / `opentelemetry-sdk` — tracing
- `tenacity` — retry policies
- `returns` — `Result` / `Maybe` types
- `python-statemachine` — state machine (or hand-rolled)
- `anyio` — structured async

**Test**
- `pytest`, `pytest-asyncio`, `pytest-cov`
- `hypothesis` — property tests
- `syrupy` — snapshot tests
- `respx` — HTTP mocking

**Repo hygiene**
- `pre-commit` — hooks for ruff, pyright, secrets scanning
- `pip-audit` — vulnerability scanning
- GitHub Actions: `ruff` → `pyright` → `pytest` → coverage gate

---

## Module pattern (copy this for every new module)

```
src/guidepoint/<concept>/
├── __init__.py     # public surface only — Protocol, factory, public types
├── _models.py      # frozen dataclasses, errors
├── _service.py     # private implementation
└── (more _files)   # all underscore-prefixed
```

The `__init__.py` re-exports a tight `__all__` list. Anything else is invisible
to consumers. Pyright fails CI if a consumer reaches into `_service.py`
directly.

See `src/guidepoint/scheduling/` for the canonical example.

---

## Rituals

- **Re-read this file** before starting a new module. Out loud, not skimmed.
- **Every new module** starts with its `Protocol`, an empty `__init__.py`, and
  a failing test. Implementation comes after.
- **No PR merges** with a CI failure or a "we'll fix later" TODO. Fix it or
  open a tracked issue with a date.
- **New external dependency = ADR.** Adding `pandas` because it's convenient
  is a violation. State the need, alternatives considered, owner.
- **PR review checklist:**
  1. Pyright strict passes
  2. Ruff clean
  3. Coverage ≥ 90%
  4. New module has Protocol + factory + tests
  5. No globals, no `print`, no `datetime.now()`
  6. ADR if any architectural decision was made
