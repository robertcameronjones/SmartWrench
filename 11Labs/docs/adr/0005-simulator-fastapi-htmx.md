# ADR 0005: Simulator app — FastAPI + vanilla HTML/JS, no React

- **Status:** Accepted
- **Date:** 2026-05-10
- **Owner:** Rob Jones
- **Reviewers:** —

## Context

We need an operator console to:

1. Edit and save Case fixtures (`fixtures/cases/*.json`) from a UI.
2. Trigger an interaction (voice today, SMS later) against the configured
   ElevenLabs agent.
3. Watch the call/message progress live: connection health, channel,
   workflow state, and a streaming log of every event.

The mockup phase needs to be visually faithful but must not place real
ElevenLabs calls; the wire-up phase swaps the trigger implementation
without touching the UI.

## Decision

- **Backend:** FastAPI on uvicorn. Pydantic-native, async-native, our
  existing `Case`/`AgentConfig` models become HTTP schemas for free,
  WebSocket support is built in.
- **Frontend:** Server-rendered Jinja2 HTML + vanilla JavaScript. No
  build step, no Node, no npm. One CSS file, one JS file. The JS uses
  `fetch` for HTTP and a single `WebSocket` for the live log feed.
- **Module placement:** `src/guidepoint/simulator/`, with the same
  encapsulation rules as every other module
  (`_underscore` privates, `__all__` in `__init__.py`, Protocol +
  factory for every swap point).
- **Mockup-vs-wire-up seam:** every external boundary is a
  `Protocol` with a real and a stub implementation. Today the trigger
  service is `_StubTriggerService` (replays a scripted event sequence)
  and the connection probe is `_EnvConnectionProbe` (reads env vars
  only). The wire-up phase adds a real implementation behind each
  Protocol and the route layer is unchanged.
- **SMS:** the channel toggle is rendered but disabled with a "soon"
  badge. We have not wired ElevenLabs SMS / Twilio yet; making the gap
  visible beats hiding it.

## Alternatives considered

- **React (or any SPA framework).** Would have added Node, npm, a
  bundler, a separate build/deploy story, and at least one test runner.
  The simulator is one page with a form and a streaming log; React's
  weight is not justified at this stage. If the simulator grows into a
  multi-tab operator suite later, a rewrite is a weekend, not a regret.
- **Pure HTMX.** Considered, but the live-log + status-pulse pattern
  is naturally a WebSocket stream feeding a small piece of stateful UI.
  Vanilla JS with a single WS handler stays cleaner than HTMX SSE
  swaps for this case. We can adopt HTMX later for non-streaming forms
  without breaking anything.
- **TUI (Textual / Rich).** Faster to build but undemonstrable to
  dealers/stakeholders. Browser wins on shareability.
- **Streamlit / Gradio.** Quicker prototype, but each one drags in its
  own opinionated runtime, breaks the architectural rules
  (no Protocol seams, no Pydantic boundary models, no structured
  logging integration), and is a worse fit for the eventual production
  shape.

## Consequences

- New runtime dependencies: `fastapi`, `uvicorn[standard]`, `jinja2`.
  New dev dependency: `httpx` (for `fastapi.testclient`).
- Static assets ship inside the package
  (`src/guidepoint/simulator/static/`,
  `src/guidepoint/simulator/templates/`) via
  `[tool.setuptools.package-data]`.
- Coverage gate excludes `src/guidepoint/simulator/__main__.py`
  (uvicorn entry point — exercised by hand, not tests).
- The wire-up phase will introduce two new private modules
  (`_real_trigger.py`, `_live_probe.py`) implementing the same
  Protocols. No public surface change.
