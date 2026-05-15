"""Round-trip tests for the agent file I/O layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from guidepoint.agent import (
    AgentConfig,
    AgentId,
    ConfigPaths,
    ToolDef,
    ToolId,
    ToolMock,
    ToolParameter,
)
from guidepoint.agent._io import (
    extract_variable_names,
    read_agent_config,
    read_all_tools,
    read_system_prompt,
    write_agent_config,
    write_system_prompt,
    write_tool,
)


class TestConfigPaths:
    def test_layout_under_root(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        assert paths.config_dir == (tmp_path / "config").resolve()
        assert paths.tools_dir == (tmp_path / "config" / "tools").resolve()
        assert paths.agent_json == (tmp_path / "config" / "agent.json").resolve()
        assert paths.system_prompt_md == (tmp_path / "config" / "system-prompt.md").resolve()

    def test_tool_path_sanitizes_name(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        assert paths.tool_path(tool_name="get slots!").name == "get_slots.json"
        assert paths.tool_path(tool_name="").name == "unnamed_tool.json"


class TestAgentConfigRoundTrip:
    def test_write_then_read_returns_equal_config(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        config = _sample_config()
        write_agent_config(paths=paths, config=config)
        loaded = read_agent_config(paths=paths)
        assert loaded == config

    def test_writes_pretty_json(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        write_agent_config(paths=paths, config=_sample_config())
        body = paths.agent_json.read_text(encoding="utf-8")
        assert body.startswith("{\n")
        assert body.endswith("}\n")
        assert '  "agent_id"' in body  # 2-space indent

    def test_unknown_field_in_json_is_rejected(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        write_agent_config(paths=paths, config=_sample_config())
        loaded = json.loads(paths.agent_json.read_text(encoding="utf-8"))
        loaded["rogue_field"] = True
        paths.agent_json.write_text(json.dumps(loaded), encoding="utf-8")
        with pytest.raises(Exception, match="Extra inputs"):
            read_agent_config(paths=paths)


class TestSystemPromptRoundTrip:
    def test_appends_trailing_newline(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        write_system_prompt(paths=paths, body="hello {{customer_first_name}}")
        text = read_system_prompt(paths=paths)
        assert text.endswith("\n")
        assert "{{customer_first_name}}" in text

    def test_extract_variable_names_finds_double_braces(self) -> None:
        names = extract_variable_names(
            prompt_body="Hi {{customer_first_name}}, your {{vehicle_year}} {{vehicle_make}}."
        )
        assert names == frozenset({"customer_first_name", "vehicle_year", "vehicle_make"})


class TestToolRoundTrip:
    def test_write_and_read_all(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        tool_a = _sample_tool(name="get_available_slots", tool_id=ToolId("ta"))
        tool_b = _sample_tool(name="book_appointment", tool_id=ToolId("tb"))
        write_tool(paths=paths, tool=tool_a)
        write_tool(paths=paths, tool=tool_b)
        loaded = read_all_tools(paths=paths)
        assert {t.name for t in loaded} == {"get_available_slots", "book_appointment"}

    def test_read_all_tools_empty_when_no_dir(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        assert read_all_tools(paths=paths) == ()


def _sample_config() -> AgentConfig:
    return AgentConfig(
        agent_id=AgentId("agent_x"),
        name="Kate",
        language="en",
        llm="gpt-4o-mini",
        temperature=0.5,
        first_message="Hi.",
        voice_id="v1",
        tts_model_id="m1",
        system_prompt_path="system-prompt.md",
        tool_ids=(ToolId("tool_a"), ToolId("tool_b")),
    )


def _sample_tool(*, name: str, tool_id: ToolId) -> ToolDef:
    return ToolDef(
        tool_id=tool_id,
        name=name,
        description="d",
        method="POST",
        url="https://example.com/" + name,
        parameters=(
            ToolParameter(name="dealer_id", type="string", description="d", required=True),
        ),
        mocks=(ToolMock(name="ok", status_code=200, body='{"ok": true}'),),
    )
