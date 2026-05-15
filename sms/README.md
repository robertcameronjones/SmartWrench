# SMS pipe

A two-way SMS pipe over a Twilio number, packaged as the `sms` Python package.

- `sms.send_sms(...)` — send one outbound SMS (called by the CLI and by `sms_adapter`).
- `sms.server:app`   — FastAPI inbound webhook with an auto-refreshing HTML viewer.
- `sms.cli`          — argparse CLI for one-shot sends.

The webhook also exposes `register_inbound_handler(handler)` so a downstream
component (e.g. `sms_adapter`) can be wired in at startup. Without one,
the webhook is a pure data pipe — it just records and shows messages.

## Setup

```bash
cd sms
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# edit .env: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
```

## Send

```bash
.venv/bin/python -m sms.cli +15551234567 "hello"
echo "long body" | .venv/bin/python -m sms.cli +15551234567 -

# (the pyproject also installs a `send-sms` console script:)
.venv/bin/send-sms +15551234567 "hello"
```

## Receive

1. Run the server:
   ```bash
   .venv/bin/uvicorn sms.server:app --host 0.0.0.0 --port 8000
   ```
2. Expose it publicly (separate terminal):
   ```bash
   ngrok http 8000
   ```
3. Copy the ngrok https URL into `.env` as `PUBLIC_BASE_URL` and restart
   `uvicorn` so signature validation can match the URL Twilio actually
   called.
4. In the Twilio Console → Phone Numbers → your number → **Messaging**
   → "A MESSAGE COMES IN" → `Webhook`, `https://<ngrok>/sms`, `HTTP POST`.
   Save.
5. Text the number. Watch <http://localhost:8000>.

JSON view of received messages: <http://localhost:8000/messages>.
Health check: <http://localhost:8000/health>.

## Layout

```
sms/
├── pyproject.toml
├── README.md
├── .env.example
├── .env             (yours; gitignored)
├── _set_webhook.py  (dev script: programmatically point Twilio at a new URL)
└── src/sms/
    ├── __init__.py    (re-exports send_sms)
    ├── _client.py     (the send_sms function — pure, no env reads)
    ├── server.py      (FastAPI app + register_inbound_handler hook)
    └── cli.py         (the outbound CLI)
```
