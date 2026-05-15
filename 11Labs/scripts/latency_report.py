"""Render an end-to-end latency report for one case as an HTML file.

Three layers of timing in one view:

1. **Lifecycle phases** — wall-clock spans between ``CaseEvent`` records
   (case.created → call.dialing → call.placed → conversation
   ended → case terminal). Tells us what the orchestration cost.

2. **Call-attempt totals** — ``started_at`` / ``ended_at`` /
   ``duration_seconds`` from each ``CallAttempt.outcome``. Tells us
   how long the actual call lasted vs the post-call retrieval.

3. **Per-turn transcript waterfall** — the gaps between consecutive
   turns inside the call. Gaps where the *agent* is the next speaker
   are Kate's response latency. Gaps where the *user* is next are
   the customer's. Gaps over ``--warn`` seconds are highlighted.

We only get turn-START timestamps from ElevenLabs, so an "agent
response gap" really measures customer-turn-start → agent-turn-start.
That includes the customer's own speech time, but the trailing silence
(after the last turn until call end) is a pure agent gap and is the
single best signal that Kate hung mid-thought.

Usage::

    # Latest case in fixtures/cases/
    python scripts/latency_report.py

    # Specific case
    python scripts/latency_report.py --case-id case_d6a1ba1400213bb3

    # Tighter "concerning gap" threshold
    python scripts/latency_report.py --warn 2.0

The report writes to ``reports/latency_<case_id>.html`` and the path
is printed to stdout. Pass ``--open`` to pop it in your browser.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, final

WARN_GAP_SECONDS_DEFAULT: Final[float] = 3.0
ALERT_GAP_SECONDS_DEFAULT: Final[float] = 6.0


# --------------------------------------------------------------------------- #
# Pure analysis                                                               #
# --------------------------------------------------------------------------- #


@final
@dataclass(frozen=True, slots=True)
class Phase:
    """One named span in the case lifecycle."""

    name: str
    started_at: datetime
    ended_at: datetime

    @property
    def duration_seconds(self) -> float:
        return max((self.ended_at - self.started_at).total_seconds(), 0.0)


@final
@dataclass(frozen=True, slots=True)
class TurnRow:
    """One row in the transcript waterfall."""

    index: int
    role: Literal["agent", "user"]
    message: str
    time_in_call_seconds: float
    gap_before_seconds: float  # 0.0 for the first turn


@final
@dataclass(frozen=True, slots=True)
class AttemptReport:
    """Per-call-attempt analysis."""

    attempt_number: int
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    business_outcome: str
    transcript_turns: tuple[TurnRow, ...]
    trailing_silence_seconds: float
    longest_agent_gap: float
    longest_user_gap: float


@final
@dataclass(frozen=True, slots=True)
class CaseReport:
    """Top-level analysis."""

    case_id: str
    correlation_id: str
    state: str
    created_at: datetime
    closed_at: datetime | None
    phases: tuple[Phase, ...]
    attempts: tuple[AttemptReport, ...]


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, str):
        # fromisoformat handles "...Z" since Python 3.11.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    msg = f"expected ISO timestamp string, got {type(value).__name__}"
    raise TypeError(msg)


def _parse_transcript(raw: str) -> tuple[TurnRow, ...]:
    """Parse the human-readable transcript string into structured turns.

    The string we save is built by ``case._post_call._format_transcript``
    in the form ``[  12.3s] Kate: hello.\\n[  15.0s] Customer: hi.``.
    We round-trip it here rather than re-pulling raw turns from
    ElevenLabs because the saved transcript is what the audit trail
    sees, and the latency report is an audit tool.
    """
    rows: list[TurnRow] = []
    prev_t = 0.0
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line.startswith("["):
            continue
        try:
            close = line.index("]")
            t_part = line[1:close].strip().rstrip("s").strip()
            time_in_call = float(t_part)
            after = line[close + 1 :].strip()
            speaker, _, message = after.partition(":")
            speaker = speaker.strip().lower()
            role: Literal["agent", "user"] = "agent" if speaker == "kate" else "user"
        except (ValueError, IndexError):
            continue
        gap = time_in_call - prev_t if rows else 0.0
        rows.append(
            TurnRow(
                index=len(rows) + 1,
                role=role,
                message=message.strip(),
                time_in_call_seconds=time_in_call,
                gap_before_seconds=max(gap, 0.0),
            )
        )
        prev_t = time_in_call
    return tuple(rows)


def _build_phases(case: dict[str, Any]) -> tuple[Phase, ...]:
    """Stitch consecutive ``CaseEvent`` records into named phases."""
    events = case.get("events", [])
    if not events:
        return ()
    pairs = [
        ("orchestration: create → dial", "case.created", "call.dialing"),
        ("provider: dial → placed", "call.dialing", "call.placed"),
        ("call: placed → transcript received", "call.placed", "conversation.transcript_received"),
        ("post-call: transcript → terminal", "conversation.transcript_received", None),
    ]
    by_event: dict[str, datetime] = {}
    for evt in events:
        name = evt.get("event")
        ts = evt.get("timestamp")
        if isinstance(name, str) and name not in by_event and ts is not None:
            by_event[name] = _parse_dt(ts)

    phases: list[Phase] = []
    for label, start_evt, end_evt in pairs:
        start_ts = by_event.get(start_evt)
        if start_ts is None:
            continue
        if end_evt is None:
            # Find the case.* terminal event timestamp.
            terminal = next(
                (
                    _parse_dt(e["timestamp"])
                    for e in events
                    if isinstance(e.get("event"), str)
                    and e["event"].startswith("case.")
                    and e["event"]
                    not in {"case.created", "case.calling", "case.between_attempts"}
                    and e is not events[0]
                ),
                None,
            )
            if terminal is None:
                continue
            end_ts = terminal
        else:
            end_ts = by_event.get(end_evt)
            if end_ts is None:
                continue
        phases.append(Phase(name=label, started_at=start_ts, ended_at=end_ts))
    return tuple(phases)


def _build_attempt_report(attempt: dict[str, Any]) -> AttemptReport:
    outcome = attempt["outcome"]
    started_at = _parse_dt(outcome["started_at"])
    ended_at = _parse_dt(outcome["ended_at"])
    duration = float(outcome.get("duration_seconds", 0.0))
    turns = _parse_transcript(str(outcome.get("transcript", "")))
    trailing = max(duration - turns[-1].time_in_call_seconds, 0.0) if turns else duration

    agent_gaps = [t.gap_before_seconds for t in turns if t.role == "agent"]
    user_gaps = [t.gap_before_seconds for t in turns if t.role == "user"]
    return AttemptReport(
        attempt_number=int(attempt.get("attempt_number", 0)),
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration,
        business_outcome=str(outcome.get("business_outcome", "") or ""),
        transcript_turns=turns,
        trailing_silence_seconds=trailing,
        longest_agent_gap=max(agent_gaps) if agent_gaps else 0.0,
        longest_user_gap=max(user_gaps) if user_gaps else 0.0,
    )


def build_case_report(case: dict[str, Any]) -> CaseReport:
    """Pure transformation: case JSON dict → ``CaseReport``."""
    return CaseReport(
        case_id=str(case["case_id"]),
        correlation_id=str(case.get("correlation_id", "")),
        state=str(case.get("state", "")),
        created_at=_parse_dt(case["created_at"]),
        closed_at=_parse_dt(case["closed_at"]) if case.get("closed_at") else None,
        phases=_build_phases(case),
        attempts=tuple(_build_attempt_report(a) for a in case.get("call_attempts", [])),
    )


# --------------------------------------------------------------------------- #
# HTML rendering                                                              #
# --------------------------------------------------------------------------- #


def _esc(s: object) -> str:
    return html_lib.escape(str(s))


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _fmt_secs(s: float) -> str:
    return f"{s:.3f}s"


def _gap_class(gap: float, *, warn: float, alert: float) -> str:
    if gap >= alert:
        return "gap-alert"
    if gap >= warn:
        return "gap-warn"
    return ""


def _render_phases_table(phases: Iterable[Phase]) -> str:
    rows = []
    for p in phases:
        rows.append(
            f"<tr><td>{_esc(p.name)}</td>"
            f"<td class='mono'>{_fmt_dt(p.started_at)}</td>"
            f"<td class='mono'>{_fmt_dt(p.ended_at)}</td>"
            f"<td class='mono right'>{_fmt_secs(p.duration_seconds)}</td></tr>"
        )
    if not rows:
        return "<p class='dim'>No lifecycle events recorded.</p>"
    return (
        "<table class='lifecycle'><thead><tr>"
        "<th>Phase</th><th>Started</th><th>Ended</th><th class='right'>Duration</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _render_attempt_section(
    attempt: AttemptReport, *, warn: float, alert: float
) -> str:
    summary = (
        f"<div class='attempt-summary'>"
        f"<span><b>Attempt {attempt.attempt_number}</b></span>"
        f"<span>started <span class='mono'>{_fmt_dt(attempt.started_at)}</span></span>"
        f"<span>ended <span class='mono'>{_fmt_dt(attempt.ended_at)}</span></span>"
        f"<span>call duration <span class='mono'>{_fmt_secs(attempt.duration_seconds)}</span></span>"
        f"<span>outcome <span class='mono'>{_esc(attempt.business_outcome or 'n/a')}</span></span>"
        f"</div>"
    )

    headline = (
        f"<div class='headlines'>"
        f"<div class='headline'><span class='label'>Longest agent gap</span>"
        f"<span class='value {_gap_class(attempt.longest_agent_gap, warn=warn, alert=alert)}'>"
        f"{_fmt_secs(attempt.longest_agent_gap)}</span></div>"
        f"<div class='headline'><span class='label'>Longest customer gap</span>"
        f"<span class='value {_gap_class(attempt.longest_user_gap, warn=warn, alert=alert)}'>"
        f"{_fmt_secs(attempt.longest_user_gap)}</span></div>"
        f"<div class='headline'><span class='label'>Trailing silence after last turn</span>"
        f"<span class='value {_gap_class(attempt.trailing_silence_seconds, warn=warn, alert=alert)}'>"
        f"{_fmt_secs(attempt.trailing_silence_seconds)}</span></div>"
        f"</div>"
    )

    if not attempt.transcript_turns:
        body = "<p class='dim'>No transcript turns recorded.</p>"
    else:
        rows = []
        for t in attempt.transcript_turns:
            gap_cls = _gap_class(t.gap_before_seconds, warn=warn, alert=alert)
            role_cls = "row-agent" if t.role == "agent" else "row-user"
            speaker = "Kate" if t.role == "agent" else "Customer"
            rows.append(
                f"<tr class='{role_cls}'>"
                f"<td class='right mono'>#{t.index}</td>"
                f"<td class='mono'>{t.time_in_call_seconds:>6.1f}s</td>"
                f"<td class='right mono {gap_cls}'>{_fmt_secs(t.gap_before_seconds)}</td>"
                f"<td>{_esc(speaker)}</td>"
                f"<td class='msg'>{_esc(t.message)}</td>"
                f"</tr>"
            )
        body = (
            "<table class='turns'><thead><tr>"
            "<th class='right'>#</th><th>t (in call)</th>"
            "<th class='right'>gap before</th><th>speaker</th><th>message</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )

    return f"<section class='attempt'>{summary}{headline}{body}</section>"


def render_html(report: CaseReport, *, warn: float, alert: float, source_path: Path) -> str:
    """Build the full HTML document for one ``CaseReport``."""
    phases_html = _render_phases_table(report.phases)
    attempts_html = "".join(
        _render_attempt_section(a, warn=warn, alert=alert) for a in report.attempts
    )
    if not attempts_html:
        attempts_html = "<p class='dim'>No call attempts recorded.</p>"

    closed = _fmt_dt(report.closed_at) if report.closed_at else "—"

    return f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'>
<title>Latency report — {_esc(report.case_id)}</title>
<style>
  :root {{
    --bg: #0e1116; --panel: #161b22; --line: #2a313c;
    --text: #e6edf3; --dim: #8b949e; --mute: #6e7681;
    --accent: #4ea1ff; --warn: #d29922; --alert: #f85149;
    --good: #3fb950; --row-agent: #1f2a3a; --row-user: #1c2630;
    --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  }}
  body {{
    margin: 0; padding: 24px; background: var(--bg); color: var(--text);
    font: 13px/1.45 -apple-system, "Segoe UI", Inter, Roboto, sans-serif;
  }}
  h1 {{ margin: 0 0 4px; font-size: 18px; }}
  h2 {{ margin: 24px 0 8px; font-size: 14px; text-transform: uppercase;
        letter-spacing: 0.06em; color: var(--dim); }}
  .meta {{ color: var(--dim); font-size: 12px; margin-bottom: 18px; }}
  .meta .mono {{ color: var(--text); }}
  .legend {{ font-size: 11px; color: var(--mute); margin-top: 6px; }}
  .legend span {{ padding: 1px 6px; border-radius: 3px; margin-right: 6px; font-family: var(--mono); }}
  .legend .gap-warn {{ background: rgba(210,153,34,0.18); color: var(--warn); }}
  .legend .gap-alert {{ background: rgba(248,81,73,0.18); color: var(--alert); }}
  table {{ border-collapse: collapse; width: 100%; background: var(--panel);
            border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line);
            vertical-align: top; }}
  th {{ background: #1c232c; font-size: 11px; text-transform: uppercase;
        letter-spacing: 0.04em; color: var(--dim); font-weight: 600; }}
  tr:last-child td {{ border-bottom: none; }}
  .right {{ text-align: right; }}
  .mono {{ font-family: var(--mono); font-size: 12px; }}
  .dim {{ color: var(--dim); }}
  .msg {{ font-family: var(--mono); font-size: 12px; }}
  .row-agent {{ background: var(--row-agent); }}
  .row-user  {{ background: var(--row-user); }}
  .gap-warn  {{ color: var(--warn); font-weight: 600; }}
  .gap-alert {{ color: var(--alert); font-weight: 700; }}
  .attempt {{ border: 1px solid var(--line); border-radius: 8px; padding: 14px;
              background: var(--panel); margin-bottom: 18px; }}
  .attempt-summary {{ display: flex; flex-wrap: wrap; gap: 16px; font-size: 12px;
                       color: var(--dim); margin-bottom: 10px; }}
  .attempt-summary b {{ color: var(--text); }}
  .headlines {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
                margin-bottom: 12px; }}
  .headline {{ background: #1c232c; border: 1px solid var(--line); border-radius: 6px;
                padding: 10px 12px; }}
  .headline .label {{ display: block; font-size: 10px; color: var(--mute);
                       text-transform: uppercase; letter-spacing: 0.06em; }}
  .headline .value {{ font-family: var(--mono); font-size: 18px; font-weight: 600;
                       margin-top: 4px; display: inline-block; }}
  .headline .value.gap-warn  {{ color: var(--warn); }}
  .headline .value.gap-alert {{ color: var(--alert); }}
  footer {{ margin-top: 28px; color: var(--mute); font-size: 11px; }}
</style></head><body>
  <h1>Latency report — <span class='mono'>{_esc(report.case_id)}</span></h1>
  <div class='meta'>
    correlation_id <span class='mono'>{_esc(report.correlation_id)}</span> ·
    state <span class='mono'>{_esc(report.state)}</span> ·
    created <span class='mono'>{_fmt_dt(report.created_at)}</span> ·
    closed <span class='mono'>{_esc(closed)}</span>
  </div>

  <h2>Case lifecycle (wall-clock)</h2>
  {phases_html}
  <div class='legend'>
    Gap thresholds:
    <span class='gap-warn'>≥ {_fmt_secs(warn)} warn</span>
    <span class='gap-alert'>≥ {_fmt_secs(alert)} alert</span>
  </div>

  <h2>Call attempts</h2>
  {attempts_html}

  <footer>
    Generated from <span class='mono'>{_esc(source_path)}</span>.
    Latency = time between consecutive turn-starts; trailing silence =
    seconds from the last turn-start until the call ended.
  </footer>
</body></html>
"""


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def _resolve_case_path(project_root: Path, case_id: str | None) -> Path:
    cases_dir = project_root / "fixtures" / "cases"
    if case_id:
        path = cases_dir / f"{case_id}.json"
        if not path.exists():
            sys.exit(f"No case file at {path}")
        return path
    candidates = sorted(
        (p for p in cases_dir.glob("case_*.json")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        sys.exit(f"No case files in {cases_dir}")
    return candidates[0]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", help="Specific case id (default: most recent)")
    parser.add_argument(
        "--project-root", type=Path, default=Path.cwd(),
        help="Project root (default: cwd)",
    )
    parser.add_argument(
        "--warn", type=float, default=WARN_GAP_SECONDS_DEFAULT,
        help=f"Warn threshold in seconds (default: {WARN_GAP_SECONDS_DEFAULT})",
    )
    parser.add_argument(
        "--alert", type=float, default=ALERT_GAP_SECONDS_DEFAULT,
        help=f"Alert threshold in seconds (default: {ALERT_GAP_SECONDS_DEFAULT})",
    )
    parser.add_argument(
        "--open", action="store_true",
        help="Open the resulting HTML file in your default browser",
    )
    args = parser.parse_args(argv)

    case_path = _resolve_case_path(args.project_root, args.case_id)
    case = json.loads(case_path.read_text(encoding="utf-8"))
    report = build_case_report(case)
    html = render_html(report, warn=args.warn, alert=args.alert, source_path=case_path)

    out_dir = (args.project_root / "reports").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"latency_{report.case_id}.html"
    out_path.write_text(html, encoding="utf-8")

    print(str(out_path))
    if args.open:
        subprocess.run(["open", str(out_path)], check=False)


if __name__ == "__main__":
    main()
