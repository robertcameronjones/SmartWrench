"""Integration tests for ``build_prompt``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from prompt_composer import (
    Channel,
    MissingPlaceholderError,
    PromptPaths,
    build_prompt,
)


@dataclass(frozen=True)
class FakeCase:
    """Minimal stand-in for ``guidepoint.case.Case``."""

    variables: dict[str, str]

    def to_variables(self) -> dict[str, str]:
        return dict(self.variables)


@pytest.fixture
def paths(tmp_path: Path) -> PromptPaths:
    system = tmp_path / "system.md"
    voice = tmp_path / "voice.md"
    sms = tmp_path / "sms.md"
    system.write_text(
        "You are Kate, helping {{customer_first_name}} with their "
        "{{vehicle_make}} {{vehicle_model}}.\n",
        encoding="utf-8",
    )
    voice.write_text("Wait for the customer to speak first.\n", encoding="utf-8")
    sms.write_text(
        "You text first. Use compact replies. Address {{customer_first_name}} by name.\n",
        encoding="utf-8",
    )
    return PromptPaths(system=system, voice=voice, sms=sms)


def _case() -> FakeCase:
    return FakeCase(
        variables={
            "customer_first_name": "Sarah",
            "vehicle_make": "Toyota",
            "vehicle_model": "Camry",
        }
    )


def test_voice_render_concats_system_and_voice_in_order(paths: PromptPaths) -> None:
    rendered = build_prompt(case=_case(), channel=Channel.VOICE, paths=paths)
    assert rendered.channel is Channel.VOICE
    assert rendered.text == (
        "You are Kate, helping Sarah with their Toyota Camry.\n"
        "\n\n"
        "Wait for the customer to speak first.\n"
    )


def test_sms_render_concats_system_and_sms_in_order(paths: PromptPaths) -> None:
    rendered = build_prompt(case=_case(), channel=Channel.SMS, paths=paths)
    assert rendered.channel is Channel.SMS
    assert rendered.text == (
        "You are Kate, helping Sarah with their Toyota Camry.\n"
        "\n\n"
        "You text first. Use compact replies. Address Sarah by name.\n"
    )


def test_render_returns_unmodified_variables_dict(paths: PromptPaths) -> None:
    case = _case()
    rendered = build_prompt(case=case, channel=Channel.VOICE, paths=paths)
    assert rendered.variables == case.to_variables()


def test_render_reports_placeholders_used(paths: PromptPaths) -> None:
    rendered = build_prompt(case=_case(), channel=Channel.SMS, paths=paths)
    assert rendered.placeholders_used == frozenset(
        {"customer_first_name", "vehicle_make", "vehicle_model"}
    )


def test_render_raises_when_case_missing_placeholder(paths: PromptPaths) -> None:
    case = FakeCase(variables={"customer_first_name": "Sarah"})
    with pytest.raises(MissingPlaceholderError) as exc:
        build_prompt(case=case, channel=Channel.VOICE, paths=paths)
    assert "vehicle_make" in exc.value.missing
    assert "vehicle_model" in exc.value.missing


def test_voice_md_can_be_empty(tmp_path: Path) -> None:
    """The voice channel md is allowed to be empty (the existing system prompt
    already covers voice). Composer must not blow up on empty channel files."""
    system = tmp_path / "system.md"
    voice = tmp_path / "voice.md"
    sms = tmp_path / "sms.md"
    system.write_text("Hi {{name}}.", encoding="utf-8")
    voice.write_text("", encoding="utf-8")
    sms.write_text("unused", encoding="utf-8")
    paths = PromptPaths(system=system, voice=voice, sms=sms)

    rendered = build_prompt(
        case=FakeCase(variables={"name": "Sarah"}),
        channel=Channel.VOICE,
        paths=paths,
    )
    assert rendered.text == "Hi Sarah.\n\n"


def test_channel_for_path_routes_correctly(paths: PromptPaths) -> None:
    assert paths.for_channel(Channel.VOICE) == paths.voice
    assert paths.for_channel(Channel.SMS) == paths.sms
