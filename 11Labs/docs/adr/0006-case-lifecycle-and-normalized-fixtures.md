# ADR 0006: Case lifecycle, Case Manager, normalized fixtures, A/B repository seam

- **Status:** Accepted
- **Date:** 2026-05-10
- **Owner:** Robert Jones
- **Reviewers:** —

## Context

Through ADR 0001–0005 the codebase grew a single denormalized `Case`
fixture under `fixtures/cases/` that bundled customer, dealer, vehicle,
and service-event data into one file. This shape had
two structural problems:

1. **It violated SPOT.** Customer details would live both in the case
   fixture and (eventually) in MySQL. Two copies, two ways for them to
   drift, no way to tell which one was authoritative.
2. **It conflated two distinct lifecycles.** A "case" was simultaneously
   the trigger ("call this customer about that vehicle"), the running
   call attempt ("Kate is dialing"), and the durable audit record
   ("the customer booked Tuesday at 1:30"). One record cannot honestly
   be all three.

In production the customer/dealer/vehicle data lives in MySQL, the
trigger queue lives in another table polled by a monitor task, and
each call placed produces an immutable audit row. The simulator needs
the same separation so the swap from JSON-fixture mode to MySQL mode
is mechanical.

We also need a single, auditable seam to ElevenLabs: nothing else in
the codebase should be able to dial a number directly.

## Decision

We split the existing `agent.Case` into three orthogonal concepts and
formalize each:

1. **Master data** lives in `guidepoint.master_data`:
   `CustomerRecord`, `DealerRecord`, `VehicleRecord` (PK = VIN, with
   `owner_id` FK to customer). Loaded through a
   `MasterDataRepository` Protocol. The simulator gets the JSON-file
   implementation; production gets MySQL behind the same Protocol.

2. **Triggers** live in `guidepoint.case._trigger_source`. A
   `Trigger` is a stimulus carrying foreign keys to master data plus
   the service event, channel preference, and pre-fetched slots. A
   `TriggerSource` Protocol exposes `pending() / get / mark_fired /
   mark_failed / save`. The simulator gets the JSON-file implementation;
   production gets the cloud-DB poller behind the same Protocol.

3. **Cases** live in `guidepoint.case`. A `Case` is created **only**
   when the case manager fires a trigger. It carries a frozen snapshot
   of customer/dealer/vehicle (full record copies, not FK references)
   plus a state machine (`CaseState`), an attempt history
   (`CallAttempt`), and an audit log (`CaseEvent`). Cases are
   never hand-authored.

4. **`CaseManager`** is the only public entry point that places a call
   to ElevenLabs. It builds the case, persists it through
   `CaseRepository`, delegates the actual call to a `CallSession`, and
   walks the case through its terminal state based on the
   `CallOutcome` returned. The state-decision is a pure function
   (`_decide_terminal_state`) for property testing.

5. **`CallSession`** is the ElevenLabs adapter, intentionally **not
   exported** from `guidepoint.case.__init__`. It is an implementation
   detail of the manager. Outside callers cannot reach it. The
   mockup-phase implementation (`_StubCallSession`) replays a
   scripted event sequence; the future live implementation will call
   the real SDK behind the same Protocol.

6. **Retry policy** is encapsulated in `RetryPolicy` (today: single-shot,
   `max_attempts=1`). When we add multi-attempt policies, only the
   pure decision function changes.

7. **Cross-cutting utilities move to top-level modules**:
   `guidepoint.clock` (the `Clock` Protocol) and `guidepoint.events`
   (a generic `EventBus[T]`). Both `master_data` and `case` depend on
   them; nothing depends back on `simulator`. The bus is generic so
   the events module has no domain dependency — the case manager
   parameterizes it as `EventBus[CaseEvent]`.

