"""Tests for the SDK adapter (``_client``) using duck-typed stubs.

We do not import or instantiate the real ``ElevenLabs`` SDK here. The
adapter's job is to navigate optional/None fields on the SDK's Pydantic
response models, so the tests feed it lightweight stub objects with the
same attribute shape and confirm the conversion is correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, final

import pytest
import structlog

from guidepoint.agent import AgentId, ToolId
from guidepoint.agent._client import build_elevenlabs_client
from guidepoint.agent._models import AgentNotFoundError

# ---------------------------------------------------------------------------
# Stubs that mimic the SDK's nested-attribute shape
# ---------------------------------------------------------------------------


@final
@dataclass
class _Prompt:
    prompt: str | None = ""
    llm: str | None = None
    temperature: float | None = None
    tool_ids: list[str] | None = None


@final
@dataclass
class _Dyn:
    dynamic_variable_placeholders: dict[str, object] | None = None


@final
@dataclass
class _Agent:
    first_message: str | None = None
    language: str | None = None
    prompt: _Prompt | None = None
    dynamic_variables: _Dyn | None = None


@final
@dataclass
class _Tts:
    voice_id: str | None = None
    model_id: str | None = None


@final
@dataclass
class _Conv:
    agent: _Agent | None = None
    tts: _Tts | None = None


@final
@dataclass
class _AgentResp:
    name: str | None = None
    conversation_config: _Conv | None = None


@final
@dataclass
class _AgentsClient:
    response: _AgentResp | None = None
    error: Exception | None = None

    def get(self, *, agent_id: str) -> _AgentResp:  # noqa: ARG002
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


@final
@dataclass
class _Schema:
    type: str = "string"
    description: str = ""


@final
@dataclass
class _BodySchema:
    properties: dict[str, _Schema] | None = None
    required: list[str] | None = None


@final
@dataclass
class _ApiSchema:
    url: str = ""
    method: str | None = None
    request_body_schema: _BodySchema | None = None


@final
@dataclass
class _Cond:
    name: str = ""
    operator: str = ""
    value: str = ""


@final
@dataclass
class _Mock:
    mock_result: str = ""
    parameter_conditions: list[_Cond] | None = None


@final
@dataclass
class _ToolCfg:
    type: str = "webhook"
    name: str = ""
    description: str = ""
    api_schema: _ApiSchema | None = None


@final
@dataclass
class _ToolResp:
    tool_config: _ToolCfg
    response_mocks: list[_Mock] | None = None


@final
@dataclass
class _ToolsClient:
    response: _ToolResp

    def get(self, *, tool_id: str) -> _ToolResp:  # noqa: ARG002
        return self.response


@final
@dataclass
class _ConvAi:
    agents: _AgentsClient
    tools: _ToolsClient


@final
@dataclass
class _Sdk:
    conversational_ai: _ConvAi


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFetchAgentConfig:
    def test_full_mapping(self) -> None:
        sdk = _build_sdk(
            agent=_AgentResp(
                name="Kate",
                conversation_config=_Conv(
                    agent=_Agent(
                        first_message="Hi.",
                        language="en",
                        prompt=_Prompt(
                            prompt="Hello {{customer_first_name}}",
                            llm="gpt-4o",
                            temperature=0.6,
                            tool_ids=["t1", "t2"],
                        ),
                        dynamic_variables=_Dyn(
                            dynamic_variable_placeholders={"dealer_name": "Village Jeep"},
                        ),
                    ),
                    tts=_Tts(voice_id="v1", model_id="m1"),
                ),
            ),
        )
        client = build_elevenlabs_client(sdk=_as_sdk(sdk))
        config, prompt = client.fetch_agent_config(agent_id=AgentId("agent_x"))

        assert prompt == "Hello {{customer_first_name}}"
        assert config.agent_id == AgentId("agent_x")
        assert config.name == "Kate"
        assert config.llm == "gpt-4o"
        assert config.temperature == 0.6
        assert config.first_message == "Hi."
        assert config.voice_id == "v1"
        assert config.tts_model_id == "m1"
        assert config.tool_ids == (ToolId("t1"), ToolId("t2"))

    def test_defaults_applied_when_sdk_returns_none(self) -> None:
        sdk = _build_sdk(agent=_AgentResp(name=None, conversation_config=None))
        client = build_elevenlabs_client(sdk=_as_sdk(sdk))
        config, prompt = client.fetch_agent_config(agent_id=AgentId("agent_x"))

        assert prompt == ""
        assert config.name == "Unnamed agent"
        assert config.language == "en"
        assert config.tool_ids == ()

    def test_emits_started_and_completed_log_events(self) -> None:
        sdk = _build_sdk(agent=_AgentResp(name="Kate"))
        client = build_elevenlabs_client(sdk=_as_sdk(sdk))
        with structlog.testing.capture_logs() as events:
            client.fetch_agent_config(agent_id=AgentId("agent_x"))

        names = [e["event"] for e in events]
        assert "agent.elevenlabs.request.started" in names
        assert "agent.elevenlabs.request.completed" in names
        completed = next(e for e in events if e["event"].endswith(".completed"))
        assert completed["agent_id"] == "agent_x"
        assert completed["elevenlabs_method"] == "agents.get"
        assert isinstance(completed["latency_ms"], float)

    def test_sdk_exception_surfaced_as_agent_not_found(self) -> None:
        sdk = _Sdk(
            conversational_ai=_ConvAi(
                agents=_AgentsClient(error=RuntimeError("boom")),
                tools=_ToolsClient(response=_ToolResp(tool_config=_ToolCfg())),
            )
        )
        client = build_elevenlabs_client(sdk=_as_sdk(sdk))
        with pytest.raises(AgentNotFoundError):
            client.fetch_agent_config(agent_id=AgentId("agent_x"))


class TestFetchTool:
    def test_webhook_with_params_and_mocks(self) -> None:
        sdk = _build_sdk(
            tool=_ToolResp(
                tool_config=_ToolCfg(
                    type="webhook",
                    name="get_slots",
                    description="d",
                    api_schema=_ApiSchema(
                        url="https://example.com/s",
                        method="get",
                        request_body_schema=_BodySchema(
                            properties={"dealer_id": _Schema(type="string", description="d")},
                            required=["dealer_id"],
                        ),
                    ),
                ),
                response_mocks=[
                    _Mock(
                        mock_result="[]",
                        parameter_conditions=[_Cond(name="x", operator="==", value="y")],
                    )
                ],
            ),
        )
        client = build_elevenlabs_client(sdk=_as_sdk(sdk))
        tool = client.fetch_tool(tool_id=ToolId("ta"))

        assert tool.name == "get_slots"
        assert tool.method == "GET"
        assert tool.url == "https://example.com/s"
        assert tool.parameters[0].required is True
        assert tool.mocks[0].body == "[]"
        assert tool.mocks[0].condition == "x == y"

    def test_non_webhook_tool_keeps_url_blank(self) -> None:
        sdk = _build_sdk(
            tool=_ToolResp(
                tool_config=_ToolCfg(type="client", name="hangup", description="d"),
            ),
        )
        client = build_elevenlabs_client(sdk=_as_sdk(sdk))
        tool = client.fetch_tool(tool_id=ToolId("ta"))
        assert tool.url == ""
        assert tool.parameters == ()

    def test_emits_failed_event_when_sdk_raises(self) -> None:
        class _Boom:
            def get(self, *, tool_id: str) -> object:  # noqa: ARG002
                raise RuntimeError("boom")

        sdk = _Sdk(
            conversational_ai=_ConvAi(
                agents=_AgentsClient(response=_AgentResp()),
                tools=_Boom(),  # type: ignore[arg-type]
            )
        )
        client = build_elevenlabs_client(sdk=_as_sdk(sdk))
        with structlog.testing.capture_logs() as events, pytest.raises(RuntimeError):
            client.fetch_tool(tool_id=ToolId("ta"))
        assert any(e["event"] == "agent.elevenlabs.request.failed" for e in events)

    def test_unknown_method_falls_back_to_post(self) -> None:
        sdk = _build_sdk(
            tool=_ToolResp(
                tool_config=_ToolCfg(
                    type="webhook",
                    name="x",
                    description="d",
                    api_schema=_ApiSchema(url="u", method="trace"),
                ),
            ),
        )
        client = build_elevenlabs_client(sdk=_as_sdk(sdk))
        tool = client.fetch_tool(tool_id=ToolId("ta"))
        assert tool.method == "POST"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_sdk(
    *,
    agent: _AgentResp | None = None,
    tool: _ToolResp | None = None,
) -> _Sdk:
    return _Sdk(
        conversational_ai=_ConvAi(
            agents=_AgentsClient(response=agent),
            tools=_ToolsClient(response=tool or _ToolResp(tool_config=_ToolCfg())),
        ),
    )


def _as_sdk(sdk: _Sdk) -> Any:
    """Return ``sdk`` typed as ``Any`` so the SDK-typed adapter accepts it."""
    return sdk
