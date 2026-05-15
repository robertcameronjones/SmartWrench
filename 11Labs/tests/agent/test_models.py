"""Validation tests for the agent boundary models.

The Case / Customer / Dealer / Vehicle / Location / OfferedSlot /
ServiceEvent models moved to ``guidepoint.case`` and
``guidepoint.master_data`` per ADR 0006. Their tests live under
``tests/case`` and ``tests/master_data`` respectively.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from guidepoint.agent import (
    AgentConfig,
    AgentConfigInvalidError,
    AgentId,
    ToolDef,
    ToolId,
    ToolMock,
    ToolParameter,
)


class TestAgentConfig:
    def test_minimal_round_trip(self) -> None:
        config = AgentConfig(
            agent_id=AgentId("agent_x"),
            name="Kate",
            language="en",
            llm="gpt-4o-mini",
            temperature=0.5,
            first_message="Hi.",
            voice_id="v1",
            tts_model_id="m1",
            system_prompt_path="system-prompt.md",
            tool_ids=(ToolId("tool_a"),),
        )
        round_tripped = AgentConfig.model_validate(config.model_dump())
        assert round_tripped == config

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfig.model_validate(
                {
                    "agent_id": "a",
                    "name": "k",
                    "language": "en",
                    "llm": "x",
                    "temperature": 0.5,
                    "first_message": "",
                    "voice_id": "v",
                    "tts_model_id": "m",
                    "system_prompt_path": "p",
                    "tool_ids": [],
                    "extra_field": True,
                }
            )

    def test_temperature_bounds_enforced(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfig(
                agent_id=AgentId("a"),
                name="k",
                language="en",
                llm="x",
                temperature=2.5,
                first_message="",
                voice_id="v",
                tts_model_id="m",
                system_prompt_path="p",
            )

    def test_is_frozen(self) -> None:
        config = _make_minimal_config()
        with pytest.raises(ValidationError):
            config.name = "Other"  # type: ignore[misc]


class TestToolDef:
    def test_defaults(self) -> None:
        tool = ToolDef(tool_id=ToolId("t"), name="get_slots", description="d")
        assert tool.method == "POST"
        assert tool.url == ""
        assert tool.parameters == ()
        assert tool.mocks == ()

    def test_param_and_mock_round_trip(self) -> None:
        tool = ToolDef(
            tool_id=ToolId("t"),
            name="get_slots",
            description="d",
            url="https://example.com/slots",
            method="GET",
            parameters=(
                ToolParameter(name="dealer_id", type="string", description="d", required=True),
            ),
            mocks=(ToolMock(name="m1", status_code=200, body="[]"),),
        )
        clone = ToolDef.model_validate(tool.model_dump())
        assert clone == tool


class TestAgentConfigInvalidError:
    def test_message_lists_each_issue(self) -> None:
        err = AgentConfigInvalidError(("a", "b"))
        assert "a" in str(err)
        assert "b" in str(err)
        assert err.issues == ("a", "b")


def _make_minimal_config() -> AgentConfig:
    return AgentConfig(
        agent_id=AgentId("a"),
        name="k",
        language="en",
        llm="x",
        temperature=0.5,
        first_message="",
        voice_id="v",
        tts_model_id="m",
        system_prompt_path="p",
    )
