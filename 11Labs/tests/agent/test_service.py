"""Service-layer tests for the agent module."""

from __future__ import annotations

from pathlib import Path

import pytest

from guidepoint.agent import (
    AgentConfigInvalidError,
    AgentId,
    AgentNotFoundError,
    ConfigPaths,
    ToolId,
    build_agent_manager,
    validate_config,
)
from tests.agent._fakes import InMemoryElevenLabsClient
from tests.agent._helpers import minimal_agent_config, sample_tool, seed


class TestPull:
    def test_writes_config_prompt_and_tools(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        agent_id = AgentId("agent_x")
        config = minimal_agent_config(tool_ids=(ToolId("ta"),))
        client = InMemoryElevenLabsClient(
            agents={agent_id: (config, "Hello {{customer_first_name}}.")},
            tools={ToolId("ta"): sample_tool(ToolId("ta"), "get_slots")},
        )
        manager = build_agent_manager(client=client, paths=paths)

        report = manager.pull(agent_id=agent_id)

        assert paths.agent_json.exists()
        assert paths.system_prompt_md.exists()
        assert paths.tool_path(tool_name="get_slots").exists()
        assert report.agent_id == agent_id
        assert len(report.tools) == 1

    def test_unknown_agent_raises(self, tmp_path: Path) -> None:
        client = InMemoryElevenLabsClient(agents={}, tools={})
        manager = build_agent_manager(client=client, paths=ConfigPaths.for_root(tmp_path))
        with pytest.raises(AgentNotFoundError):
            manager.pull(agent_id=AgentId("missing"))

    def test_pull_fetches_each_declared_tool_once(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        agent_id = AgentId("agent_x")
        config = minimal_agent_config(tool_ids=(ToolId("ta"), ToolId("tb")))
        client = InMemoryElevenLabsClient(
            agents={agent_id: (config, "")},
            tools={
                ToolId("ta"): sample_tool(ToolId("ta"), "tool_a"),
                ToolId("tb"): sample_tool(ToolId("tb"), "tool_b"),
            },
        )
        manager = build_agent_manager(client=client, paths=paths)
        manager.pull(agent_id=agent_id)
        assert client.tool_calls == (ToolId("ta"), ToolId("tb"))


class TestValidate:
    def test_passes_when_prompt_only_uses_case_keys(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        prompt = "Hi {{customer_first_name}} from {{dealer_name}}."
        seed(paths=paths, config=minimal_agent_config(), prompt=prompt, tools=())
        validate_config(paths=paths)

    def test_flags_unresolved_variable(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        prompt = "Hi {{not_a_real_variable}}."
        seed(paths=paths, config=minimal_agent_config(), prompt=prompt, tools=())
        with pytest.raises(AgentConfigInvalidError) as exc:
            validate_config(paths=paths)
        assert "not_a_real_variable" in str(exc.value)

    def test_flags_tool_count_mismatch(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        config = minimal_agent_config(tool_ids=(ToolId("ta"), ToolId("tb")))
        seed(
            paths=paths,
            config=config,
            prompt="{{customer_first_name}}",
            tools=(sample_tool(ToolId("ta"), "only_one"),),
        )
        with pytest.raises(AgentConfigInvalidError) as exc:
            validate_config(paths=paths)
        assert "tool" in str(exc.value).lower()

    def test_manager_validate_delegates_to_helper(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        seed(
            paths=paths,
            config=minimal_agent_config(),
            prompt="{{customer_first_name}}",
            tools=(),
        )
        client = InMemoryElevenLabsClient(agents={}, tools={})
        manager = build_agent_manager(client=client, paths=paths)
        manager.validate()
