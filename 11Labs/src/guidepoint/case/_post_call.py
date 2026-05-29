"""Cleanup after the party — turn a ``PostCallReport`` into a ``CallOutcome``.

Per ADR 0006 we use the ElevenLabs native Twilio integration, so we
don't observe the conversation while it's happening. We learn what
was said by polling the conversation API after the call ends, then
shaping the response into a ``PostCallReport``. From there, three
things have to happen with it:

1. Format the transcript into a single auditable string.
2. Append a ``conversation.transcript_received`` event to the case so
   any live UI watching the bus sees the call closed out.
3. Convert the report into the ``CallOutcome`` ``CaseManager`` reads
   to walk the case to its terminal state.

This module is the single place those three things happen. Pure
functions where possible; the one I/O helper takes the repository and
bus as explicit dependencies. ``_LiveCallSession`` is the only
caller.
"""

from __future__ import annotations

import secrets

from guidepoint.case._models import (
    CallOutcome,
    CallResult,
    Case,
    CaseEvent,
    PostCallReport,
)
from guidepoint.case._repository import CaseRepository
from guidepoint.clock import Clock
from guidepoint.events import EventBus

_CaseEventBus = EventBus[CaseEvent]


def _format_transcript(report: PostCallReport) -> str:
    """One line per turn: ``[  2.5s] Kate: hello``."""
    if not report.transcript:
        return ""
    lines: list[str] = []
    for turn in report.transcript:
        speaker = "Kate" if turn.role == "agent" else "Customer"
        lines.append(f"[{turn.time_in_call_seconds:6.1f}s] {speaker}: {turn.message}")
    return "\n".join(lines)


def _report_to_outcome(report: PostCallReport) -> CallOutcome:
    """Status ``done`` → ``answered``, ``failed`` → ``error``. Everything else copies."""
    result: CallResult = "answered" if report.status == "done" else "error"
    return CallOutcome(
        result=result,
        business_outcome=report.business_outcome,
        booked_slot_id=report.booked_slot_id,
        booked_slot_display=report.booked_slot_display,
        elevenlabs_conversation_id=report.elevenlabs_conversation_id,
        started_at=report.started_at,
        ended_at=report.ended_at,
        duration_seconds=report.duration_seconds,
        transcript=_format_transcript(report),
        recording_url=report.recording_url,
        error_detail=report.error_detail,
    )


async def ingest_post_call_report(
    *,
    case: Case,
    attempt_number: int,
    report: PostCallReport,
    case_repo: CaseRepository,
    bus: _CaseEventBus,
    clock: Clock,
) -> CallOutcome:
    """Persist the closing audit event, publish it, return the outcome.

    Called by ``CallSession`` once the call ends — by the stub
    inline, by the live session from inside the webhook handler. Both
    paths land here so the case audit trail and the live event bus
    see the same shape regardless of source.
    """
    audit_event = CaseEvent(
        event_id=f"evt_{secrets.token_hex(6)}",
        case_id=case.case_id,
        correlation_id=case.correlation_id,
        attempt_number=attempt_number,
        timestamp=clock.now(),
        source="elevenlabs",
        level="info",
        event="conversation.transcript_received",
        detail=(
            f"conversation_id={report.elevenlabs_conversation_id} "
            f"turns={len(report.transcript)} "
            f"duration={report.duration_seconds:.1f}s "
            f"status={report.status}"
        ),
    )
    case_repo.append_event(case.case_id, audit_event)
    await bus.publish(audit_event)
    return _report_to_outcome(report)


__all__ = ["ingest_post_call_report"]
