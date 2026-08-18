import base64
import json
import sys
import time
from types import SimpleNamespace

import pytest

from app.models.agent_config import AgentConfig
from app.services.agent import (
    AgentExecutionRequest,
    FallbackAgentInputProvider,
    ImplementationContextInputProvider,
    SubprocessAgentExecutionController,
)


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


def test_handle_success_output_extracts_blocked_outputs(tmp_path):
    controller = SubprocessAgentExecutionController()
    result_path = tmp_path / "reviewer-result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "blocked",
                "message": "Changes required",
                "neededClarifications": ["Handle malformed URLs"],
                "outputs": [
                    {
                        "name": "review.json",
                        "content": '{"findings": ["malformed URL"]}',
                        "contentType": "text",
                    }
                ],
            }
        )
    )
    execution = _execution(str(tmp_path), "reviewer-result.json")
    execution.required_outputs = {"blocked": ["review.json"]}

    status, message, clarifications, files = controller._handle_success_output(
        execution,
        "",
    )

    assert status == "blocked"
    assert message == "Changes required"
    assert clarifications == ["Handle malformed URLs"]
    assert files == ["review.json"]
    assert json.loads((tmp_path / "review.json").read_text()) == {
        "findings": ["malformed URL"]
    }


def test_handle_success_output_requires_configured_status_outputs(tmp_path):
    controller = SubprocessAgentExecutionController()
    result_path = tmp_path / "reviewer-result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "blocked",
                "message": "Changes required",
                "neededClarifications": ["Handle malformed URLs"],
                "outputs": [],
            }
        )
    )
    execution = _execution(str(tmp_path), "reviewer-result.json")
    execution.required_outputs = {"blocked": ["review.json"]}

    with pytest.raises(ValueError, match="missing required output.*review.json"):
        controller._handle_success_output(execution, "")


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


def test_start_execution_restores_missing_stdin_from_input_provider(monkeypatch, tmp_path):
    workspace_path = tmp_path / "ISSUE-RESTORE"
    repository_path = workspace_path / "repository"
    repository_path.mkdir(parents=True)
    popen_calls = []

    class InputProvider:
        def __init__(self):
            self.calls = []

        def fetch_attachment(self, issue_identifier, filename):
            self.calls.append((issue_identifier, filename))
            return b"# Restored plan\n"

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
    provider = InputProvider()
    controller = SubprocessAgentExecutionController(input_provider=provider)

    controller.start_execution(
        AgentExecutionRequest(
            agent_name="implementer",
            issue={"id": "1", "identifier": "SHOP-1"},
            agent_config=AgentConfig(command="fake-agent", stdin="plan.md"),
            workspace_path=str(workspace_path),
            repository_path=str(repository_path),
        )
    )

    assert provider.calls == [("SHOP-1", "plan.md")]
    assert (workspace_path / "plan.md").read_bytes() == b"# Restored plan\n"
    assert popen_calls[0][2].stdin.value == "# Restored plan\n"


def test_start_execution_does_not_fetch_when_stdin_already_exists(monkeypatch, tmp_path):
    workspace_path = tmp_path / "ISSUE-LOCAL"
    repository_path = workspace_path / "repository"
    repository_path.mkdir(parents=True)
    (workspace_path / "plan.md").write_text("# Local plan\n")

    class InputProvider:
        def fetch_attachment(self, *_args):
            pytest.fail("Existing agent input must not be fetched again")

    class FakeProcess:
        def __init__(self):
            self.stdin = SimpleNamespace(write=lambda _content: None, close=lambda: None)

    monkeypatch.setattr(
        "app.services.agent.subprocess.Popen",
        lambda *_args, **_kwargs: FakeProcess(),
    )
    controller = SubprocessAgentExecutionController(input_provider=InputProvider())

    controller.start_execution(
        AgentExecutionRequest(
            agent_name="implementer",
            issue={"id": "1", "identifier": "SHOP-1"},
            agent_config=AgentConfig(command="fake-agent", stdin="plan.md"),
            workspace_path=str(workspace_path),
            repository_path=str(repository_path),
        )
    )


def test_start_execution_refreshes_configured_stdin_from_provider(monkeypatch, tmp_path):
    workspace_path = tmp_path / "ISSUE-REFRESH"
    repository_path = workspace_path / "repository"
    repository_path.mkdir(parents=True)
    stdin_path = workspace_path / "implementation-context.json"
    stdin_path.write_text('{"reviewFeedback":"old"}')

    class InputProvider:
        def fetch_attachment(self, issue_identifier, filename):
            assert (issue_identifier, filename) == (
                "SHOP-1",
                "implementation-context.json",
            )
            return b'{"reviewFeedback":"new"}'

    class CapturingStdin:
        def __init__(self):
            self.value = ""

        def write(self, content):
            self.value += content

        def close(self):
            pass

    process = SimpleNamespace(stdin=CapturingStdin())
    monkeypatch.setattr(
        "app.services.agent.subprocess.Popen",
        lambda *_args, **_kwargs: process,
    )
    controller = SubprocessAgentExecutionController(input_provider=InputProvider())

    controller.start_execution(
        AgentExecutionRequest(
            agent_name="implementer",
            issue={"id": "1", "identifier": "SHOP-1"},
            agent_config=AgentConfig(
                command="fake-agent",
                stdin="implementation-context.json",
                refresh_stdin=True,
            ),
            workspace_path=str(workspace_path),
            repository_path=str(repository_path),
        )
    )

    assert stdin_path.read_text() == '{"reviewFeedback":"new"}'
    assert process.stdin.value == '{"reviewFeedback":"new"}'


