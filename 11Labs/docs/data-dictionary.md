# Data Dictionary

> Every persisted file or wire payload that carries Guidepoint data, with its
> fields, types, owner, and validation rule. **The Pydantic models are the
> source of truth.** This document renders them in human-readable form.
> If a field changes here without a corresponding model change, the model wins.

Module map (after [ADR 0006](adr/0006-case-lifecycle-and-normalized-fixtures.md)):

| Domain | Module | What lives here |
|---|---|---|
| Agent (ElevenLabs config) | `guidepoint.agent` | `AgentConfig`, `ToolDef`, system-prompt audit |
| Master data (reference rows) | `guidepoint.master_data` | `CustomerRecord`, `DealerRecord`, `VehicleRecord`, `MasterDataRepository` |
| Case lifecycle | `guidepoint.case` | `Trigger`, `Case`, `CaseEvent`, `CallAttempt`, `CallOutcome`, `PostCallReport`, `CaseManager`, `CallSession` |
| Time | `guidepoint.clock` | `Clock` |
| Pub/sub | `guidepoint.events` | `EventBus[T]` |
| Operator UI | `simulator` (sibling project at `../simulator`) | FastAPI app, browser-boundary models |

---

## File map

Per [ADR 0004](adr/0004-json-everywhere.md), every human-authored file is JSON.
Per [ADR 0006](adr/0006-case-lifecycle-and-normalized-fixtures.md), the case
domain is normalized: one JSON file per row, one directory per entity, mirroring
the future MySQL/Redshift schema.

| Path | Format | Owner | Purpose |
|---|---|---|---|
| `config/agent.json` | JSON | `AgentConfig` | Local source of truth for the ElevenLabs agent's basic settings + attached tool ids. |
| `config/system-prompt.md` | Markdown | hand-authored | The system prompt body. References `{{variables}}` resolved at runtime by the Case payload. |
| `config/tools/<name>.json` | JSON | `ToolDef` | One file per webhook tool: name, description, URL, method, parameters, mocks. |
| `fixtures/customers/<customer_id>.json` | JSON | `CustomerRecord` | One file per customer (master data). |
| `fixtures/dealers/<dealer_id>.json` | JSON | `DealerRecord` | One file per dealer (master data). |
| `fixtures/vehicles/<vin>.json` | JSON | `VehicleRecord` | One file per vehicle (master data). VIN is the primary key. |
| `fixtures/triggers/<trigger_id>.json` | JSON | `Trigger` | One file per pending stimulus. The simulator's UI fires these. |
| `fixtures/cases/<case_id>.json` | JSON | `Case` | One file per case **created by the case manager**, never hand-authored. |
| `.env` | dotenv | manual | Secrets (API keys, agent ids). Never committed. |
| `pyproject.toml` | TOML | tooling | Project metadata + tooling config. Not "data". |

Wire payloads (no on-disk file, but they cross a boundary):

| Payload | Direction | Owner | Purpose |
|---|---|---|---|
| ElevenLabs `dynamic_variables` | us → ElevenLabs | `Case.to_variables()` | Per-call key/value strings the LLM resolves into the prompt. |
| ElevenLabs tool call request | ElevenLabs → us | per-tool `ToolDef.parameters` | What the LLM may send to a webhook. |
| ElevenLabs tool call response | us → ElevenLabs | per-tool documented contract | What the LLM sees back. |
| ElevenLabs `post_call_transcription` webhook | ElevenLabs → us | `PostCallReport` | Fired once when the call ends, carrying the full transcript. The only ground truth `CallSession` consumes from ElevenLabs. |
| `CaseEvent` (WebSocket) | server → simulator browser | `CaseEvent` | One audit-log line streamed live to the operator console. |

---

## `config/agent.json`

Validated by `AgentConfig` (`src/guidepoint/agent/_models.py`).

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `agent_id` | `AgentId` (str) | yes | non-empty | The ElevenLabs agent id. |
| `name` | str | yes | min 1 char | Human-readable name shown in the dashboard. |
| `language` | str | yes | 2-8 chars | ISO 639-1 code (`en`, `es`). |
| `llm` | str | yes | min 1 char | LLM identifier (`gpt-4o-mini`). |
| `temperature` | float | yes | 0.0 – 2.0 | LLM sampling temperature. |
| `first_message` | str | yes | (may be empty) | Empty = wait for user; non-empty = Kate speaks first. |
| `voice_id` | str | yes | min 1 char | ElevenLabs voice id. |
| `tts_model_id` | str | yes | min 1 char | TTS engine identifier. |
| `system_prompt_path` | str | yes | min 1 char | Filename (relative to `config/`) of the prompt body. |
| `tool_ids` | list[ToolId] | no, default `[]` | non-empty strings | ElevenLabs tool ids attached to this agent. |

