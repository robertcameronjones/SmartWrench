"""
Twilio SMS receiver — the data pipe.

POST /sms        Twilio "A MESSAGE COMES IN" webhook target. Validates the
                 X-Twilio-Signature header, records the message, optionally
                 dispatches it to a registered inbound handler, returns an
                 empty TwiML response. No reply.
GET  /messages   JSON dump of received messages, newest first.
GET  /           Plain HTML list of received messages, auto-refresh.
GET  /health     Liveness + config probe.

Run:
  uvicorn sms.server:app --host 0.0.0.0 --port 8000

Dispatch hook
=============
``sms_adapter`` (or any other consumer that needs the conversation half)
can register an inbound handler at startup::

    from sms.server import register_inbound_handler
    register_inbound_handler(my_async_handler)

The handler is awaited inside the webhook with the parsed fields. Errors
in the handler are logged but do not break the TwiML response — Twilio
still sees a 200/empty so it doesn't retry.

If no handler is registered, the webhook just records to the in-memory
inbox and returns. That matches the original "dumb pipe" behavior so
this server still works standalone for ad-hoc testing.
"""

from __future__ import annotations

import os
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from twilio.request_validator import RequestValidator

load_dotenv()

# Read these per request rather than caching at import time. The
# simulator host process loads its .env files in main() AFTER this
# module has already been imported, so a snapshot here would always
# be empty in the mounted-webhook deployment.
def _auth_token() -> str:
    return os.getenv("TWILIO_AUTH_TOKEN", "")


def _public_base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


def _skip_validation() -> bool:
    return os.getenv("SKIP_SIGNATURE_VALIDATION", "0") == "1"


app = FastAPI(title="Twilio SMS pipe")

INBOX: list[dict[str, Any]] = []

# Dispatch hook: callable(*, from_number, to_number, body, message_sid) -> Awaitable
InboundHandler = Callable[..., Awaitable[None]]
_inbound_handler: InboundHandler | None = None


def register_inbound_handler(handler: InboundHandler) -> None:
    """Wire a coroutine to be invoked for every inbound message.

    Called once at host-process startup (e.g. by the simulator boot or by
    a standalone sms_adapter runner). Subsequent calls replace the prior
    handler. Pass ``None`` to deregister.
    """
    global _inbound_handler
    _inbound_handler = handler


def _validate_twilio_signature(request: Request, form: dict[str, str]) -> None:
    if _skip_validation():
        return
    auth_token = _auth_token()
    if not auth_token:
        raise HTTPException(500, "TWILIO_AUTH_TOKEN not set")
    public_base_url = _public_base_url()
    if not public_base_url:
        raise HTTPException(
            500,
            "PUBLIC_BASE_URL not set — required for signature validation. "
            "Set it to your public https URL or set SKIP_SIGNATURE_VALIDATION=1.",
        )
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"{public_base_url}{request.url.path}"
    if not RequestValidator(auth_token).validate(url, form, signature):
        raise HTTPException(403, "Invalid Twilio signature")


@app.post("/sms")
async def inbound_sms(
    request: Request,
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(""),
    MessageSid: str = Form(...),
    NumMedia: int = Form(0),
):
    form = dict((await request.form()).multi_items())
    _validate_twilio_signature(request, form)

    record = {
        "received_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "message_sid": MessageSid,
        "from": From,
        "to": To,
        "body": Body,
        "num_media": NumMedia,
    }
    INBOX.append(record)
    print(f"[in] {From} -> {To}: {Body!r}  ({MessageSid})")

    handler = _inbound_handler
    if handler is not None:
        try:
            await handler(
                from_number=From,
                to_number=To,
                body=Body,
                message_sid=MessageSid,
            )
        except Exception:
            # Never let a downstream handler crash the webhook — Twilio
            # would retry, which compounds the failure. Log and move on.
            print("[error] inbound handler raised:")
            traceback.print_exc()

    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response/>',
        media_type="application/xml",
    )


@app.get("/messages")
def list_messages():
    return JSONResponse({"count": len(INBOX), "messages": list(reversed(INBOX))})


@app.get("/health")
def health():
    return {
        "ok": True,
        "auth_token_set": bool(_auth_token()),
        "public_base_url": _public_base_url() or None,
        "signature_validation": not _skip_validation(),
        "inbox_count": len(INBOX),
        "inbound_handler_registered": _inbound_handler is not None,
    }


@app.get("/", response_class=HTMLResponse)
def index():
    rows = "".join(
        f"<tr><td>{m['received_at']}</td>"
        f"<td>{m['from']}</td>"
        f"<td>{m['to']}</td>"
        f"<td>{_html_escape(m['body'])}</td>"
        f"<td><code>{m['message_sid']}</code></td></tr>"
        for m in reversed(INBOX)
    ) or "<tr><td colspan=5 style='color:#888'>No messages received yet.</td></tr>"

    public_base_url = _public_base_url()
    skip_validation = _skip_validation()
    return f"""
<!doctype html>
<html><head>
  <meta charset="utf-8"/>
  <title>SMS pipe</title>
  <meta http-equiv="refresh" content="3"/>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 2rem; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #eee; vertical-align: top; }}
    th {{ background: #f5f5f5; }}
    code {{ font-size: 0.85em; color: #666; }}
    .meta {{ color: #888; margin-bottom: 1rem; }}
  </style>
</head><body>
  <h1>SMS in</h1>
  <div class="meta">
    {len(INBOX)} message(s) — auto-refreshes every 3s.
    Webhook: <code>POST {public_base_url or '(set PUBLIC_BASE_URL)'}/sms</code> ·
    Signature validation: <b>{'on' if not skip_validation else 'OFF'}</b> ·
    Inbound handler: <b>{'registered' if _inbound_handler else 'none'}</b>
  </div>
  <table>
    <thead><tr><th>Received (UTC)</th><th>From</th><th>To</th><th>Body</th><th>SID</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</body></html>
"""


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


__all__ = ["app", "inbound_sms", "register_inbound_handler"]
