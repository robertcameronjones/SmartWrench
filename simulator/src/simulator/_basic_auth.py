"""HTTP Basic Auth middleware for the simulator.

Activates when BOTH ``BASIC_AUTH_USER`` and ``BASIC_AUTH_PASS`` env vars
are set. When either is missing, the middleware no-ops, so local dev
keeps working without configuration.

Exempt paths
============
- ``/sms`` — Twilio's inbound webhook. Twilio does NOT send Basic Auth
  headers; that endpoint relies on its own X-Twilio-Signature header
  (or SKIP_SIGNATURE_VALIDATION) for trust.
- ``/twilio/*`` — the mounted Twilio debug app (messages list, health).
  Same reason: Twilio's webhook lives under this prefix in some configs.

WebSocket caveat
================
Starlette HTTP middleware does not intercept WebSocket upgrades. The
``/ws/log`` event stream is therefore NOT protected by this middleware.
That's acceptable for the "block random visitors" use case — an attacker
would still need to discover the path. If real auth is required, put
Cloudflare Access in front instead.
"""

from __future__ import annotations

import base64
import hmac
import os
import secrets
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_EXEMPT_PREFIXES: tuple[str, ...] = ("/sms", "/twilio", "/health")


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Require HTTP Basic Auth when BASIC_AUTH_USER + BASIC_AUTH_PASS are set."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        if _is_exempt(request.url.path):
            return await call_next(request)

        expected_user = os.getenv("BASIC_AUTH_USER", "")
        expected_pass = os.getenv("BASIC_AUTH_PASS", "")
        if not expected_user or not expected_pass:
            # Auth not configured → behave as if middleware isn't installed.
            return await call_next(request)

        if not _credentials_match(
            request.headers.get("authorization", ""),
            expected_user=expected_user,
            expected_pass=expected_pass,
        ):
            return Response(
                status_code=401,
                content="Authentication required.\n",
                headers={"WWW-Authenticate": 'Basic realm="kate-simulator"'},
                media_type="text/plain",
            )
        return await call_next(request)


def _is_exempt(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _EXEMPT_PREFIXES)


def _credentials_match(
    authorization_header: str,
    *,
    expected_user: str,
    expected_pass: str,
) -> bool:
    """Constant-time check of an ``Authorization: Basic ...`` header."""
    scheme, _, encoded = authorization_header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, _, password = decoded.partition(":")
    # secrets.compare_digest is constant-time; resists timing attacks.
    user_ok = secrets.compare_digest(user.encode("utf-8"), expected_user.encode("utf-8"))
    pass_ok = hmac.compare_digest(password.encode("utf-8"), expected_pass.encode("utf-8"))
    return user_ok and pass_ok


__all__ = ["BasicAuthMiddleware"]
