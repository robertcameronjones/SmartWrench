"""Tests for the v2 ``CallManager`` surface on ``_LiveCallSession``.

The voice adapter has always exposed ``place(case)`` (the v1
``CallSession`` Protocol). Phase 5 adds ``start(*, case, stage,
attempt_number)`` (the v2 ``CallManager`` Protocol) and rebases
``place`` onto it. These tests pin both directions:

- The class structurally satisfies both Protocols.
- ``start`` records ``stage`` and ``attempt_number`` in the call's
  ``CaseEvent`` audit trail and uses the explicit ``attempt_number``
  (not ``case.attempt_count + 1``) the v2 driver will pass.
- ``place`` keeps the v1 attempt-number convention so the existing
  ``CaseManager`` loop is unaffected.

The ElevenLabs SDK is stubbed at the client level. No outbound calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, final

import pytest

from guidepoint.case import (
    CallManager,
    CallStage,
    CaseEvent,
    JsonCasePaths,
    build_json_case_repository,
    build_live_call_session,
    build_voice_call_manager,
)
from guidepoint.case._call_session import CallSession, _LiveCallSession
from guidepoint.events import build_event_bus

from tests.case._helpers import FixedClock, sample_case


# --------------------------------------------------------------------------- #
# Fakes for the ElevenLabs SDK surface ``_LiveCallSession`` touches.          #
# --------------------------------------------------------------------------- #


@dataclass
class _FakeTwilio:
    """Stand-in for ``client.conversational_ai.twilio``."""

    outbound_calls: list[dict[str, Any]] = field(default_factory=list)

    def outbound_call(self, **kwargs: Any) -> Any:
        self.outbound_calls.append(kwargs)
        return _FakeConvCreate(conversation_id="conv_test_1")


@dataclass
class _FakeConvCreate:
    conversation_id: str


@dataclass
class _FakeConvGet:
    status: str = "done"
    transcript: tuple[Any, ...] = ()
    analysis: Any = None
    metadata: Any = None
    audio_url: str = ""


@dataclass
class _FakeConversations:
    """Stand-in for ``client.conversational_ai.conversations``."""

    response: _FakeConvGet = field(default_factory=_FakeConvGet)

    def get(self, *, conversation_id: str) -> _FakeConvGet:
        return self.response


@dataclass
class _FakeConvAi:
    twilio: _FakeTwilio = field(default_factory=_FakeTwilio)
    conversations: _FakeConversations = field(default_factory=_FakeConversations)


@dataclass
class _FakeClient:
    conversational_ai: _FakeConvAi = field(default_factory=_FakeConvAi)


# --------------------------------------------------------------------------- #
# Builders share a configuration helper to keep tests narrow.                 #
# --------------------------------------------------------------------------- #


def _build(
    tmp_path: Path,
    *,
    client: _FakeClient | None = None,
):
    repo = build_json_case_repository(paths=JsonCasePaths(cases_dir=tmp_path / "cases"))
    bus = build_event_bus(payload_type=CaseEvent)
    clock = FixedClock(instant=datetime(2026, 5, 10, 12, 0, tzinfo=UTC))
    fake_client = client or _FakeClient()
    session = build_live_call_session(
        client=fake_client,  # type: ignore[arg-type]
        agent_id="agent",
        phone_number_id="pn",
        case_repo=repo,
        bus=bus,
        clock=clock,
    )
    return session, repo, fake_client


# --------------------------------------------------------------------------- #
# Protocol conformance.                                                       #
# --------------------------------------------------------------------------- #


def test_live_call_session_is_a_call_manager(tmp_path: Path) -> None:
    session, _repo, _client = _build(tmp_path)
    # ``CallManager`` is a Protocol; structural conformance is what we
    # care about. Pin both the attribute existence and the explicit
    # builder alias.
    assert hasattr(session, "start")
    assert callable(session.start)
    cm: CallManager = session  # type: ignore[assignment]
    assert cm is session


def test_live_call_session_is_still_a_call_session(tmp_path: Path) -> None:
    """v1 Protocol must keep working."""

    session, _repo, _client = _build(tmp_path)
    assert hasattr(session, "place")
    cs: CallSession = session  # type: ignore[assignment]
    assert cs is session


def test_build_voice_call_manager_returns_call_manager(tmp_path: Path) -> None:
    repo = build_json_case_repository(paths=JsonCasePaths(cases_dir=tmp_path / "cases"))
    bus = build_event_bus(payload_type=CaseEvent)
    clock = FixedClock(instant=datetime(2026, 5, 10, 12, 0, tzinfo=UTC))
    mgr = build_voice_call_manager(
        client=_FakeClient(),  # type: ignore[arg-type]
        agent_id="agent",
        phone_number_id="pn",
        case_repo=repo,
        bus=bus,
        clock=clock,
    )
    assert hasattr(mgr, "start")
    assert hasattr(mgr, "place")  # also still a CallSession


# --------------------------------------------------------------------------- #
# start() — stage + attempt_number are honoured.                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_start_records_stage_and_attempt_number_in_events(
    tmp_path: Path,
) -> None:
    session, repo, _client = _build(tmp_path)
    case = sample_case()
    repo.save(case)

    # Speed up the poll loop — we want the very first poll to find "done".
    inner: _LiveCallSession = session  # type: ignore[assignment]
    inner._settings = inner._settings.__class__(  # type: ignore[reportPrivateUsage]
        poll_interval_seconds=0.0, max_wait_seconds=1.0
    )

    await session.start(case=case, stage=CallStage.INITIAL_REMINDER, attempt_number=7)

    fresh = repo.get(case.case_id)
    dialing = [e for e in fresh.events if e.event == "call.dialing"]
    assert dialing, "expected call.dialing event"
    assert "stage=initial_reminder" in dialing[0].detail
    assert dialing[0].attempt_number == 7

    placed = [e for e in fresh.events if e.event == "call.placed"]
    assert placed
    assert "stage=initial_reminder" in placed[0].detail
    assert placed[0].attempt_number == 7


@pytest.mark.asyncio
async def test_place_uses_v1_attempt_number_convention(tmp_path: Path) -> None:
    """v1 ``place(case)`` must keep computing attempt_number = case.attempt_count + 1."""

    session, repo, _client = _build(tmp_path)
    case = sample_case().model_copy(update={"attempt_count": 3})
    repo.save(case)

    inner: _LiveCallSession = session  # type: ignore[assignment]
    inner._settings = inner._settings.__class__(  # type: ignore[reportPrivateUsage]
        poll_interval_seconds=0.0, max_wait_seconds=1.0
    )

    await session.place(case)

    fresh = repo.get(case.case_id)
    dialing = [e for e in fresh.events if e.event == "call.dialing"]
    assert dialing
    assert dialing[0].attempt_number == 4
    assert "stage=outreach" in dialing[0].detail
