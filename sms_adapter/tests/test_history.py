"""JSON history store: append & load."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sms_adapter import Turn, TurnRole, build_json_history_store


def _turn(role: TurnRole, text: str, sid: str = "") -> Turn:
    return Turn(role=role, text=text, timestamp=datetime.now(UTC), twilio_sid=sid)


def test_load_empty_returns_empty_tuple(tmp_path: Path) -> None:
    store = build_json_history_store(root=tmp_path)
    assert store.load("conv_001") == ()


def test_append_then_load_round_trip(tmp_path: Path) -> None:
    store = build_json_history_store(root=tmp_path)
    store.append("conv_001", _turn(TurnRole.ASSISTANT, "hi", "SM1"))
    store.append("conv_001", _turn(TurnRole.USER, "yes", ""))
    store.append("conv_001", _turn(TurnRole.ASSISTANT, "great", "SM2"))

    loaded = store.load("conv_001")
    assert len(loaded) == 3
    assert loaded[0].role is TurnRole.ASSISTANT and loaded[0].text == "hi"
    assert loaded[1].role is TurnRole.USER and loaded[1].text == "yes"
    assert loaded[2].twilio_sid == "SM2"


def test_separate_conversations_dont_mix(tmp_path: Path) -> None:
    store = build_json_history_store(root=tmp_path)
    store.append("a", _turn(TurnRole.ASSISTANT, "from-a"))
    store.append("b", _turn(TurnRole.ASSISTANT, "from-b"))
    assert [t.text for t in store.load("a")] == ["from-a"]
    assert [t.text for t in store.load("b")] == ["from-b"]


def test_path_traversal_in_conversation_id_is_neutralized(tmp_path: Path) -> None:
    store = build_json_history_store(root=tmp_path)
    store.append("../escape", _turn(TurnRole.ASSISTANT, "should not escape"))
    # The neutralized filename should sit inside tmp_path.
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].parent == tmp_path
