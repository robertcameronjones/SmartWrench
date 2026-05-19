"""HTTP Basic Auth middleware: one mode, always on.

The whole site is gated by HTTP Basic Auth. The auth *username* must
match a user id in the :class:`UserRegistry`; the password must match
its entry. On success the validated id is stashed on
``request.state.user_id`` so the routes' ``get_user_context``
dependency reads it directly.

Exempt paths (no auth required):

- ``/sms`` — Twilio's inbound webhook (Twilio doesn't send Basic Auth
  headers; that endpoint trusts X-Twilio-Signature instead).
- ``/twilio/*`` — the mounted Twilio debug app.
- ``/health`` — Render's liveness check has no credentials.

WebSocket caveat
================
Starlette HTTP middleware doesn't intercept WebSocket upgrades, so
``/ws/log`` isn't protected here. That's acceptable for "block random
visitors" — the path is unguessable. For real auth put Cloudflare
Access in front.
"""

from __future__ import annotations

import base64
import hmac
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from simulator._users import UserRegistry

_EXEMPT_PREFIXES: tuple[str, ...] = ("/sms", "/twilio", "/health")
_REALM = 'Basic realm="kate-simulator"'


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        if _is_exempt(request.url.path):
            return await call_next(request)

        registry: UserRegistry = request.app.state.user_registry
        validated_id = _validate(
            request.headers.get("authorization", ""),
            registry=registry,
        )
        if validated_id is None:
            return _unauthorized()
        request.state.user_id = validated_id
        return await call_next(request)


def _is_exempt(path: str) -> bool:
    return any(
        path == prefix or path.startswith(prefix + "/") for prefix in _EXEMPT_PREFIXES
    )


def _unauthorized() -> Response:
    return Response(
        status_code=401,
        content="Authentication required.\n",
        headers={"WWW-Authenticate": _REALM},
        media_type="text/plain",
    )


def _validate(authorization_header: str, *, registry: UserRegistry) -> str | None:
    """Return the validated user id, or None on auth failure.

    Always runs a constant-time compare against *some* string even
    for unknown users so the failure mode doesn't leak whether the
    username or the password was wrong.
    """
    scheme, _, encoded = authorization_header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    user_id, sep, password = decoded.partition(":")
    if not sep:
        return None

    try:
        user = registry.get(user_id)
        expected = user.password
    except Exception:
        expected = ""

    if not hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
        return None
    if not expected:
        # Unknown user; compare passed only because both sides were empty.
        return None
    return user_id


__all__ = ["BasicAuthMiddleware"]
