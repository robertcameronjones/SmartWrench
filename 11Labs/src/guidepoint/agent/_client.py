"""ElevenLabs SDK adapter implementing ``ElevenLabsClient``.

Translates the SDK's ``GetAgentResponseModel`` / ``ToolResponseModel`` into
our internal ``AgentConfig`` / ``ToolDef`` boundary models. The service
layer never touches the SDK directly — this is the only place that does.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast, final

import structlog

from guidepoint.agent._models import (
    AgentConfig,
    AgentId,
    AgentNotFoundError,
    ElevenLabsClient,
    ToolDef,
    ToolHttpMethod,
    ToolId,
    ToolMock,
    ToolParameter,
)

if TYPE_CHECKING:
    from elevenlabs.client import ElevenLabs

_log = structlog.get_logger(__name__)

# Default filename for the markdown body referenced from agent.toml. Stored
# relative to the config directory; the loader joins it with ConfigPaths.
_SYSTEM_PROMPT_FILENAME = "system-prompt.md"

# Default values used when ElevenLabs returns ``None`` for a field we treat
# as required. Centralized here so we have one place to grep.
_DEFAULT_LANGUAGE = "en"
_DEFAULT_LLM = "gpt-4o-mini"
_DEFAULT_TEMPERATURE = 0.5
_DEFAULT_TTS_MODEL = "eleven_turbo_v2_5"
_DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"


@final
@dataclass(frozen=True, slots=True)
class _ElevenLabsAdapter:
    """Concrete ``ElevenLabsClient`` backed by the official Python SDK."""

    _sdk: ElevenLabs

    def fetch_agent_config(self, *, agent_id: AgentId) -> tuple[AgentConfig, str]:
        bound = _log.bind(elevenlabs_method="agents.get", agent_id=agent_id)
        bound.info("agent.elevenlabs.request.started")
        started = time.monotonic()
        try:
            agent = self._sdk.conversational_ai.agents.get(agent_id=agent_id)
        except Exception as exc:
            bound.error(
                "agent.elevenlabs.request.failed",
                latency_ms=_elapsed_ms(started),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise AgentNotFoundError(agent_id) from exc

        prompt_body, config = _agent_to_config(agent_id=agent_id, agent=agent)
        bound.info(
            "agent.elevenlabs.request.completed",
            latency_ms=_elapsed_ms(started),
            agent_name=config.name,
            llm=config.llm,
            voice_id=config.voice_id,
            tool_count=len(config.tool_ids),
            prompt_chars=len(prompt_body),
        )
        return config, prompt_body

    def fetch_tool(self, *, tool_id: ToolId) -> ToolDef:
        bound = _log.bind(elevenlabs_method="tools.get", tool_id=tool_id)
        bound.info("agent.elevenlabs.request.started")
        started = time.monotonic()
        try:
            tool = self._sdk.conversational_ai.tools.get(tool_id=tool_id)
        except Exception as exc:
            bound.error(
                "agent.elevenlabs.request.failed",
                latency_ms=_elapsed_ms(started),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        result = _tool_to_def(tool_id=tool_id, tool=tool)
        bound.info(
            "agent.elevenlabs.request.completed",
            latency_ms=_elapsed_ms(started),
            tool_name=result.name,
            method=result.method,
            url=result.url,
            parameter_count=len(result.parameters),
            mock_count=len(result.mocks),
        )
        return result


def build_elevenlabs_client(*, sdk: ElevenLabs) -> ElevenLabsClient:
    """Construct the production ``ElevenLabsClient`` adapter.

    The SDK client is injected so tests can pass a stub. There is no
    module-level singleton — every consumer builds its own.
    """
    return _ElevenLabsAdapter(_sdk=sdk)


# ---------------------------------------------------------------------------
# Conversion helpers (private)
# ---------------------------------------------------------------------------


def _agent_to_config(*, agent_id: AgentId, agent: Any) -> tuple[str, AgentConfig]:
    """Map ``GetAgentResponseModel`` → ``(prompt_body, AgentConfig)``."""
    conv = _attr(agent, "conversation_config")
    agent_block = _attr(conv, "agent")
    prompt_block = _attr(agent_block, "prompt")
    tts_block = _attr(conv, "tts")

    prompt_body = _coerce_str(_attr(prompt_block, "prompt"), default="")
    llm = _coerce_str(_attr(prompt_block, "llm"), default=_DEFAULT_LLM)
    temperature = _coerce_float(_attr(prompt_block, "temperature"), default=_DEFAULT_TEMPERATURE)
    tool_ids_raw = _attr(prompt_block, "tool_ids") or ()
    tool_ids = tuple(ToolId(str(t)) for t in tool_ids_raw)

    config = AgentConfig(
        agent_id=agent_id,
        name=_coerce_str(_attr(agent, "name"), default="Unnamed agent"),
        language=_coerce_str(_attr(agent_block, "language"), default=_DEFAULT_LANGUAGE),
        llm=llm,
        temperature=temperature,
        first_message=_coerce_str(_attr(agent_block, "first_message"), default=""),
        voice_id=_coerce_str(_attr(tts_block, "voice_id"), default=_DEFAULT_VOICE_ID),
        tts_model_id=_coerce_str(_attr(tts_block, "model_id"), default=_DEFAULT_TTS_MODEL),
        system_prompt_path=_SYSTEM_PROMPT_FILENAME,
        tool_ids=tool_ids,
    )
    return prompt_body, config


def _tool_to_def(*, tool_id: ToolId, tool: Any) -> ToolDef:
    """Map ``ToolResponseModel`` → ``ToolDef``.

    Webhook tools are fully captured. For client/system/MCP tools we still
    record name + description so the local config reflects the dashboard,
    but the URL/method/parameters fields stay empty.
    """
    cfg = _attr(tool, "tool_config")
    cfg_type = _coerce_str(_attr(cfg, "type"), default="webhook")
    name = _coerce_str(_attr(cfg, "name"), default="unnamed_tool")
    description = _coerce_str(_attr(cfg, "description"), default="")

    method: ToolHttpMethod = "POST"
    url = ""
    parameters: tuple[ToolParameter, ...] = ()

    if cfg_type == "webhook":
        api_schema = _attr(cfg, "api_schema")
        url = _coerce_str(_attr(api_schema, "url"), default="")
        method = _coerce_method(_attr(api_schema, "method"))
        parameters = _extract_webhook_params(api_schema=api_schema)

    return ToolDef(
        tool_id=tool_id,
        name=name,
        description=description,
        method=method,
        url=url,
        parameters=parameters,
        mocks=_extract_mocks(tool=tool),
    )


def _extract_webhook_params(*, api_schema: Any) -> tuple[ToolParameter, ...]:
    """Flatten request_body_schema.properties into our parameter list."""
    body_schema = _attr(api_schema, "request_body_schema")
    properties_raw = _attr(body_schema, "properties")
    if not isinstance(properties_raw, dict):
        return ()
    properties = cast("dict[str, object]", properties_raw)
    required_raw = _attr(body_schema, "required") or ()
    required_set = frozenset(str(r) for r in required_raw)

    items: list[ToolParameter] = []
    for param_name, schema in sorted(properties.items()):
        items.append(
            ToolParameter(
                name=str(param_name),
                type=_coerce_str(_attr(schema, "type"), default="string"),
                description=_coerce_str(_attr(schema, "description"), default=""),
                required=str(param_name) in required_set,
            )
        )
    return tuple(items)


def _extract_mocks(*, tool: Any) -> tuple[ToolMock, ...]:
    """Map ``response_mocks`` to our ``ToolMock`` list."""
    raw = _attr(tool, "response_mocks") or ()
    items: list[ToolMock] = []
    for index, mock in enumerate(raw):
        items.append(
            ToolMock(
                name=f"mock_{index + 1}",
                status_code=200,
                body=_coerce_str(_attr(mock, "mock_result"), default=""),
                condition=_summarize_conditions(_attr(mock, "parameter_conditions")),
            )
        )
    return tuple(items)


def _summarize_conditions(conditions: Any) -> str:
    """Render parameter_conditions as a one-line human-readable summary."""
    if not conditions:
        return ""
    parts: list[str] = []
    for cond in conditions:
        name = _coerce_str(_attr(cond, "name"), default="?")
        op = _coerce_str(_attr(cond, "operator"), default="==")
        value = _coerce_str(_attr(cond, "value"), default="?")
        parts.append(f"{name} {op} {value}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Tiny safe-attribute helpers
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str) -> Any:
    """Return ``obj.name`` or ``None`` if obj is ``None`` / missing the attr."""
    if obj is None:
        return None
    return getattr(obj, name, None)


def _coerce_str(value: Any, *, default: str) -> str:
    if value is None:
        return default
    return str(value)


def _coerce_float(value: Any, *, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _elapsed_ms(started: float) -> float:
    """Return milliseconds elapsed since ``started`` (a ``time.monotonic`` value).

    ``monotonic`` rather than wall-clock so latency stays correct across
    NTP adjustments. Not affected by the architecture rule against
    ``datetime.now()`` — that rule is about wall-time business logic, not
    duration measurement.
    """
    return round((time.monotonic() - started) * 1000.0, 2)


def _coerce_method(value: Any) -> ToolHttpMethod:
    raw = _coerce_str(value, default="POST").upper()
    if raw in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return cast("ToolHttpMethod", raw)
    return "POST"