8. **Fixtures are normalized**: one JSON file per row, one directory
   per entity (`fixtures/customers/`, `fixtures/dealers/`,
   `fixtures/vehicles/`, `fixtures/triggers/`, `fixtures/cases/`).
   Filenames are the primary key. The old denormalized case fixture
   is deleted.

The simulator's UI is rewritten to match: pick a trigger, edit the
linked records (customer/dealer/vehicle/trigger) inline through PUT
endpoints, fire the trigger via `POST /api/fire`, and watch the case
unfold over `/ws/log`.

## Alternatives considered

- **Keep the denormalized `Case` and add a `_metadata` block.**
  Rejected: this would have made the SPOT violation explicit but not
  fixed it. We would still have to reconcile case-snapshot fields with
  MySQL rows on every read.

- **Make `CallSession` part of the public API and let callers (the
  simulator, tests) place calls directly.** Rejected: that would
  reintroduce the same problem the case manager solves — multiple
  paths to ElevenLabs, no single audit point. The case manager is
  the SPOT for ElevenLabs traffic.

- **Single state machine spanning case + call.** Rejected: the case
  state (`booked`, `unreachable`, …) is a business outcome that
  outlives any one call attempt. The call state (`dialing`,
  `connected`, `ended`) lives only as long as the call does. Two
  state machines, mirrored at the boundary by `CallSession`, lets us
  add multi-attempt logic without touching call-level code.

- **Keep `EventBus` typed to a single payload (`CaseEvent`).**
  Rejected: doing so created a circular import between `events` and
  `case`. A generic `EventBus[T]` parameterized at construction time
  is one extra annotation per consumer and breaks the cycle cleanly.

- **A "live" mode flag toggled by env var inside the case manager.**
  Rejected: the swap point is the `CallSession` (and the
  `MasterDataRepository`, and the `TriggerSource`). The case manager
  is the same code in either mode. Each Protocol is the seam for its
  own A/B switch.

## Consequences

**Positive:**

- One concept, one model. `CustomerRecord` exists in exactly one
  place (`master_data._models`); the case carries a *snapshot copy*,
  not a duplicate definition.
- Going to MySQL is mechanical: write a `_MysqlMasterDataRepository`,
  wire it into the factory, delete the JSON repo. No business code
  changes.
- The case manager is the only path to ElevenLabs. Auditing,
  rate-limiting, retry policy, observability — all single-place.
- Cases are immutable audit objects in a meaningful sense. Re-reading
  a case six months from now shows what Kate was actually told,
  regardless of subsequent customer record edits.
- The simulator already exercises the same code path production will
  run. The only thing the live deployment swaps is the `CallSession`.

**Negative:**

- The trigger fixture has to keep its FKs in sync with the master-data
  fixtures by hand. The case manager's foreign-key check
  (`TriggerForeignKeyError`) catches this fast, but it is now a real
  authoring concern.
- One more directory level for fixture files (now five per entity).
  The simulator's repository layer makes this transparent at runtime.
- `CallSession` cannot be exercised end-to-end from outside the case
  module — tests must go through the case manager. This is intentional
  but does mean stub-call-session tests live under `tests/case/` only.

**Neutral / new constraints:**

- Every new domain module must follow the same shape: `_models.py`,
  `_repository.py` (Protocol + JSON impl), `__init__.py` re-exporting
  the public surface, no underscore-prefixed names crossing the
  boundary.
- Any future additional ElevenLabs touch point (e.g. webhook receivers
  for tool calls) must route through the case manager. The architecture
  rule is enforced by the unimportability of `CallSession`.

## References

- Code: `src/guidepoint/master_data/`, `src/guidepoint/case/`,
  `src/guidepoint/clock/`, `src/guidepoint/events/`,
  `src/guidepoint/simulator/_routes.py`.
- Docs: `docs/data-dictionary.md` (rewritten for the new shape).
- Prior: ADR 0002 (encapsulation), ADR 0004 (JSON everywhere),
  ADR 0005 (simulator stack).
