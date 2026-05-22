"""End-to-end smoke for the SMS arm of the unified Case lifecycle.

Round trip:

1. Build the simulator app with a fake ``SmsCallSession`` wired into
   the channel router. The voice ``CallSession`` is faked too so
   ``build_app`` doesn't reach for ElevenLabs.
2. POST ``/api/fire`` with ``channel="sms"`` -> assert the fake
   Twilio sender saw the opening message and the routing store
   bound the customer's phone to the case_id.
3. POST a fake Twilio inbound webhook to ``/sms`` -> assert the
   fake Twilio sender saw the LLM-driven reply and the history file
   has 3 turns (open / inbound / reply).

No network, no real LLM, no real Twilio.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from guidepoint.case import Case, CallOutcome, CaseEvent, CaseId
from guidepoint.events import build_event_bus
from prompt_composer import PromptPaths
from sms_adapter import (
    HistoryStore,
    RoutingStore,
    SmsCallSession,
    Turn,
    build_json_history_store,
    build_json_routing_store,
    build_sms_call_session,
)

from simulator import build_app
from tests._helpers import (
    FixedClock,
    StubProbe,
    UserClient,
    healthy_status,
    seed_master_data,
)

TEST_USER = "demo"
TEST_PASSWORD = "demo"


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


@final
@dataclass
class FakeTwilio:
    """Records every outbound SMS; returns a deterministic fake SID."""

    sent: list[tuple[str, str]] = field(default_factory=list)
    counter: int = 0

    def __call__(self, *, to: str, body: str) -> str:
        self.counter += 1
        sid = f"SM{self.counter:032x}"
        self.sent.append((to, body))
        return sid


@final
@dataclass
class FakeLlm:
    """Returns canned replies; records every call."""

    replies: list[str]
    calls: list[tuple[str, tuple[Turn, ...]]] = field(default_factory=list)

    def __call__(self, *, system: str, history: tuple[Turn, ...]) -> str:
        self.calls.append((system, history))
        if not self.replies:
            raise AssertionError("FakeLlm exhausted; no canned reply available")
        return self.replies.pop(0)


@final
class FakeCallSession:
    """Voice CallSession that should never be called in SMS tests."""

    async def place(self, case: Case) -> CallOutcome:
        raise AssertionError("voice CallSession invoked in an SMS-only test")


# --------------------------------------------------------------------------- #
# App fixture                                                                 #
# --------------------------------------------------------------------------- #


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_test_app(
    tmp_path: Path,
    *,
    fake_twilio: FakeTwilio,
    fake_llm: FakeLlm,
) -> tuple[FastAPI, SmsCallSession, RoutingStore, HistoryStore]:
    seed_master_data(tmp_path)
    clock = FixedClock()
    bus = build_event_bus(payload_type=CaseEvent)

    history = build_json_history_store(root=tmp_path / "data" / "sms" / "history")
    routing = build_json_routing_store(path=tmp_path / "data" / "sms" / "routing.json")

    # case_repo is the same shared one ``build_app`` builds by
    # default from JsonCasePaths.for_root(tmp_path); we have to build
    # the SMS session with that same instance so its emitted events
    # land on the case the route handler saved.
    from guidepoint.case import JsonCasePaths, build_json_case_repository

    case_repo = build_json_case_repository(paths=JsonCasePaths.for_root(tmp_path))

    sms_session = build_sms_call_session(
        twilio_send=fake_twilio,
        llm_complete=fake_llm,
        history=history,
        routing=routing,
        prompt_paths=PromptPaths(
            system=_REPO_ROOT / "11Labs" / "config" / "system-prompt.md",
            voice=_REPO_ROOT / "11Labs" / "config" / "voice.md",
            sms=_REPO_ROOT / "sms_adapter" / "config" / "sms.md",
        ),
        case_repo=case_repo,
        bus=bus,
        clock=clock,
        event_log_path=None,
        # Keep the inactivity timeout short so the
        # "no inbound -> UNREACHABLE" path is testable without sleeping
        # 24 hours.
        inactivity_timeout=timedelta(seconds=30),
    )

    app = build_app(
        project_root=tmp_path,
        clock=clock,
        bus=bus,
        case_repo=case_repo,
        probe=StubProbe(status=healthy_status(clock=clock)),
        call_session=FakeCallSession(),
        sms_session=sms_session,
        sms_routing=routing,
    )
    return app, sms_session, routing, history


@pytest.fixture
def fake_twilio() -> FakeTwilio:
    return FakeTwilio()


@pytest.fixture
def fake_llm() -> FakeLlm:
    # Second reply must contain BOTH a confirmation verb (matches
    # ``_CONFIRMATION_VERBS`` in ``sms_adapter._call_session``) AND
    # one of the offered slot display strings for the session's
    # booking heuristic to trip and terminate the conversation.
    # Tuesday's display is "Tuesday, May 12 - 8:30 AM".
    return FakeLlm(
        replies=[
            "Hi, this is Kate. Quick text about your Jeep service. Reply Y to schedule.",
            "Great, you're scheduled for Tuesday, May 12 - 8:30 AM. See you then!",
        ]
    )


@pytest.fixture
def client_with_sms(
    tmp_path: Path,
    fake_twilio: FakeTwilio,
    fake_llm: FakeLlm,
):
    """Yield a ``UserClient`` + the SMS triad inside an active ``TestClient`` context.

    The ``with TestClient(app)`` block is critical: it triggers FastAPI's
    lifespan and — more importantly — keeps the anyio blocking portal
    (and the asyncio loop running on it) alive across requests. Without
    it, recent Starlette versions tear the portal down after each
    request and cancel any pending background tasks; that hangs
    ``_SmsCallSession.place()`` mid-``to_thread`` and surfaces as
    "routing entry not found" / "twilio.sent empty" race failures.
    """
    app, session, routing, history = _build_test_app(
        tmp_path, fake_twilio=fake_twilio, fake_llm=fake_llm
    )
    with TestClient(app) as raw:
        yield (
            UserClient(raw, user=TEST_USER, password=TEST_PASSWORD),
            session,
            routing,
            history,
        )


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.01) -> None:
    """Spin briefly until ``predicate()`` returns truthy or timeout fires.

    Bumped from 2s to 5s because the SMS background task runs on
    Starlette's portal loop in a separate thread; on a busy laptop the
    LLM + Twilio + history + event-bus roundtrip can occasionally tail
    past 2s and we don't want false negatives. The predicates wake on
    cheap polls every 10ms so the steady-state cost is unchanged.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for predicate")


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


