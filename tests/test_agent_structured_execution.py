import base64
import json
from types import SimpleNamespace

import pytest

from app.models.agent_config import AgentConfig
from app.services.agent import AgentExecutionRequest, SubprocessAgentExecutionController


def _execution(workspace_path: str, structured_output_file: str | None):
    return SimpleNamespace(
        agent_name="planner",
        workspace_path=workspace_path,
        repository_path=f"{workspace_path}/repository",
        output_file="plan.md",
        structured_output_file=structured_output_file,
    )


def test_handle_success_output_extracts_structured_outputs(tmp_path):
    controller = SubprocessAgentExecutionController()
    result_path = tmp_path / "planner-result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "success",
                "message": "done",
                "neededClarifications": [],
                "outputs": [
                    {"name": "plan.md", "content": "plan body", "contentType": "text"},
                    {
                        "name": "artifacts/blob.bin",
                        "content": base64.b64encode(b"abc").decode("ascii"),
                        "contentType": "binary",
                    },
                ],
            }
        )
    )

    status, message, clarifications, files = controller._handle_success_output(
        _execution(str(tmp_path), "planner-result.json"),
        "",
    )

    assert status == "success"
    assert message == "done"
    assert clarifications == []
    assert files == ["plan.md", "artifacts/blob.bin"]
    assert (tmp_path / "plan.md").read_text() == "plan body"
    assert (tmp_path / "artifacts" / "blob.bin").read_bytes() == b"abc"


def test_handle_success_output_returns_blocked_payload(tmp_path):
    controller = SubprocessAgentExecutionController()
    result_path = tmp_path / "planner-result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "blocked",
                "message": "Need additional input",
                "neededClarifications": ["missing acceptance criteria"],
                "outputs": [],
            }
        )
    )

    status, message, clarifications, files = controller._handle_success_output(
        _execution(str(tmp_path), "planner-result.json"),
        "",
    )

    assert status == "blocked"
    assert message == "Need additional input"
    assert clarifications == ["missing acceptance criteria"]
    assert files == []


def test_handle_success_output_rejects_invalid_structured_status(tmp_path):
    controller = SubprocessAgentExecutionController()
    result_path = tmp_path / "planner-result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "unknown",
                "message": "bad",
                "neededClarifications": [],
                "outputs": [],
            }
        )
    )

    with pytest.raises(ValueError):
        controller._handle_success_output(_execution(str(tmp_path), "planner-result.json"), "")


def test_handle_success_output_non_structured_reports_output_file(tmp_path):
    controller = SubprocessAgentExecutionController()

    status, message, clarifications, files = controller._handle_success_output(
        _execution(str(tmp_path), None),
        "generated plan",
    )

    assert status == "success"
    assert message == ""
    assert clarifications == []
    assert files == ["plan.md"]
    assert (tmp_path / "plan.md").read_text() == "generated plan"


def test_handle_success_output_writes_nested_non_structured_output(tmp_path):
    controller = SubprocessAgentExecutionController()
    execution = _execution(str(tmp_path), None)
    execution.output_file = "artifacts/report.md"

    controller._handle_success_output(execution, "generated report")

    assert (tmp_path / "artifacts" / "report.md").read_text() == "generated report"


def test_handle_success_output_rejects_non_structured_path_escape(tmp_path):
    controller = SubprocessAgentExecutionController()
    execution = _execution(str(tmp_path), None)
    execution.output_file = "../report.md"

    with pytest.raises(ValueError, match="escapes workspace root"):
        controller._handle_success_output(execution, "generated report")


def test_load_stdin_rejects_path_escape(tmp_path):
    controller = SubprocessAgentExecutionController()

    with pytest.raises(ValueError, match="escapes workspace root"):
        controller._load_stdin_content(str(tmp_path), "../issue.json")


def test_load_stdin_fails_when_configured_file_is_missing(tmp_path):
    controller = SubprocessAgentExecutionController()

    with pytest.raises(FileNotFoundError, match="Agent stdin file not found"):
        controller._load_stdin_content(str(tmp_path), "missing.json")


def test_load_stdin_propagates_read_failure(monkeypatch, tmp_path):
    controller = SubprocessAgentExecutionController()
    stdin_path = tmp_path / "issue.json"
    stdin_path.write_text("issue body")

    def fail_open(*_args, **_kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr("builtins.open", fail_open)

    with pytest.raises(PermissionError, match="permission denied"):
        controller._load_stdin_content(str(tmp_path), "issue.json")


def test_load_stdin_allows_explicitly_empty_configuration(tmp_path):
    controller = SubprocessAgentExecutionController()

    assert controller._load_stdin_content(str(tmp_path), "") == ""


def test_start_execution_does_not_spawn_when_stdin_is_missing(monkeypatch, tmp_path):
    workspace_path = tmp_path / "ISSUE-MISSING"
    repository_path = workspace_path / "repository"
    repository_path.mkdir(parents=True)
    popen_calls = []
    monkeypatch.setattr(
        "app.services.agent.subprocess.Popen",
        lambda *args, **kwargs: popen_calls.append((args, kwargs)),
    )
    controller = SubprocessAgentExecutionController()
    config = AgentConfig(
        command="fake-agent",
        args=[],
        stdin="missing.json",
        sandbox="workspace-write",
        env=[],
    )

    with pytest.raises(FileNotFoundError, match="missing.json"):
        controller.start_execution(
            AgentExecutionRequest(
                agent_name="planner",
                issue={"id": "1"},
                agent_config=config,
                workspace_path=str(workspace_path),
                repository_path=str(repository_path),
            )
        )

    assert popen_calls == []


def test_start_execution_runs_in_repository_and_keeps_artifacts_in_workspace(monkeypatch, tmp_path):
    workspace_path = tmp_path / "ISSUE-1"
    repository_path = workspace_path / "repository"
    repository_path.mkdir(parents=True)
    (workspace_path / "issue.json").write_text("issue body")
    popen_calls = []

    class CapturingStdin:
        def __init__(self):
            self.value = ""

        def write(self, content):
            self.value += content

        def close(self):
            pass

    class FakeProcess:
        def __init__(self):
            self.stdin = CapturingStdin()

    def fake_popen(command, **kwargs):
        process = FakeProcess()
        popen_calls.append((command, kwargs, process))
        return process

    monkeypatch.setattr("app.services.agent.subprocess.Popen", fake_popen)
    controller = SubprocessAgentExecutionController()
    config = AgentConfig(
        command="fake-agent",
        args=["--result", "{structured}"],
        stdin="issue.json",
        structured="result.json",
        sandbox="workspace-write",
        env=[],
    )

    execution = controller.start_execution(
        AgentExecutionRequest(
            agent_name="planner",
            issue={"id": "1"},
            agent_config=config,
            workspace_path=str(workspace_path),
            repository_path=str(repository_path),
        )
    )

    command, kwargs, process = popen_calls[0]
    assert command == ["fake-agent", "--result", str(workspace_path / "result.json")]
    assert kwargs["cwd"] == str(repository_path)
    assert process.stdin.value == "issue body"
    assert execution.workspace_path == str(workspace_path)
    assert execution.repository_path == str(repository_path)
    assert (workspace_path / "log" / "planner.log").is_file()
    assert not (repository_path / "issue.json").exists()
    assert not (repository_path / "log").exists()
