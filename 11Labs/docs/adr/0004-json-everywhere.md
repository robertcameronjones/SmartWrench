# ADR 0004: JSON for all human-authored agent and case config

- **Status:** Accepted
- **Date:** 2026-05-10
- **Owner:** Rob Jones
- **Reviewers:** —
- **Supersedes:** ADR 0003 (TOML for human-authored agent config)

## Context

ADR 0003 chose TOML for the agent and tool config files on the strength
of comments + diffability, with JSON only for case fixtures. After
living with the split for one cycle of pull → edit → trigger, two
problems surfaced:

1. **Two formats means two mental models.** Reading `agent.toml` and a
   case fixture in JSON side by side requires switching parsing rules. For
   a one-engineer project that never stops touching both, that is pure
   tax with no payoff.

2. **The consumer (ElevenLabs) speaks JSON.** Every payload we ship,
   every dashboard test panel input, every webhook body, every line in
   the ElevenLabs request log — JSON. When something goes wrong in a
   live conversation, the troubleshooting loop is:

   ```
   open ElevenLabs log → copy payload → paste somewhere → diff against fixture
   ```

   If the fixture is also JSON, that diff is a one-liner. If the
   fixture is TOML and the agent config is TOML, every step of the
   loop pays a translation cost.

A third issue, separate but resolved by the same change: the
`agent.toml.variables` array duplicates per-call values that already
live in the `Case` fixture, and the two had already drifted (TOML
"Village Jeep" vs. JSON "Village Jeep of Royal Oak"; TOML
`service_reason_type=recall` vs. JSON `"maintenance"`). That is a SPOT
violation (ARCHITECTURE rule #1). The cleanest fix is to delete
`agent.toml.variables` and let the Case be the single source of
runtime variable values.

## Decision

- **JSON** for all human-authored agent and tool config:
  `config/agent.json`, `config/tools/<name>.json`,
  `fixtures/cases/<id>.json`.
- **Markdown** for the system prompt body (`config/system-prompt.md`),
  unchanged. The prompt is literal text Kate reads — neither JSON nor
  TOML applies.
- **Markdown** for documentation (`docs/`), unchanged.
- The `agent.toml.variables` array is **deleted**. There are no
  dashboard-default variables. The Case fixture is the only place
  runtime variable values live.
- `tomli_w` runtime dependency is removed. Reading uses stdlib `json`.
- Comments that previously lived in TOML headers move to:
  - Pydantic field descriptions (in `_models.py`).
  - The data dictionary (`docs/data-dictionary.md`).
  - A leading `_comment` field in fixtures when context is essential.

## Consequences

- One format to read, edit, grep, diff, and paste into the dashboard.
- The trigger script's `--dry-run` output is identical in shape to the
  on-disk fixture (modulo the `to_variables` flatten), so paste-from-
  fixture-to-dashboard works unchanged.
- The variable audit simplifies: no defaults set; the only error is an
  unresolved `{{var}}`, the only warning is an unused Case key.
- Comments in config files are lost. This is the real cost. Mitigation:
  Pydantic models carry the schema, the data dictionary carries the
  prose, and human review catches "wait, why is this value 0.7?" the
  same way it does in any JSON-config codebase.
- Dashboard "Test agent" no longer auto-fills variable defaults.
  Workflow: `python scripts/trigger.py --case fixtures/cases/<id>.json
  --dry-run` prints the payload; copy `dynamic_variables` into the
  dashboard test panel.

## Alternatives considered

- **Stay split (ADR 0003 unchanged).** Rejected — the parity argument
  with ElevenLabs payloads outweighs TOML's comment support for a
  project where the same engineer authors and operates.
- **TOML everywhere (including fixtures).** Rejected for the same
  reason in reverse — moves us *further* from ElevenLabs' format,
  not closer.
- **YAML.** Already rejected in ADR 0003 for the right reasons
  (security gotchas, indentation footguns); none of those reasons
  changed.
