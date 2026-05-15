"""JSON routing store: bind / unbind / find."""

from __future__ import annotations

from pathlib import Path

from sms_adapter import build_json_routing_store


def test_find_unknown_returns_none(tmp_path: Path) -> None:
    store = build_json_routing_store(path=tmp_path / "r.json")
    assert store.find_conversation_id("+13135551212") is None


def test_bind_then_find(tmp_path: Path) -> None:
    store = build_json_routing_store(path=tmp_path / "r.json")
    store.bind(phone="+13135551212", conversation_id="conv_001")
    assert store.find_conversation_id("+13135551212") == "conv_001"


def test_unbind_removes(tmp_path: Path) -> None:
    store = build_json_routing_store(path=tmp_path / "r.json")
    store.bind(phone="+13135551212", conversation_id="conv_001")
    store.unbind(phone="+13135551212")
    assert store.find_conversation_id("+13135551212") is None


def test_unbind_nonexistent_phone_is_a_noop(tmp_path: Path) -> None:
    store = build_json_routing_store(path=tmp_path / "r.json")
    store.unbind(phone="+19998887777")  # no error


def test_persists_across_reload(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    s1 = build_json_routing_store(path=path)
    s1.bind(phone="+13135551212", conversation_id="conv_001")
    s2 = build_json_routing_store(path=path)
    assert s2.find_conversation_id("+13135551212") == "conv_001"