def test_fallback_input_provider_uses_first_available_result():
    calls = []

    class Provider:
        def __init__(self, name, result):
            self.name = name
            self.result = result

        def fetch_attachment(self, issue_identifier, filename):
            calls.append((self.name, issue_identifier, filename))
            return self.result

    provider = FallbackAgentInputProvider(
        (Provider("jira", None), Provider("bitbucket", b"pull request"))
    )

    assert provider.fetch_attachment("ISSUE-1", "pull-request.json") == b"pull request"
    assert calls == [
        ("jira", "ISSUE-1", "pull-request.json"),
        ("bitbucket", "ISSUE-1", "pull-request.json"),
    ]


def test_implementation_context_combines_plan_and_pull_request_comments():
    class PlanProvider:
        def fetch_attachment(self, issue_identifier, filename):
            assert (issue_identifier, filename) == ("SHOP-1", "plan.md")
            return b"# Build the application\n"

    class ReviewProvider:
        def fetch_attachment(self, issue_identifier, filename):
            assert (issue_identifier, filename) == (
                "SHOP-1",
                "pull-request-comments.json",
            )
            return json.dumps(
                {
                    "pullRequest": {"id": 4, "sourceCommit": "abc123"},
                    "activeComments": [
                        {"id": 8, "content": "Handle malformed URLs"}
                    ],
                }
            ).encode()

    provider = ImplementationContextInputProvider(PlanProvider(), ReviewProvider())

    content = provider.fetch_attachment("SHOP-1", "implementation-context.json")

    assert json.loads(content) == {
        "issue": "SHOP-1",
        "plan": "# Build the application\n",
        "reviewFeedback": {
            "pullRequest": {"id": 4, "sourceCommit": "abc123"},
            "activeComments": [{"id": 8, "content": "Handle malformed URLs"}],
        },
    }


def test_implementation_context_allows_initial_run_without_pull_request():
    class Provider:
        def __init__(self, content):
            self.content = content

        def fetch_attachment(self, *_args):
            return self.content

    provider = ImplementationContextInputProvider(
        Provider(b"# Initial plan\n"),
        Provider(None),
    )

    content = provider.fetch_attachment("SHOP-1", "implementation-context.json")

    assert json.loads(content)["reviewFeedback"] is None


def test_implementation_context_requires_plan():
    class Provider:
        def __init__(self, content):
            self.content = content

        def fetch_attachment(self, *_args):
            return self.content

    provider = ImplementationContextInputProvider(Provider(None), Provider(b"{}"))

    assert provider.fetch_attachment("SHOP-1", "implementation-context.json") is None


def test_implementation_context_rejects_malformed_review_data():
    class Provider:
        def __init__(self, content):
            self.content = content

        def fetch_attachment(self, *_args):
            return self.content

    provider = ImplementationContextInputProvider(
        Provider(b"# Plan\n"),
        Provider(b"not-json"),
    )

    with pytest.raises(ValueError, match="pull-request-comments.json"):
        provider.fetch_attachment("SHOP-1", "implementation-context.json")


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


def test_agent_stdout_is_drained_without_pipe_deadlock(tmp_path):
    workspace_path = tmp_path / "ISSUE-OUTPUT"
    repository_path = workspace_path / "repository"
    repository_path.mkdir(parents=True)
    (workspace_path / "issue.json").write_text("{}")
    output_size = 1_000_000
    controller = SubprocessAgentExecutionController(execution_timeout_seconds=5)

    execution = controller.start_execution(
        AgentExecutionRequest(
            agent_name="writer",
            issue={"id": "1"},
            agent_config=AgentConfig(
                command=sys.executable,
                args=["-c", f"print('x' * {output_size}, end='')"],
                stdin="issue.json",
                output_file="report.txt",
            ),
            workspace_path=str(workspace_path),
            repository_path=str(repository_path),
        )
    )

    result = None
    deadline = time.monotonic() + 5
    while result is None and time.monotonic() < deadline:
        result = controller.poll_execution(execution)
        time.sleep(0.01)

    assert result is not None
    assert result.exit_code == 0
    assert len(result.stdout) == output_size
    assert (workspace_path / "report.txt").stat().st_size == output_size


def test_agent_execution_timeout_terminates_process(tmp_path):
    workspace_path = tmp_path / "ISSUE-TIMEOUT"
    repository_path = workspace_path / "repository"
    repository_path.mkdir(parents=True)
    (workspace_path / "issue.json").write_text("{}")
    controller = SubprocessAgentExecutionController(
        execution_timeout_seconds=0.05,
        termination_grace_seconds=0.05,
    )

    execution = controller.start_execution(
        AgentExecutionRequest(
            agent_name="sleeper",
            issue={"id": "1"},
            agent_config=AgentConfig(
                command=sys.executable,
                args=["-c", "import time; time.sleep(30)"],
                stdin="issue.json",
            ),
            workspace_path=str(workspace_path),
            repository_path=str(repository_path),
        )
    )
    time.sleep(0.1)

    result = controller.poll_execution(execution)

    assert result is not None
    assert result.exit_code == 124
    assert result.status == "failed"
    assert "timed out after 0.05 seconds" in result.stderr
    assert execution.process.poll() is not None


def test_read_stderr_returns_tail_and_points_to_full_log(tmp_path):
    controller = SubprocessAgentExecutionController()
    controller._ERROR_LOG_MAX_CHARS = 20
    log_path = tmp_path / "log" / "implementer.log"
    log_path.parent.mkdir()
    log_path.write_text("command line\n" + "old output\n" + "final useful error!!")

    content = controller._read_stderr_content(str(tmp_path), "implementer")

    assert "Earlier agent output omitted" in content
    assert str(log_path) in content
    assert content.endswith("final useful error!!")
    assert "old output" not in content
