from types import SimpleNamespace

import pytest
import requests

from app.models.agent_config import AgentConfig
from app.services import jira as jira_module
from app.services.actions import ActionRegistry, PhaseResult
from app.services.agent import AgentExecutionResult
from app.services.jira import JiraClient


def _client(monkeypatch):
    monkeypatch.setattr(type(jira_module.settings), "validate_jira", lambda self: None)
    monkeypatch.setattr(jira_module.settings, "JIRA_USER_EMAIL", "bot@example.com")
    monkeypatch.setattr(jira_module.settings, "JIRA_API_TOKEN", "token")
    monkeypatch.setattr(jira_module.settings, "JIRA_HOST", "https://example.atlassian.net")
    return JiraClient()


def _phase_result(workspace, files, issue=None):
    return PhaseResult(
        issue=issue or {"id": "10001", "identifier": "ABC-123", "title": "Attach outputs"},
        workspace_path=str(workspace),
        repository_path=str(workspace / "repository"),
        phase_name="plan",
        agent_name="planner",
        agent_config=AgentConfig(
            command="fake",
            stdin="issue.json",
            output_file="plan.md",
            structured="planner-result.json",
        ),
        execution=AgentExecutionResult(
            exit_code=0,
            stdout="agent stdout",
            stderr="agent stderr",
            status="success",
            message="complete",
            needed_clarifications=[],
            files=files,
        ),
    )


def test_jira_registers_attach_outputs_action_without_prechecking(monkeypatch):
    client = _client(monkeypatch)
    registry = ActionRegistry()

    client.register_actions(registry)

    handler = registry.resolve("jira:attach_outputs")
    assert handler.__self__ is client
    assert handler.__func__ is JiraClient.attach_outputs
    with pytest.raises(ValueError, match="already registered"):
        client.register_actions(registry)


def test_attach_outputs_posts_all_files_in_one_multipart_request(monkeypatch, tmp_path):
    client = _client(monkeypatch)
    (tmp_path / "plan.md").write_text("plan text")
    nested = tmp_path / "artifacts"
    nested.mkdir()
    (nested / "blob.unknownext").write_bytes(b"\x00\x01")
    captured = {}

    def fake_post(url, headers, auth, files):
        captured["url"] = url
        captured["headers"] = headers
        captured["auth"] = auth
        captured["files"] = [
            (field, name, handle.read(), content_type)
            for field, (name, handle, content_type) in files
        ]
        return SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: [{"id": "1"}, {"id": "2"}],
        )

    monkeypatch.setattr(jira_module.requests, "post", fake_post)

    client.attach_outputs(
        _phase_result(tmp_path, ["plan.md", "artifacts/blob.unknownext"])
    )

    assert captured["url"].endswith("/rest/api/3/issue/ABC-123/attachments")
    assert captured["headers"] == {
        "Accept": "application/json",
        "X-Atlassian-Token": "no-check",
    }
    assert captured["files"] == [
        ("file", "plan.md", b"plan text", "text/markdown"),
        ("file", "artifacts/blob.unknownext", b"\x00\x01", "application/octet-stream"),
    ]


def test_attach_outputs_with_no_files_is_a_successful_noop(monkeypatch, tmp_path):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        jira_module.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("Jira must not be called"),
    )

    client.attach_outputs(_phase_result(tmp_path, []))


def test_attach_outputs_uploads_again_when_action_is_retried(monkeypatch, tmp_path):
    client = _client(monkeypatch)
    (tmp_path / "plan.md").write_text("plan")
    calls = []

    def fake_post(*_args, **_kwargs):
        calls.append(True)
        return SimpleNamespace(status_code=200, text="", json=lambda: [{"id": "1"}])

    monkeypatch.setattr(jira_module.requests, "post", fake_post)
    phase_result = _phase_result(tmp_path, ["plan.md"])

    client.attach_outputs(phase_result)
    client.attach_outputs(phase_result)

    assert calls == [True, True]


@pytest.mark.parametrize(
    "output_name, expected",
    [
        ("../outside.txt", "escapes workspace root"),
        ("missing.txt", "not found"),
        ("artifacts", "not a file"),
    ],
)
def test_attach_outputs_rejects_invalid_paths_before_http(
    monkeypatch,
    tmp_path,
    output_name,
    expected,
):
    client = _client(monkeypatch)
    (tmp_path / "artifacts").mkdir()
    monkeypatch.setattr(
        jira_module.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("Jira must not be called"),
    )

    with pytest.raises((ValueError, FileNotFoundError), match=expected):
        client.attach_outputs(_phase_result(tmp_path, [output_name]))


def test_attach_outputs_rejects_symlink_escape_before_http(monkeypatch, tmp_path):
    client = _client(monkeypatch)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside")
    (tmp_path / "link.txt").symlink_to(outside)
    monkeypatch.setattr(
        jira_module.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("Jira must not be called"),
    )

    with pytest.raises(ValueError, match="escapes workspace root"):
        client.attach_outputs(_phase_result(tmp_path, ["link.txt"]))


def test_attach_outputs_rejects_unreadable_file_before_http(monkeypatch, tmp_path):
    client = _client(monkeypatch)
    output = tmp_path / "plan.md"
    output.write_text("plan")
    real_access = jira_module.os.access
    monkeypatch.setattr(
        jira_module.os,
        "access",
        lambda path, mode: False if path == str(output) else real_access(path, mode),
    )
    monkeypatch.setattr(
        jira_module.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("Jira must not be called"),
    )

    with pytest.raises(PermissionError, match="not readable"):
        client.attach_outputs(_phase_result(tmp_path, ["plan.md"]))


@pytest.mark.parametrize("status_code", [401, 403, 413, 500])
def test_attach_outputs_rejects_http_failures(monkeypatch, tmp_path, status_code):
    client = _client(monkeypatch)
    (tmp_path / "plan.md").write_text("plan")
    response = SimpleNamespace(status_code=status_code, text="failed")
    monkeypatch.setattr(jira_module.requests, "post", lambda *_args, **_kwargs: response)

    with pytest.raises(RuntimeError, match=rf"failed \({status_code}\)"):
        client.attach_outputs(_phase_result(tmp_path, ["plan.md"]))


def test_attach_outputs_wraps_connection_failure(monkeypatch, tmp_path):
    client = _client(monkeypatch)
    (tmp_path / "plan.md").write_text("plan")

    def fail(*_args, **_kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(jira_module.requests, "post", fail)

    with pytest.raises(RuntimeError, match="attachment request failed: offline"):
        client.attach_outputs(_phase_result(tmp_path, ["plan.md"]))


@pytest.mark.parametrize("payload", [None, {}, [], [None]])
def test_attach_outputs_rejects_malformed_response(monkeypatch, tmp_path, payload):
    client = _client(monkeypatch)
    (tmp_path / "plan.md").write_text("plan")
    response = SimpleNamespace(status_code=200, text="", json=lambda: payload)
    monkeypatch.setattr(jira_module.requests, "post", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="non-empty attachment array"):
        client.attach_outputs(_phase_result(tmp_path, ["plan.md"]))


def test_attach_outputs_rejects_invalid_json_response(monkeypatch, tmp_path):
    client = _client(monkeypatch)
    (tmp_path / "plan.md").write_text("plan")

    def invalid_json():
        raise ValueError("invalid")

    response = SimpleNamespace(status_code=200, text="", json=invalid_json)
    monkeypatch.setattr(jira_module.requests, "post", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="not valid JSON"):
        client.attach_outputs(_phase_result(tmp_path, ["plan.md"]))
