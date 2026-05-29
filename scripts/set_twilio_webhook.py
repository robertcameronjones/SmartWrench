"""Point a Twilio phone number's SMS webhook at a new URL.

Use when you cut over inbound SMS between environments (local ngrok,
Render, etc.) and don't want to dig through the Twilio Console.

Usage:
    python scripts/set_twilio_webhook.py <base_url> [--number +1...]

``<base_url>`` is the scheme+host (NO trailing slash, NO path). ``/sms``
is appended automatically since that's the route the SMS adapter
serves. ``--number`` defaults to ``TWILIO_FROM_NUMBER`` from
``sms/.env``.

Credentials are read from ``sms/.env``:
    TWILIO_ACCOUNT_SID  — either an Account SID (AC...) or API Key SID (SK...)
    TWILIO_AUTH_TOKEN   — Account Auth Token or API Key Secret to match
    TWILIO_FROM_NUMBER  — E.164 phone number whose webhook is being changed

If the SID is an API Key (SK...), Twilio routes it to its parent
account automatically — no separate Account SID is needed.

Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from twilio.rest import Client

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / "sms" / ".env"


def _load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        sys.exit(f"env file not found: {path}")
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.split("#", 1)[0].strip()
    return env


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "base_url",
        help="Public base URL of the inbound webhook host, e.g. https://kate-v2.onrender.com",
    )
    parser.add_argument(
        "--number",
        default=None,
        help="E.164 phone number to update (defaults to TWILIO_FROM_NUMBER in sms/.env)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Look up everything but don't actually PATCH",
    )
    args = parser.parse_args()

    env = _load_env(_ENV_FILE)
    sid = env.get("TWILIO_ACCOUNT_SID", "")
    secret = env.get("TWILIO_AUTH_TOKEN", "")
    number = args.number or env.get("TWILIO_FROM_NUMBER", "")
    if not (sid and secret and number):
        sys.exit("sms/.env missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER")

    target_url = args.base_url.rstrip("/") + "/sms"

    # 2-arg Client form — Twilio routes API Key SIDs (SK...) to their
    # parent account automatically; Account SIDs (AC...) are used directly.
    client = Client(sid, secret)

    numbers = client.incoming_phone_numbers.list(phone_number=number)
    if not numbers:
        sys.exit(f"phone number {number} not found under SID {sid}")
    if len(numbers) > 1:
        sys.exit(f"unexpected: {len(numbers)} matches for {number}")
    n = numbers[0]

    print(f"phone_number      : {n.phone_number}")
    print(f"phone_number_sid  : {n.sid}")
    print(f"current sms_url   : {n.sms_url}")
    print(f"current sms_method: {n.sms_method}")
    print(f"new     sms_url   : {target_url}")

    if args.dry_run:
        print("dry-run: not patching")
        return

    updated = client.incoming_phone_numbers(n.sid).update(
        sms_url=target_url,
        sms_method="POST",
    )
    print(f"updated sms_url   : {updated.sms_url}")
    print(f"updated sms_method: {updated.sms_method}")


if __name__ == "__main__":
    main()
