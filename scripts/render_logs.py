"""Pull (and filter) deployed-simulator logs from the CLI.

Two surfaces, one tool:

1. **Render stdout** — what the structlog handler writes. Lifecycle
   headlines (``simulator.fire.accepted``, ``sms_call_manager.start``,
   ``outbound.worker.sent``, ``sms_call_manager.completed``,
   ``timer.scheduled``). Pulled via Render's ``/v1/logs`` API.

2. **Simulator case audit trail** — the per-turn detail (SMS bodies,
   ``sms.opened`` / ``sms.outbound`` / ``sms.inbound`` events with
   their summaries). Pulled via the authenticated ``/api/cases`` and
   ``/api/cases/{id}`` endpoints on kate-v2. Skipped silently when
   ``SIM_AUTH`` isn't set.

By default both surfaces are pulled and printed in time order so a
single ``python scripts/render_logs.py`` answers "what happened on
kate-v2 in the last 30 minutes".

Usage:
    python scripts/render_logs.py                       # last 30m, filtered, both surfaces
    python scripts/render_logs.py --since 2h            # last 2 hours
    python scripts/render_logs.py --since 17:10 --until 17:35   # today, UTC
    python scripts/render_logs.py --since 2026-05-30T17:10:00Z
    python scripts/render_logs.py --case case_a066234bea219188  # one specific case
    python scripts/render_logs.py --no-cases            # Render only
    python scripts/render_logs.py --all                 # disable default drop-filter
    python scripts/render_logs.py --filter 'outbound\\.'  # custom regex

Credentials read from the repo-root ``.env``:
    RENDER_API_KEY = rnd_...      (required)
    SIM_AUTH       = rob:password (optional; enables case audit pull)

Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / ".env"
_API_ROOT = "https://api.render.com/v1"
_SIM_BASE_DEFAULT = "https://kate-v2.onrender.com"
_PAGE_LIMIT = 100  # Render's hard cap

# Default filter: drop FastAPI access-log noise we don't care about. Anything
# else (structlog JSON, errors, tracebacks, deploy events) passes through.
_DEFAULT_DROP = re.compile(
    r'GET (/health|/api/cases/[^ ]+|/api/connection|/api/world/[^ ]+) HTTP'
)


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


def _api_get(path: str, *, token: str, params: dict[str, str] | None = None) -> dict:
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    req = urllib.request.Request(
        f"{_API_ROOT}{path}{qs}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        sys.exit(f"render api {exc.code} on GET {path}: {body[:300]}")


def _sim_get(
    base_url: str,
    path: str,
    *,
    auth: str,
    params: dict[str, str] | None = None,
) -> object:
    """GET against the deployed simulator using HTTP Basic Auth."""
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    encoded = base64.b64encode(auth.encode()).decode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}{qs}",
        headers={
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        sys.exit(f"sim api {exc.code} on GET {path}: {body[:300]}")


def _resolve_service(name: str, *, token: str) -> tuple[str, str]:
    """Return (service_id, owner_id) for the service named ``name``."""
    services = _api_get("/services", token=token, params={"limit": "50"})
    for entry in services:
        svc = entry["service"]
        if svc["name"] == name:
            return svc["id"], svc["ownerId"]
    available = ", ".join(e["service"]["name"] for e in services) or "(none)"
    sys.exit(f"no Render service named {name!r}. Available: {available}")


def _parse_time(text: str, *, default_now: datetime) -> datetime:
    """Accept ISO timestamps, ``HH:MM[:SS]`` (today UTC), or ``Nm/h/d`` (ago)."""
    t = text.strip()
    m = re.fullmatch(r"(\d+)([smhd])", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
        }[unit]
        return default_now - delta
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", t)
    if m:
        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        return datetime.combine(default_now.date(), time(hh, mm, ss), tzinfo=UTC)
    try:
        # fromisoformat handles 'Z' as +00:00 starting in 3.11.
        return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        sys.exit(f"could not parse time: {text!r} (try '30m', '17:10', or ISO)")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_logs(
    *,
    service_id: str,
    owner_id: str,
    token: str,
    start: datetime,
    end: datetime,
    max_pages: int,
) -> list[dict]:
    """Page through /v1/logs from ``start`` to ``end`` (inclusive)."""
    out: list[dict] = []
    cursor = start
    for _page in range(max_pages):
        data = _api_get(
            "/logs",
            token=token,
            params={
                "ownerId": owner_id,
                "resource": service_id,
                "startTime": _iso(cursor),
                "endTime": _iso(end),
                "limit": str(_PAGE_LIMIT),
                "direction": "forward",
            },
        )
        page = data.get("logs", [])
        out.extend(page)
        next_start = data.get("nextStartTime")
        has_more = bool(data.get("hasMore"))
        if not has_more or not next_start:
            break
        cursor = datetime.fromisoformat(next_start.replace("Z", "+00:00"))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--service", default="kate-v2", help="Render service name (default: kate-v2)"
    )
    parser.add_argument(
        "--since",
        default="30m",
        help="Window start: '30m', '2h', '17:10' (UTC today), or ISO (default: 30m)",
    )
    parser.add_argument(
        "--until",
        default=None,
        help="Window end (same formats as --since). Default: now",
    )
    parser.add_argument(
        "--filter",
        default=None,
        help="Regex; only lines whose message matches are printed. Overrides default drop filter.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Disable the default 'drop access-log noise' filter; print every line.",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=50,
        help="Max pages of 100 to pull (default: 50 = up to 5000 lines).",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=None,
        metavar="CASE_ID",
        help="Specific case_id(s) to fetch from /api/cases/{id}. "
        "Repeatable. When omitted, every case_id mentioned in the "
        "Render log window is pulled.",
    )
    parser.add_argument(
        "--no-cases",
        action="store_true",
        help="Skip the simulator case-audit pull (Render only).",
    )
    parser.add_argument(
        "--sim-url",
        default=_SIM_BASE_DEFAULT,
        help=f"Base URL of the deployed simulator (default: {_SIM_BASE_DEFAULT})",
    )
    args = parser.parse_args()

    env = _load_env(_ENV_FILE)
    token = env.get("RENDER_API_KEY", "")
    if not token:
        sys.exit(f"RENDER_API_KEY not set in {_ENV_FILE}")

    now = datetime.now(UTC)
    start = _parse_time(args.since, default_now=now)
    end = _parse_time(args.until, default_now=now) if args.until else now
    if end <= start:
        sys.exit(f"--until ({_iso(end)}) must be after --since ({_iso(start)})")

    service_id, owner_id = _resolve_service(args.service, token=token)

    keep: re.Pattern[str] | None = re.compile(args.filter) if args.filter else None
    drop = None if (args.all or keep is not None) else _DEFAULT_DROP

    logs = _fetch_logs(
        service_id=service_id,
        owner_id=owner_id,
        token=token,
        start=start,
        end=end,
        max_pages=args.pages,
    )

    print(f"=== Render structlog  window {_iso(start)} → {_iso(end)}  service={args.service}")
    shown = 0
    seen_cases: list[str] = []
    case_re = re.compile(r"case_[0-9a-f]{16}")
    for entry in logs:
        msg = entry.get("message", "")
        if keep is not None and not keep.search(msg):
            continue
        if drop is not None and drop.search(msg):
            continue
        ts = entry.get("timestamp", "")[:23].replace("T", " ")
        print(f"{ts}  {msg}")
        shown += 1
        for cid in case_re.findall(msg):
            if cid not in seen_cases:
                seen_cases.append(cid)

    print(f"--- {shown}/{len(logs)} render lines shown", file=sys.stderr)

    if args.no_cases:
        return

    sim_auth = env.get("SIM_AUTH", "")
    if not sim_auth:
        print(
            "\n(skipping case audit pull — set SIM_AUTH=rob:<password> in .env to enable)",
            file=sys.stderr,
        )
        return

    target_cases = args.case if args.case else seen_cases
    if not target_cases:
        print("\n(no case_ids found in render window)", file=sys.stderr)
        return

    for cid in target_cases:
        print(f"\n=== Case audit  {cid}")
        case = _sim_get(args.sim_url, f"/api/cases/{cid}", auth=sim_auth)
        if not isinstance(case, dict):
            print(f"  (unexpected response shape: {type(case).__name__})")
            continue
        print(
            f"  state={case.get('state')}  channel={case.get('initial_channel')}  "
            f"customer={(case.get('customer') or {}).get('phone')}  "
            f"correlation_id={case.get('correlation_id')}  "
            f"created_at={case.get('created_at')}"
        )
        events = case.get("events") or ()
        if not events:
            print("  (no events)")
        for evt in events:
            ts = (evt.get("timestamp") or "")[:23].replace("T", " ")
            print(
                f"  {ts}  {evt.get('source','?'):>3}  "
                f"{evt.get('event',''):<32}  {evt.get('detail','')}"
            )


if __name__ == "__main__":
    main()
