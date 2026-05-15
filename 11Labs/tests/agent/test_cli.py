"""CLI dispatch tests for ``python -m guidepoint.agent``."""

from __future__ import annotations

from pathlib import Path

import pytest

from guidepoint.agent import (
    AgentConfig,
    AgentId,
    AgentManager,
    ConfigPaths,
    build_agent_manager,
)
from guidepoint.agent.__main__ import main
from guidepoint.agent._io import write_agent_config, write_system_prompt
from tests.agent._fakes import InMemoryElevenLabsClient


class TestPullCommand:
    def test_success_writes_files_and_returns_zero(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        agent_id = AgentId("agent_x")
        client = InMemoryElevenLabsClient(
            agents={agent_id: (_minimal_config(agent_id), "Hello {{customer_first_name}}.")},
            tools={},
        )

        def factory(paths: ConfigPaths) -> AgentManager:
            return build_agent_manager(client=client, paths=paths)

        rc = main(
            ("--project-root", str(tmp_path), "pull", "--agent", agent_id),
            manager_factory=factory,
        )

        assert rc == 0
        captured = capsys.readouterr()
        assert "Pulled agent" in captured.out
        assert (tmp_path / "config" / "agent.json").exists()
        assert (tmp_path / "config" / "system-prompt.md").exists()

    def test_missing_agent_id_returns_usage_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("ELEVENLABS_AGENT_ID", raising=False)
        # Suppress dotenv from re-injecting a value from the on-disk .env:
        monkeypatch.setattr("guidepoint.agent.__main__.load_dotenv", lambda: None)
        rc = main(("--project-root", str(tmp_path), "pull"))
        assert rc == 2
        assert "ELEVENLABS_AGENT_ID" in capsys.readouterr().err

    def test_missing_api_key_returns_usage_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        # Suppress dotenv from re-injecting a key from disk:
        monkeypatch.setattr("guidepoint.agent.__main__.load_dotenv", lambda: None)
        rc = main(("--project-root", str(tmp_path), "pull", "--agent", "agent_x"))
        assert rc == 2
        assert "ELEVENLABS_API_KEY" in capsys.readouterr().err


class TestValidateCommand:
    def test_passes_on_well_formed_config(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        write_agent_config(paths=paths, config=_minimal_config(AgentId("a")))
        write_system_prompt(paths=paths, body="Hi {{customer_first_name}}.")

        rc = main(("--project-root", str(tmp_path), "validate"))
        assert rc == 0

    def test_fails_on_unresolved_variable(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        write_agent_config(paths=paths, config=_minimal_config(AgentId("a")))
        write_system_prompt(paths=paths, body="Hi {{not_real}}.")

        rc = main(("--project-root", str(tmp_path), "validate"))
        assert rc == 3
        assert "not_real" in capsys.readouterr().err


def _minimal_config(agent_id: AgentId) -> AgentConfig:
    return AgentConfig(
        agent_id=agent_id,
        name="Kate",
        language="en",
        llm="gpt-4o-mini",
        temperature=0.5,
        first_message="Hi.",
        voice_id="v",
        tts_model_id="m",
        system_prompt_path="system-prompt.md",
        tool_ids=(),
    )
