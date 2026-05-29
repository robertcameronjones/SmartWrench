# prompt_composer

Picks the right spot md for the channel, concatenates with the system prompt,
substitutes `{{placeholders}}` from `case.to_variables()`. Returns a
`RenderedPrompt`. That's the whole job.

## Hard rules

1. The Composer never edits, splits, reorders, paraphrases, or supplements
   prompt text.
2. The Composer never assembles a prompt from fragments. One channel = one
   spot md, plus the shared system prompt.
3. The only transformation is `{{key}}` → `case.to_variables()[key]`.
4. If a `{{placeholder}}` in the prompt is not in `case.to_variables()`,
   the Composer raises `MissingPlaceholderError` immediately. No silent
   fallbacks.
5. Concatenation order: `system.md` first, then a blank line, then the
   channel md. Substitution runs across the joined string.

## Layout

```
prompt_composer/
├── pyproject.toml
├── src/prompt_composer/
│   ├── __init__.py     # public API
│   └── _renderer.py    # substitution
└── tests/
```

## Spot md locations

The Composer doesn't hard-code paths. Caller passes `PromptPaths`. Today the
caller would build it as:

```python
from pathlib import Path
from prompt_composer import PromptPaths

paths = PromptPaths(
    system=Path("11Labs/config/system-prompt.md"),
    post_booking=Path("11Labs/config/prompt-post-booking.md"),
    voice=Path("11Labs/config/voice.md"),
    sms=Path("sms_adapter/config/sms.md"),
)
```

## Usage

```python
from prompt_composer import build_prompt, Channel, PromptStage

rendered = build_prompt(
    case=case,
    channel=Channel.SMS,
    stage=PromptStage.INITIAL_REMINDER,
    paths=paths,
)
rendered.text          # str  — stage prompt + sms.md, vars substituted
rendered.variables     # dict — same dict ElevenLabs gets
rendered.channel       # Channel.SMS
rendered.stage         # PromptStage.INITIAL_REMINDER
rendered.placeholders_used  # frozenset[str]
```

Stage routing:

- ``OUTREACH`` → ``system`` (``system-prompt.md``)
- ``INITIAL_REMINDER`` / ``FINAL_REMINDER`` / ``FEEDBACK`` → ``post_booking`` (``prompt-post-booking.md``)

`case` is anything with `to_variables() -> dict[str, str]`. In practice,
`guidepoint.case.Case`. The composer doesn't depend on `guidepoint`; it
depends on the duck-typed protocol.

## Setup

```bash
cd prompt_composer
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```
