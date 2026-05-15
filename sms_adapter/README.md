# sms_adapter

Conversation half of the SMS channel. Holds per-conversation message
history, routes inbound SMS to the right thread, asks the LLM what to
say next, sends the reply via Twilio.

## Reading the public surface

Open `src/sms_adapter/__init__.py`. Everything that flows IN, OUT, and
the one function that does the business is declared there. Nothing else.

| Direction | Name                          | What it is                         |
|----------:|-------------------------------|------------------------------------|
| in        | `open_conversation(ctx, deps)` | Fire button with channel=sms      |
| in        | `handle_inbound(...)`          | Twilio webhook delivered an SMS   |
| in        | `close_conversation(...)`      | Case ended (book / decline)        |
| out       | `TwilioSend` (Protocol)        | Send one SMS                       |
| out       | `LlmComplete` (Protocol)       | Call LLM with [system, ...history] |
| out       | `HistoryStore` (Protocol)      | Persist message history             |
| out       | `RoutingStore` (Protocol)      | phone → conversation_id            |
| business  | `take_turn(...)`               | The only place the LLM is called   |

## What it depends on

- `prompt_composer` — composes the system prompt for `Channel.SMS`.
- `sms` — Twilio send + the inbound webhook (we register a handler).
- `llm` — LiteLLM completion.
- `guidepoint` (later) — when real `Case` files exist, we'll add a
  `Case → SmsContext` adapter. For now `SmsContext` is the lightweight
  stand-in.

## Setup

```bash
cd sms_adapter
python3 -m venv .venv
source .venv/bin/activate
pip install -e ../prompt_composer
pip install -e ../sms
pip install -e ../llm
pip install -e ".[dev]"
pytest
```

## Wiring at startup

Whoever hosts the SMS adapter (today: the simulator) builds `SmsDeps`
once, then:

1. Calls `sms.server.register_inbound_handler(...)` with a callback that
   delegates to `handle_inbound(...)`.
2. On a Fire-button press with channel=sms, builds an `SmsContext` from
   master data and calls `open_conversation(ctx, deps=deps)`.

See `simulator/src/simulator/_app.py` for the live wiring.