class TestSmsFire:
    def test_fire_sms_sends_opening_and_binds_phone(
        self,
        client_with_sms: tuple[UserClient, SmsCallSession, RoutingStore, HistoryStore],
        fake_twilio: FakeTwilio,
    ) -> None:
        client, session, routing, history = client_with_sms
        res = client.post(
            "/api/fire",
            json={
                "service_type": "maintenance",
                "service_summary": "oil change",
                "narrative": "",
                "channel": "sms",
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        case_id = body["case_id"]
        # case_ids now come from the case factory ("case_<hex>") since
        # SMS goes through the same CaseManager as voice. The legacy
        # "sms_<hex>" synthetic prefix is gone.
        assert case_id.startswith("case_")

        # The opening is produced asynchronously by the background
        # _SmsCallSession.place() task spawned from start(). Wait for
        # the FULL ``_send_opening`` cycle (twilio_send + history.append
        # + _emit) to complete, not just one step — under load the
        # post-send writes can lag the twilio side by a tick.
        _wait_until(
            lambda: len(fake_twilio.sent) >= 1 and len(history.load(case_id)) >= 1
        )

        # Twilio saw the opening message addressed to the customer's phone.
        to, opening = fake_twilio.sent[0]
        assert to == "+13135550000"  # SAMPLE_CUSTOMER phone
        assert "Kate" in opening

        # The routing store bound the customer's phone to this case_id,
        # tagged with the operator's user_id and channel.
        entry = routing.find_entry("+13135550000")
        assert entry is not None
        assert entry.conversation_id == case_id
        assert entry.user_id == TEST_USER
        assert entry.channel == "sms"

        # The SmsCallSession has an active conversation for this case.
        assert session.has_active(CaseId(case_id))

        # History file has the assistant's opening turn.
        turns = history.load(case_id)
        assert len(turns) == 1
        assert turns[0].role.value == "assistant"
        assert turns[0].text == opening

    def test_fire_sms_503_when_no_sms_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seed_master_data(tmp_path)
        clock = FixedClock()
        # Clear Twilio env vars so ``build_sms_session`` returns
        # ``(None, None)`` and the Fire route 503s on channel=sms.
        # The dev machine has these set from ``.env``; without this
        # the test would attempt a real Twilio session and fail
        # somewhere unrelated.
        for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
            monkeypatch.delenv(var, raising=False)
        app = build_app(
            project_root=tmp_path,
            clock=clock,
            bus=build_event_bus(payload_type=CaseEvent),
            probe=StubProbe(status=healthy_status(clock=clock)),
            call_session=FakeCallSession(),
        )
        with TestClient(app) as raw:
            client = UserClient(raw, user=TEST_USER, password=TEST_PASSWORD)
            res = client.post(
                "/api/fire",
                json={
                    "service_type": "maintenance",
                    "service_summary": "oil change",
                    "narrative": "",
                    "channel": "sms",
                },
            )
        assert res.status_code == 503, res.text


class TestSmsInboundWebhook:
    def test_inbound_routes_through_session_and_sends_reply(
        self,
        client_with_sms: tuple[UserClient, SmsCallSession, RoutingStore, HistoryStore],
        fake_twilio: FakeTwilio,
        fake_llm: FakeLlm,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, session, routing, history = client_with_sms

        # Skip Twilio signature validation on the webhook for this test.
        # sms.server reads this per request, so a setenv is sufficient.
        monkeypatch.setenv("SKIP_SIGNATURE_VALIDATION", "1")

        # Open the SMS conversation first.
        fire_res = client.post(
            "/api/fire",
            json={
                "service_type": "maintenance",
                "service_summary": "oil change",
                "narrative": "",
                "channel": "sms",
            },
        )
        assert fire_res.status_code == 200, fire_res.text
        case_id = fire_res.json()["case_id"]
        # Wait for opening to be sent so the session is ready to
        # receive an inbound. (start() returns once the case hits
        # CALLING; the opener happens inside the background task.)
        _wait_until(lambda: len(fake_twilio.sent) >= 1)

        # Customer texts back. Webhook is global (no user param) — raw.
        # The path Twilio is configured for is bare ``/sms`` — that's
        # also where build_app mounts ``inbound_sms``.
        inbound = client.raw.post(
            "/sms",
            data={
                "From": "+13135550000",
                "To": "+12485551234",
                "Body": "yes please",
                "MessageSid": "SMfake_inbound_001",
                "NumMedia": "0",
            },
        )
        assert inbound.status_code == 200, inbound.text
        # TwiML response is empty (the pipe never replies inline).
        assert "<Response/>" in inbound.text

        # Wait for the SMS session loop to dequeue, call the fake LLM,
        # and hand the reply to fake Twilio.
        _wait_until(lambda: len(fake_twilio.sent) >= 2)

        reply_to, reply_body = fake_twilio.sent[1]
        assert reply_to == "+13135550000"
        assert "Tuesday" in reply_body  # second canned LLM reply
        assert "scheduled" in reply_body  # contains confirmation verb

        # The LLM's second invocation saw the inbound user turn in history.
        _system, second_history = fake_llm.calls[-1]
        assert any(t.role.value == "user" and t.text == "yes please" for t in second_history)

        # History file grew to 3 turns: opener / inbound / reply.
        # The session writes history asynchronously, so poll briefly.
        _wait_until(lambda: len(history.load(case_id)) >= 3)
        turns = history.load(case_id)
        assert len(turns) == 3
        assert [t.role.value for t in turns] == ["assistant", "user", "assistant"]
        assert turns[1].text == "yes please"
        assert turns[2].text == reply_body

        # The reply confirmed a slot, so the session terminated and the
        # routing was unbound. has_active flips off too.
        _wait_until(lambda: not session.has_active(CaseId(case_id)))
        assert routing.find_conversation_id("+13135550000") is None

    def test_inbound_unknown_phone_is_logged_not_raised(
        self,
        client_with_sms: tuple[UserClient, SmsCallSession, RoutingStore, HistoryStore],
        fake_twilio: FakeTwilio,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _session, _routing, _history = client_with_sms
        monkeypatch.setenv("SKIP_SIGNATURE_VALIDATION", "1")

        # No prior /api/fire — phone has no bound conversation.
        res = client.raw.post(
            "/sms",
            data={
                "From": "+19995550000",
                "To": "+12485551234",
                "Body": "hello?",
                "MessageSid": "SMfake_orphan_001",
                "NumMedia": "0",
            },
        )
        # Webhook still 200s so Twilio doesn't retry.
        assert res.status_code == 200, res.text
        assert fake_twilio.sent == []  # no reply attempted

    def test_stop_keyword_marks_case_declined(
        self,
        client_with_sms: tuple[UserClient, SmsCallSession, RoutingStore, HistoryStore],
        fake_twilio: FakeTwilio,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, session, _routing, _history = client_with_sms
        monkeypatch.setenv("SKIP_SIGNATURE_VALIDATION", "1")

        fire_res = client.post(
            "/api/fire",
            json={
                "service_type": "maintenance",
                "service_summary": "oil change",
                "narrative": "",
                "channel": "sms",
            },
        )
        assert fire_res.status_code == 200
        case_id = fire_res.json()["case_id"]
        _wait_until(lambda: len(fake_twilio.sent) >= 1)

        # Customer texts STOP — universally recognized opt-out keyword.
        inbound = client.raw.post(
            "/sms",
            data={
                "From": "+13135550000",
                "To": "+12485551234",
                "Body": "STOP",
                "MessageSid": "SMfake_stop_001",
                "NumMedia": "0",
            },
        )
        assert inbound.status_code == 200

        # Session terminates without calling the LLM again — only the
        # opening turn went through.
        _wait_until(lambda: not session.has_active(CaseId(case_id)))
        # No reply should have been sent in response to STOP.
        assert len(fake_twilio.sent) == 1

        # Case lands in DECLINED.
        case_blob = client.get(f"/api/cases/{case_id}").json()
        assert case_blob["state"] == "declined"


# SmsCallSession unit tests (no FastAPI) live in
# ``sms_adapter/tests/test_call_session.py`` since the class is owned
# by sms_adapter. This file only covers the simulator wiring.
