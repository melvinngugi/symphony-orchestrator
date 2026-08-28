import pytest
from pydantic import ValidationError

from app.core.config import (
    WorkflowConfigLoadError,
    load_agents_config,
    load_config,
)
from app.models.agent_config import AgentConfig


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


def test_load_config_accepts_alternate_workflow_path(tmp_path):
    workflow_path = tmp_path / "custom-workflow.md"
    workflow_path.write_text("---\nphases:\n  custom: {}\n---\n# Custom workflow\n")

    assert load_config(str(workflow_path)) == {"phases": {"custom": {}}}


def test_load_config_rejects_missing_workflow(tmp_path):
    workflow_path = tmp_path / "missing-workflow.md"

    with pytest.raises(WorkflowConfigLoadError, match=str(workflow_path)):
        load_config(str(workflow_path))


def test_load_config_rejects_unreadable_workflow(monkeypatch, tmp_path):
    workflow_path = tmp_path / "unreadable-workflow.md"
    workflow_path.write_text("---\nphases: {}\n---\n")

    def deny_read(path, mode):
        assert path == str(workflow_path)
        assert mode == "r"
        raise PermissionError("permission denied")

    monkeypatch.setattr("builtins.open", deny_read)

    with pytest.raises(WorkflowConfigLoadError, match="permission denied"):
        load_config(str(workflow_path))


def test_load_config_rejects_malformed_yaml(tmp_path):
    workflow_path = tmp_path / "malformed-workflow.md"
    workflow_path.write_text("---\nphases: [\n---\n")

    with pytest.raises(WorkflowConfigLoadError, match="invalid YAML"):
        load_config(str(workflow_path))


def test_load_config_rejects_empty_front_matter(tmp_path):
    workflow_path = tmp_path / "empty-workflow.md"
    workflow_path.write_text("---\n---\n# Empty workflow\n")

    with pytest.raises(WorkflowConfigLoadError, match="must not be empty"):
        load_config(str(workflow_path))


def test_reviewer_routes_required_changes_to_clarification_needed():
    registry = load_agents_config("agents.yaml")
    workflow = load_config("WORKFLOW.md")
    reviewer_prompt = " ".join(registry.agents["reviewer"].args)

    assert "status success only when there are no findings" in reviewer_prompt
    assert "return status blocked" in reviewer_prompt
    assert "summarize every required change clearly in neededClarifications" in reviewer_prompt
    assert "pull-request comments rather than review.json" in reviewer_prompt
    assert workflow["phases"]["review"]["transitions"]["success"] == {
        "next": "Done",
        "do": [{"action": "bitbucket:publish-review-comment"}],
    }
    assert workflow["phases"]["review"]["transitions"]["blocked"] == {
        "next": "Clarification Needed",
        "do": [{"action": "bitbucket:publish-review-comment"}],
    }
    assert registry.agents["reviewer"].required_outputs == {}
    assert registry.agents["implementer"].stdin == "implementation-context.json"
    assert registry.agents["implementer"].refresh_stdin is True


def test_agent_config_rejects_unknown_required_output_status():
    with pytest.raises(ValidationError, match="success.*blocked"):
        AgentConfig(
            command="codex",
            stdin="issue.json",
            required_outputs={"needs_work": ["review.json"]},
        )
