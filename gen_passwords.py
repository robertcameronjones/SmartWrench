"""Generate strong 15-char passwords for each operator and print a
``USERS=...`` line ready to paste into the Render dashboard.

Each password contains at least one upper, lower, digit, and special.
Special set excludes characters that would break the env-var format
(``,`` separates entries, ``:`` separates id:password) or that have
shell/YAML semantics (`` ` ``, ``$``, ``"``, ``'``, ``\\``, ``#``,
``=``).
"""

from __future__ import annotations

import secrets
import string

USERS: tuple[str, ...] = (
    "rob",
    "craig",
    "erin",
    "karen",
    "patryk",
    "chase",
    "john",
    "pablo",
    "ibi",
)
LENGTH = 15
SPECIAL = "!@%^&*-_+?"
ALPHABET = string.ascii_lowercase + string.ascii_uppercase + string.digits + SPECIAL


def generate_password() -> str:
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c in SPECIAL for c in pw)
        ):
            return pw


def main() -> None:
    pairs = [(user, generate_password()) for user in USERS]
    env_line = "USERS=" + ",".join(f"{u}:{p}" for u, p in pairs)
    print(env_line)
    print()
    print("Per-user credentials (send to each person via secure channel):")
    for u, p in pairs:
        print(f"  {u:<8} {p}")


if __name__ == "__main__":
    main()
