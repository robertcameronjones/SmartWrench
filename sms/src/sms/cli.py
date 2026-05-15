"""One-shot outbound SMS sender for the Twilio test number.

Usage:
  python -m sms.cli +15551234567 "hello from the test harness"
  python -m sms.cli +15551234567 "hi" --from +15559998888
  echo "long body" | python -m sms.cli +15551234567 -

Reads TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER from .env.
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from sms._client import send_sms


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Send an SMS via Twilio.")
    parser.add_argument("to", help="Destination phone number in E.164 format (e.g. +15551234567)")
    parser.add_argument("body", help="Message body. Use '-' to read from stdin.")
    parser.add_argument(
        "--from",
        dest="from_number",
        default=os.getenv("TWILIO_FROM_NUMBER"),
        help="Sender number (defaults to TWILIO_FROM_NUMBER from .env).",
    )
    args = parser.parse_args()

    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        print("error: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set in .env", file=sys.stderr)
        return 2
    if not args.from_number:
        print("error: no --from given and TWILIO_FROM_NUMBER not set in .env", file=sys.stderr)
        return 2

    body = sys.stdin.read() if args.body == "-" else args.body
    body = body.strip()
    if not body:
        print("error: empty message body", file=sys.stderr)
        return 2

    msg_sid = send_sms(
        to=args.to,
        body=body,
        account_sid=sid,
        auth_token=token,
        from_number=args.from_number,
    )
    print(f"sent  sid={msg_sid}  from={args.from_number}  to={args.to}")
    print(f"body: {body!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