> **Note (ADR 0004):** there is no `variables` field. Per-call variable
> values live exclusively in the Case snapshot.

---

## `config/tools/<name>.json`

Validated by `ToolDef`. Field shape unchanged from previous versions
(`tool_id`, `name`, `description`, `method`, `url`, `parameters`, `mocks`).

---

## `config/system-prompt.md`

Plain markdown. The only validation is **placeholder resolution**: every
`{{name}}` inside this file must be produced by `Case.to_variables()`.
Protected by `.cursor/rules/system-prompt-protected.mdc`.

---

## Master data — `guidepoint.master_data`

Reference rows that exist independently of any one call. In production these
live in MySQL; in simulator mode they live as JSON files. The
`MasterDataRepository` Protocol abstracts the storage.

### `CustomerRecord` — `fixtures/customers/<customer_id>.json`

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `id` | `CustomerId` (str) | yes | non-empty | Internal customer id. |
| `first_name` | str | yes | min 1 char | Used in greeting. |
| `last_name` | str | yes | min 1 char | Used in formal address. |
| `phone` | str | yes | min 7 chars (E.164 recommended) | Customer phone. |
| `opt_status` | `"opted_in" \| "opted_out" \| "unknown"` | no, default `"unknown"` | enum | Consent status. **A trigger pointing at an `"opted_out"` customer must not be fired.** |
| `preferred_channel` | `"voice" \| "sms" \| "email" \| "unknown"` | no, default `"unknown"` | enum | Hint to the orchestrator picking a channel. |
| `timezone` | str | no, default `"UTC"` | min 3 chars | IANA tz name. |

Derived (computed property):

| Field | Type | Description |
|---|---|---|
| `full_name` | str | `"{first_name} {last_name}"`. |

### `DealerRecord` — `fixtures/dealers/<dealer_id>.json`

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `id` | `DealerId` (str) | yes | non-empty | Internal dealer id. |
| `name` | str | yes | min 1 char | Display name. |
| `phone` | str | yes | min 7 chars | Service-department phone. |
| `address` | str | yes | min 1 char | One-line formatted address. |
| `ride_radius_miles` | int | yes | 0 – 500 | Distance the dealer's loaner-shuttle covers. |

### `VehicleRecord` — `fixtures/vehicles/<vin>.json`

VIN is the primary key.

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `vin` | `VehicleVin` (str) | yes | unique | Vehicle VIN. |
| `owner_id` | `CustomerId` | yes | FK → `CustomerRecord.id` | Owning customer. |
| `year` | int | yes | 1980 – 2100 | Model year. |
| `make` | str | yes | min 1 char | Manufacturer. |
| `model` | str | yes | min 1 char | Model. |
| `odometer_miles` | int | yes | 0 – 1,000,000 | Most recent telematics-reported odometer. |
| `current_location` | `Location` | yes | — | Most recent telematics-reported location. |

### `Location`

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `latitude` | float | yes | -90.0 – 90.0 | WGS-84 latitude. |
| `longitude` | float | yes | -180.0 – 180.0 | WGS-84 longitude. |
| `description` | str | yes | min 1 char | Human-readable label (`"Owner's home, driveway"`). |

---

## Case domain — `guidepoint.case`

### `Trigger` — `fixtures/triggers/<trigger_id>.json`

