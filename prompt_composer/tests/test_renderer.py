"""Unit tests for the substitution engine."""

from __future__ import annotations

import pytest

from prompt_composer._renderer import (
    MissingPlaceholderError,
    find_placeholders,
    substitute,
)


def test_find_placeholders_returns_keys_only() -> None:
    text = "Hi {{customer_first_name}}, your {{vehicle_make}} {{vehicle_model}} is ready."
    assert find_placeholders(text) == frozenset(
        {"customer_first_name", "vehicle_make", "vehicle_model"}
    )


def test_find_placeholders_handles_whitespace_inside_braces() -> None:
    assert find_placeholders("{{ foo }} and {{bar}}") == frozenset({"foo", "bar"})


def test_find_placeholders_ignores_single_braces_and_random_text() -> None:
    assert find_placeholders("a {single} brace and a {{ok_key}}") == frozenset({"ok_key"})


def test_substitute_replaces_every_occurrence() -> None:
    text = "Hi {{name}}. Your car ({{make}} {{model}}) — yes, the {{model}} — is ready."
    result, used = substitute(text, {"name": "Sarah", "make": "Toyota", "model": "Camry"})
    assert result == "Hi Sarah. Your car (Toyota Camry) — yes, the Camry — is ready."
    assert used == frozenset({"name", "make", "model"})


def test_substitute_raises_on_missing_placeholder() -> None:
    with pytest.raises(MissingPlaceholderError) as exc:
        substitute("Hi {{name}}, drive your {{vehicle_make}}.", {"name": "Sarah"})
    assert exc.value.missing == frozenset({"vehicle_make"})


def test_missing_error_lists_all_missing_keys_sorted() -> None:
    with pytest.raises(MissingPlaceholderError) as exc:
        substitute("{{z}} {{a}} {{m}}", {})
    assert exc.value.missing == frozenset({"a", "m", "z"})
    msg = str(exc.value)
    assert "'a'" in msg and "'m'" in msg and "'z'" in msg


def test_substitute_does_not_recurse_into_substituted_value() -> None:
    """A value that itself contains ``{{key}}`` is left alone — no recursion.

    Otherwise an attacker controlling a customer name field could inject
    further substitutions. The substituted value is text, not a template.
    """
    result, _ = substitute("Hi {{name}}.", {"name": "{{evil}}"})
    assert result == "Hi {{evil}}."


def test_substitute_with_no_placeholders_returns_text_unchanged() -> None:
    text = "No placeholders here."
    result, used = substitute(text, {"unused": "value"})
    assert result == text
    assert used == frozenset()
