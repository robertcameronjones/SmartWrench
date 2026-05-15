"""take_turn: the one business function."""

from __future__ import annotations

from datetime import UTC, datetime

from sms_adapter import SmsContext, Turn, TurnRole, take_turn


class _RecordingLlm:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.system_seen = ""
        self.history_seen: tuple[Turn, ...] = ()

    def __call__(self, *, system: str, history: tuple[Turn, ...]) -> str:
        self.system_seen = system
        self.history_seen = history
        return self.reply


def test_opening_turn_with_empty_history_sends_bootstrap_user_msg(
    prompt_paths, context: SmsContext
) -> None:
    llm = _RecordingLlm(reply="Hi Sarah! It's Kate from Westside Toyota.")
    out = take_turn(
        ctx=context,
        history=(),
        inbound=None,
        prompt_paths=prompt_paths,
        llm_complete=llm,
    )
    assert out == "Hi Sarah! It's Kate from Westside Toyota."
    # Bootstrap user turn was injected
    assert len(llm.history_seen) == 1
    assert llm.history_seen[0].role is TurnRole.USER


def test_system_prompt_is_substituted_with_context_variables(
    prompt_paths, context: SmsContext
) -> None:
    llm = _RecordingLlm(reply="ack")
    take_turn(
        ctx=context,
        history=(),
        inbound=None,
        prompt_paths=prompt_paths,
        llm_complete=llm,
    )
    assert "Sarah" in llm.system_seen
    assert "Toyota" in llm.system_seen
    assert "Camry" in llm.system_seen
    assert "{{customer_first_name}}" not in llm.system_seen


def test_system_prompt_includes_sms_overlay(prompt_paths, context: SmsContext) -> None:
    llm = _RecordingLlm(reply="ack")
    take_turn(
        ctx=context,
        history=(),
        inbound=None,
        prompt_paths=prompt_paths,
        llm_complete=llm,
    )
    assert "You text first" in llm.system_seen


def test_inbound_turn_uses_provided_history_unchanged(
    prompt_paths, context: SmsContext
) -> None:
    llm = _RecordingLlm(reply="next")
    history = (
        Turn(role=TurnRole.ASSISTANT, text="hi", timestamp=datetime.now(UTC)),
        Turn(role=TurnRole.USER, text="yes", timestamp=datetime.now(UTC)),
    )
    take_turn(
        ctx=context,
        history=history,
        inbound="yes",
        prompt_paths=prompt_paths,
        llm_complete=llm,
    )
    # The history is passed through; no bootstrap turn injected.
    assert llm.history_seen == history