A pending stimulus. The case manager fires triggers; each fire creates exactly
one Case.

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `id` | `TriggerId` (str) | yes | non-empty | Internal trigger id (filename stem). |
| `vehicle_vin` | `VehicleVin` | yes | FK → `VehicleRecord.vin` | The vehicle this trigger is about. The customer is derived through the vehicle's `owner_id`. |
| `dealer_id` | `DealerId` | yes | FK → `DealerRecord.id` | The dealership Kate represents on this call. |
| `service_event` | `ServiceEvent` | yes | — | Why we're calling. |
| `channel_preference` | `"voice" \| "sms"` | yes | enum | Channel the operator/upstream wants. |
| `destination` | str | yes | min 7 chars | Where to call/text. Usually `customer.phone`, sometimes overridden. |
| `offered_slots` | tuple[OfferedSlot] | no, default `[]` | — | Pre-fetched appointment options. |
| `source` | `"telematics" \| "operator" \| "batch"` | no, default `"operator"` | enum | Where the trigger came from. |
| `status` | `"pending" \| "fired" \| "failed" \| "cancelled"` | no, default `"pending"` | enum | Lifecycle status; the case manager mutates this when it fires. |
| `created_at` | datetime | yes | tz-aware UTC | When the trigger was inserted. |
| `fired_at` | datetime \| null | no, default null | tz-aware UTC | Set when the case manager fires it. |
| `error_detail` | str | no, default `""` | — | If `status='failed'`, the reason. |

### `ServiceEvent`

`summary` is for the customer's ears. `narrative` is **for Kate's
context only** — she uses it to understand the situation but does not
read it verbatim and does not troubleshoot from it.

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `type` | `"dtc" \| "recall" \| "maintenance"` | yes | enum | Category. Drives prompt branching. |
| `summary` | str | yes | min 1 char | Customer-facing short label. No codes, no jargon. |
| `narrative` | str | no, default `""` | — | Internal context for Kate. Free-text. |

### `OfferedSlot`

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `id` | `SlotId` (str) | yes | non-empty | Slot identifier the booking tool will reference. |
| `starts_at` | datetime | yes | ISO 8601 with tz | Canonical slot start time. |
| `display` | str | yes | min 1 char | Human-readable rendering. What Kate reads aloud. |

### `Case` — `fixtures/cases/<case_id>.json`

Created by the case manager when a trigger fires. **Never hand-authored.**
Carries a frozen snapshot of the master data captured at fire time so the
case is replayable and auditable independent of later edits to the
underlying records.

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `case_id` | `CaseId` (str) | yes | unique | Internal case id. |
| `trigger_id` | `TriggerId` | yes | FK → `Trigger.id` | The trigger that created this case. |
| `correlation_id` | str | yes | non-empty | Threaded into every log event for this case. |
| `customer` | `CustomerRecord` | yes | snapshot | Frozen copy of the customer at fire time. |
| `dealer` | `DealerRecord` | yes | snapshot | Frozen copy of the dealer at fire time. |
| `vehicle` | `VehicleRecord` | yes | snapshot | Frozen copy of the vehicle at fire time. |
| `service_event` | `ServiceEvent` | yes | snapshot | Copied from the trigger. |
| `offered_slots` | tuple[OfferedSlot] | no, default `[]` | snapshot | Copied from the trigger. |
| `channel` | `"voice" \| "sms"` | no, default `"voice"` | enum | Snapshotted from `Trigger.channel_preference` at fire time. Drives which `CallSession` the manager dispatches to. |
| `state` | `CaseState` | no, default `created` | enum | Where in the business lifecycle this case is. See below. |
| `attempt_count` | int | no, default 0 | ≥ 0 | Number of call attempts placed so far. |
| `next_attempt_at` | datetime \| null | no, default null | tz-aware UTC | If retryable, when to try again. |
| `call_attempts` | tuple[CallAttempt] | no, default `[]` | — | History of placed calls. |
| `events` | tuple[CaseEvent] | no, default `[]` | — | Audit log. |
| `outcome_detail` | str | no, default `""` | — | Free-text reason recorded with the terminal state. |
| `booked_slot_id` | `SlotId` \| null | no, default null | — | Set when state = `booked`. |
| `created_at` | datetime | yes | tz-aware UTC | When the case manager created the case. |
| `closed_at` | datetime \| null | no, default null | tz-aware UTC | When the case reached a terminal state. |

### `CaseState`

| Value | Terminal? | Set by | Meaning |
|---|---|---|---|
| `created` | no | case manager | The case has been built but no call attempted yet. |
| `ready_to_call` | no | case manager | Pre-flight checks pass. |
| `calling` | no | case manager | A call attempt is in flight. |
| `between_attempts` | no | case manager | Waiting on retry policy (unused under single-shot). |
| `booked` | yes | case manager | Customer accepted a slot. |
| `declined` | yes | case manager | Customer declined to schedule. |
| `unreachable` | yes | case manager | No answer / busy / connection failed / error. |
| `escalated` | yes | case manager | Transferred to a human. |
| `cancelled` | yes | case manager | External cancellation (operator or upstream). |

