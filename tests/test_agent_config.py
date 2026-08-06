import pytest

from app.core.config import load_agents_config


def test_project_structured_agents_define_result_destination():
    registry = load_agents_config("agents.yaml")

    assert registry.agents
    for agent in registry.agents.values():
        if agent.structured:
            assert any("{structured}" in argument for argument in agent.args)


def test_load_agents_config_rejects_structured_agent_without_placeholder(tmp_path):
    config_path = tmp_path / "agents.yaml"
    config_path.write_text(
        """
agents:
  broken:
    command: codex
    args: [exec]
    stdin: issue.json
    structured: result.json
"""
    )

    with pytest.raises(ValueError, match="Structured agent 'broken'"):
        load_agents_config(str(config_path))
