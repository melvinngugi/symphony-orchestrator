from types import SimpleNamespace

import pytest
import requests

from app.core.workflow_validation import WorkflowValidationError
from app.services import jira as jira_module
from app.services.jira import JiraClient


def _client(monkeypatch):
    monkeypatch.setattr(type(jira_module.settings), "validate_jira", lambda self: None)
    monkeypatch.setattr(jira_module.settings, "JIRA_USER_EMAIL", "bot@example.com")
    monkeypatch.setattr(jira_module.settings, "JIRA_API_TOKEN", "token")
    monkeypatch.setattr(jira_module.settings, "JIRA_HOST", "https://example.atlassian.net")
    monkeypatch.setattr(jira_module.settings, "JIRA_PROJECT_KEY", "SHOP SPACE")
    return JiraClient()


def test_fetch_project_status_names_flattens_and_deduplicates(monkeypatch):
    client = _client(monkeypatch)
    captured = {}
    payload = [
        {
            "name": "Task",
            "statuses": [
                {"name": "To Do"},
                {"name": "In Progress"},
            ],
        },
        {
            "name": "Bug",
            "statuses": [
                {"name": "to do"},
                {"name": "Done"},
            ],
        },
    ]

    def fake_get(url, headers, auth):
        captured["url"] = url
        captured["headers"] = headers
        captured["auth"] = auth
        return SimpleNamespace(status_code=200, text="", json=lambda: payload)

    monkeypatch.setattr(jira_module.requests, "get", fake_get)

    names = client.fetch_project_status_names()

    assert names == {"To Do", "In Progress", "Done"}
    assert captured["url"].endswith("/rest/api/3/project/SHOP%20SPACE/statuses")


def test_fetch_project_status_names_rejects_http_failure(monkeypatch):
    client = _client(monkeypatch)
    response = SimpleNamespace(status_code=401, text="unauthorized")
    monkeypatch.setattr(jira_module.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(RuntimeError, match=r"failed \(401\): unauthorized"):
        client.fetch_project_status_names()


def test_fetch_project_status_names_wraps_connection_failure(monkeypatch):
    client = _client(monkeypatch)

    def fail_get(*_args, **_kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(jira_module.requests, "get", fail_get)

    with pytest.raises(RuntimeError, match="request failed: offline"):
        client.fetch_project_status_names()


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({}, "must be an array"),
        ([None], r"issue type \[0\] must be an object"),
        ([{}], r"issue type \[0\].statuses must be an array"),
        ([{"statuses": [None]}], r"status \[0\]\[0\] must be an object"),
        ([{"statuses": [{}]}], r"status \[0\]\[0\].name must be a non-empty string"),
        ([], "contains no statuses"),
        ([{"statuses": []}], "contains no statuses"),
    ],
)
def test_fetch_project_status_names_rejects_malformed_payload(monkeypatch, payload, expected):
    client = _client(monkeypatch)
    response = SimpleNamespace(status_code=200, text="", json=lambda: payload)
    monkeypatch.setattr(jira_module.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match=expected):
        client.fetch_project_status_names()


def test_fetch_project_status_names_rejects_invalid_json(monkeypatch):
    client = _client(monkeypatch)

    def invalid_json():
        raise ValueError("invalid")

    response = SimpleNamespace(status_code=200, text="", json=invalid_json)
    monkeypatch.setattr(jira_module.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="not valid JSON"):
        client.fetch_project_status_names()


def test_validate_workflow_states_accepts_all_references_case_insensitively(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        client,
        "fetch_project_status_names",
        lambda: {"To Do", "In Progress", "Done", "Clarification Needed"},
    )
    config = {
        "phases": {
            "plan": {
                "states": ["to do"],
                "transitions": {
                    "on_start": "IN PROGRESS",
                    "success": "done",
                    "blocked": {"next": "clarification needed", "do": []},
                },
            }
        }
    }

    client.validate_workflow_states(config)


def test_validate_workflow_states_reports_all_invalid_references(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(client, "fetch_project_status_names", lambda: {"To Do", "Done"})
    config = {
        "phases": {
            "plan": {
                "states": ["Missing Trigger"],
                "transitions": {
                    "on_start": "Missing Start",
                    "success": "Missing Success",
                    "blocked": {"next": "Missing Blocked", "do": []},
                },
            }
        }
    }

    with pytest.raises(WorkflowValidationError) as exc_info:
        client.validate_workflow_states(config)

    message = str(exc_info.value)
    assert "phases.plan.states[0]: unknown Jira state 'Missing Trigger'" in message
    assert "phases.plan.transitions.on_start: unknown Jira state 'Missing Start'" in message
    assert "phases.plan.transitions.success: unknown Jira state 'Missing Success'" in message
    assert "phases.plan.transitions.blocked.next: unknown Jira state 'Missing Blocked'" in message
    assert len(exc_info.value.errors) == 4


def test_validate_workflow_states_wraps_status_discovery_failure(monkeypatch):
    client = _client(monkeypatch)

    def fail():
        raise RuntimeError("Jira unavailable")

    monkeypatch.setattr(client, "fetch_project_status_names", fail)

    with pytest.raises(
        WorkflowValidationError,
        match="jira.project_statuses.*Jira unavailable",
    ):
        client.validate_workflow_states({"phases": {}})