### `CallState` (live, not persisted on `Case`)

Mirrored by `CallSession` from ElevenLabs while a call is in flight.
Surfaced through the live event stream; not a column on `Case`.

| Value | Meaning |
|---|---|
| `dialing` | Outbound dialing started. |
| `ringing` | Carrier reports ringing. |
| `connected` | Customer picked up. |
| `in_conversation` | Active turn-taking. |
| `ended` | ElevenLabs declared the conversation finished. |

### `CallOutcome`

Returned by `CallSession.place` to `CaseManager` at the end of one
attempt. `result` is what telephony reports; `business_outcome` is what
the conversation produced.

| Field | Type | Description |
|---|---|---|
| `result` | `"answered" \| "no_answer" \| "busy" \| "connection_failed" \| "error"` | Telephony / SDK level. |
| `business_outcome` | `"booked" \| "declined" \| "transferred" \| "inconclusive"` \| null | Conversation-level. Null if the call never connected. |
| `booked_slot_id` | `SlotId` \| null | Set when `business_outcome = "booked"`. |
| `elevenlabs_conversation_id` | str | Their conversation id, for cross-system lookup. |
| `started_at` / `ended_at` | datetime | Bracketing the attempt. |
| `duration_seconds` | float | Call duration. |
| `transcript` | str | Full transcript, when available. |
| `recording_url` | str | Recording URL, when available. |
| `error_detail` | str | If `result = "error"`, the reason. |

### `CallAttempt`

One row on `Case.call_attempts`.

| Field | Type | Description |
|---|---|---|
| `attempt_number` | int (≥ 1) | 1-based attempt counter. |
| `outcome` | `CallOutcome` | What this attempt produced. |

### `PostCallReport`

Boundary model for the ElevenLabs `post_call_transcription` webhook,
narrowed to the fields `CallSession` actually consumes. Both
`_StubCallSession` (synthesized from the canned script) and the future
`_LiveCallSession` (built from the verified webhook body) end up
holding one of these and funnel it through
`case._post_call.ingest_post_call_report`, which appends a
`conversation.transcript_received` `CaseEvent` and converts the report
into a `CallOutcome`.

| Field | Type | Required? | Description |
|---|---|---|---|
| `elevenlabs_conversation_id` | str (≥ 1) | yes | Their id; cross-system lookup. |
| `status` | `"done" \| "failed"` | yes | Whether the call completed cleanly. |
| `started_at` | datetime | yes | Call start (tz-aware). |
| `ended_at` | datetime | yes | Call end (tz-aware). |
| `duration_seconds` | float (≥ 0) | yes | Total call duration. |
| `transcript` | tuple[`TranscriptTurn`] | no, default `()` | Per-turn record of what each side said. |
| `business_outcome` | `"booked" \| "declined" \| "transferred" \| "inconclusive"` | no, default `"inconclusive"` | Inferred from the conversation analysis or our own tool-call audit. |
| `booked_slot_id` | `SlotId` \| null | no | Set when `business_outcome = "booked"`. |
| `recording_url` | str | no, default `""` | Audio download URL when delivered. |
| `error_detail` | str | no, default `""` | When `status = "failed"`, the reason. |

### `TranscriptTurn`

One side of one exchange in the call. Mirrors the per-turn shape
inside the ElevenLabs `post_call_transcription` payload.

| Field | Type | Description |
|---|---|---|
| `role` | `"agent" \| "user"` | Who spoke. |
| `message` | str (≥ 1) | What they said. |
| `time_in_call_seconds` | float (≥ 0) | Offset from call start. |

### `CaseEvent`

One row on `Case.events`. Also published live to the `EventBus`
for any UI subscribed to the WebSocket.

| Field | Type | Description |
|---|---|---|
| `event_id` | str | Unique event id. |
| `case_id` | `CaseId` | The case this event belongs to. |
| `correlation_id` | str | Same correlation id stamped on every log line for this case. |
| `attempt_number` | int \| null | Set when the event happened during a call attempt. |
| `timestamp` | datetime | tz-aware UTC. |
| `source` | `"system" \| "elevenlabs" \| "tool_webhook" \| "operator"` | Who emitted the event. |
| `level` | `"info" \| "warn" \| "error" \| "debug"` | Log severity. |
| `event` | str | Event name (`case.created`, `agent.message`, `tool.call`). |
| `detail` | str | Free-text payload. |

