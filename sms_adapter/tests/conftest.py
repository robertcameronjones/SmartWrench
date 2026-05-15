"""Shared fixtures for sms_adapter tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from prompt_composer import PromptPaths
from sms_adapter import (
    HistoryStore,
    LlmComplete,
    RoutingStore,
    SmsContext,
    SmsDeps,
    Turn,
    TurnRole,
    TwilioSend,
    build_json_history_store,
    build_json_routing_store,
)


@pytest.fixture
def prompt_paths(tmp_path: Path) -> PromptPaths:
    system = tmp_path / "system.md"
    voice = tmp_path / "voice.md"
    sms = tmp_path / "sms.md"
    system.write_text(
        "You are Kate. Customer is {{customer_first_name}}, vehicle is "
        "{{vehicle_year}} {{vehicle_make}} {{vehicle_model}}.",
        encoding="utf-8",
    )
    voice.write_text("", encoding="utf-8")
    sms.write_text("You text first. Be brief.", encoding="utf-8")
    return PromptPaths(system=system, voice=voice, sms=sms)


@pytest.fixture
def context() -> SmsContext:
    return SmsContext(
        conversation_id="conv_test_001",
        customer_phone="+13135551212",
        variables={
            "customer_first_name": "Sarah",
            "vehicle_year": "2020",
            "vehicle_make": "Toyota",
            "vehicle_model": "Camry",
        },
    )


@pytest.fixture
def history_store(tmp_path: Path) -> HistoryStore:
    return build_json_history_store(root=tmp_path / "history")


@pytest.fixture
def routing_store(tmp_path: Path) -> RoutingStore:
    return build_json_routing_store(path=tmp_path / "routing.json")


class FakeTwilio:
    """Records every send. Returns a deterministic SID."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []  # (to, body)

    def send(self, *, to: str, body: str) -> str:
        self.sent.append((to, body))
        return f"SM_fake_{len(self.sent):04d}"


class FakeLlm:
    """Returns scripted replies in order. Records every call."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[str, tuple[Turn, ...]]] = []  # (system, history)

    def complete(self, *, system: str, history: tuple[Turn, ...]) -> str:
        self.calls.append((system, history))
        if not self._replies:
            raise RuntimeError("FakeLlm: no more scripted replies")
        return self._replies.pop(0)


@pytest.fixture
def fake_twilio() -> FakeTwilio:
    return FakeTwilio()


@pytest.fixture
def make_deps(
    prompt_paths: PromptPaths,
    history_store: HistoryStore,
    routing_store: RoutingStore,
    fake_twilio: FakeTwilio,
):
    """Factory that builds SmsDeps with a configurable LLM script."""

    def _make(llm_replies: list[str]) -> tuple[SmsDeps, FakeLlm]:
        llm = FakeLlm(llm_replies)
        deps = SmsDeps(
            twilio_send=fake_twilio.send,
            llm_complete=llm.complete,
            history=history_store,
            routing=routing_store,
            prompt_paths=prompt_paths,
        )
        return deps, llm

    return _make