---

## ElevenLabs `dynamic_variables` payload

Produced by `Case.to_variables()`. Sent as
`conversation_initiation_client_data.dynamic_variables`. **All values are
strings** — the ElevenLabs API requires it.

| Variable | Source | Example |
|---|---|---|
| `case_id` | `case.case_id` | `case_2026_05_10_0001` |
| `trigger_id` | `case.trigger_id` | `trig_2026_05_10_0001` |
| `customer_id` | `customer.id` | `cust_jones_robert` |
| `customer_first_name` | `customer.first_name` | `Robert` |
| `customer_last_name` | `customer.last_name` | `Jones` |
| `customer_full_name` | derived | `Robert Jones` |
| `customer_phone` | `customer.phone` | `+13139095330` |
| `customer_opt_status` | `customer.opt_status` | `opted_in` |
| `customer_preferred_channel` | `customer.preferred_channel` | `voice` |
| `customer_timezone` | `customer.timezone` | `America/Detroit` |
| `dealer_id` | `dealer.id` | `dealer_village_jeep_royal_oak` |
| `dealer_name` | `dealer.name` | `Village Jeep` |
| `dealer_phone` | `dealer.phone` | `+12485551234` |
| `dealer_address` | `dealer.address` | `123 Main St, Royal Oak, MI 48067` |
| `ride_radius_miles` | `dealer.ride_radius_miles` | `10` |
| `vehicle_year` | `vehicle.year` | `2025` |
| `vehicle_make` | `vehicle.make` | `Jeep` |
| `vehicle_model` | `vehicle.model` | `Grand Cherokee` |
| `vehicle_vin` | `vehicle.vin` | `1C4RJFBG5NC123456` |
| `vehicle_odometer_miles` | `vehicle.odometer_miles` | `32180` |
| `vehicle_location_lat` | `vehicle.current_location.latitude` | `42.489500` |
| `vehicle_location_lon` | `vehicle.current_location.longitude` | `-83.144600` |
| `vehicle_location_description` | `vehicle.current_location.description` | `Owner's home, driveway` |
| `service_reason_type` | `service_event.type` | `maintenance` |
| `service_reason_summary` | `service_event.summary` | `30,000 mile service` |
| `service_reason_narrative` | `service_event.narrative` | `Standard 30k mile service per manufacturer schedule.` |
| `slot_count` | `len(offered_slots)` | `3` |
| `slot_options` | `; `-joined `slot.display` | `Tuesday, May 12, 2026 - 8:30 AM; ...` |

The set of keys this dict produces is the exact universe `validate_config`
accepts as resolvers for `{{variable}}` placeholders in
`config/system-prompt.md`. There are no other resolvers; no dashboard
defaults exist (ADR 0004).

---

## Conventions

- **Naming**: `snake_case` for fields and dynamic variables. `PascalCase` for model classes.
- **Strings on the wire**: ElevenLabs `dynamic_variables` are always strings. Numbers are stringified by `Case.to_variables()`. Don't depend on the LLM parsing them as numbers — render hints in the prompt.
- **Unknown fields**: every model has `extra="forbid"`. If a JSON file carries a key the model doesn't know about, validation fails. There is no silent acceptance.
- **Required vs optional**: anything without a `default=` in the model is required. Optional fields get explicit defaults (no implicit `None`).
- **Times**: stored as tz-aware UTC. Render to local time only at the presentation boundary (the `display` string on `OfferedSlot`).
- **Source of truth**: this document mirrors the model. The model wins. Update both in the same commit.

---

## Variable-namespace audit

Every `{{var}}` in `config/system-prompt.md` must be a key produced by
`Case.to_variables()`. The dedicated audit module
(`src/guidepoint/agent/_variable_audit.py`) is the single place this
rule lives; both `validate_config` and the CLI delegate to it.

```bash
python -m guidepoint.agent check-prompt          # errors fail, warnings print
python -m guidepoint.agent check-prompt --strict # warnings fail too
```

- **Errors** (CI gate): a `{{var}}` in the prompt that is not a Case key — the literal `{{var}}` text would leak into the conversation at runtime.
- **Warnings**: Case keys the prompt does not reference. These do not fail `validate`; they surface here so the prompt and Case schema stay in sync over time.
