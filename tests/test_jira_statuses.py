from types import SimpleNamespace

import pytest
import requests

from app.core.workflow_validation import (
    WorkflowStateValidationError,
    WorkflowValidationError,
)
from app.services import jira as jira_module
from app.services.jira import JiraClient


def _client(monkeypatch):
    monkeypatch.setattr(type(jira_module.settings), "validate_jira", lambda self: None)
    monkeypatch.setattr(jira_module.settings, "JIRA_USER_EMAIL", "bot@example.com")
    monkeypatch.setattr(jira_module.settings, "JIRA_API_TOKEN", "token")
    monkeypatch.setattr(jira_module.settings, "JIRA_HOST", "https://example.atlassian.net")
    monkeypatch.setattr(jira_module.settings, "JIRA_PROJECT_KEY", "SHOP SPACE")
    return JiraClient()


def _response(payload, status_code=200, text=""):
    return SimpleNamespace(
        status_code=status_code,
        text=text,
        json=lambda: payload,
    )


def test_fetch_project_status_names_resolves_project_and_paginates(monkeypatch):
    client = _client(monkeypatch)
    calls = []

    def fake_get(url, headers, auth, params=None, timeout=None):
        assert timeout == client.request_timeout
        calls.append((url, headers, auth, params))
        if url.endswith("/rest/api/3/project/SHOP%20SPACE"):
            return _response({"id": "10001"})
        if params["startAt"] == 0:
            return _response(
                {
                    "startAt": 0,
                    "isLast": False,
                    "values": [{"name": "To Do"}, {"name": "In Progress"}],
                }
            )
        return _response(
            {
                "startAt": 2,
                "isLast": True,
                "values": [{"name": " to do "}, {"name": "Done"}],
            }
        )

    monkeypatch.setattr(jira_module.requests, "get", fake_get)

    names = client.fetch_project_status_names()

    assert names == {"To Do", "In Progress", "Done"}
    assert calls[0][0].endswith("/rest/api/3/project/SHOP%20SPACE")
    assert [call[0] for call in calls[1:]] == [
        "https://example.atlassian.net/rest/api/3/statuses/search",
        "https://example.atlassian.net/rest/api/3/statuses/search",
    ]
    assert [call[3] for call in calls[1:]] == [
        {
            "projectId": "10001",
            "includeGlobalStatuses": True,
            "startAt": 0,
            "maxResults": 100,
        },
        {
            "projectId": "10001",
            "includeGlobalStatuses": True,
            "startAt": 2,
            "maxResults": 100,
        },
    ]


def test_fetch_project_status_names_rejects_project_lookup_http_failure(monkeypatch):
    client = _client(monkeypatch)
    response = SimpleNamespace(status_code=404, text="missing")
    monkeypatch.setattr(jira_module.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(RuntimeError, match=r"project lookup request failed \(404\): missing"):
        client.fetch_project_status_names()


def test_fetch_project_status_names_wraps_project_lookup_connection_failure(monkeypatch):
    client = _client(monkeypatch)

    def fail_get(*_args, **_kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(jira_module.requests, "get", fail_get)

    with pytest.raises(RuntimeError, match="project lookup request failed: offline"):
        client.fetch_project_status_names()


@pytest.mark.parametrize(
    "payload, expected",
    [
        ([], "must be an object"),
        ({}, "id must be a numeric value"),
        ({"id": True}, "id must be a numeric value"),
        ({"id": "ABC"}, "id must be a numeric value"),
    ],
)
def test_fetch_project_status_names_rejects_malformed_project_lookup(
    monkeypatch, payload, expected
):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        jira_module.requests,
        "get",
        lambda *_args, **_kwargs: _response(payload),
    )

    with pytest.raises(ValueError, match=expected):
        client.fetch_project_status_names()


def test_fetch_project_status_names_rejects_invalid_project_lookup_json(monkeypatch):
    client = _client(monkeypatch)

    def invalid_json():
        raise ValueError("invalid")

    response = SimpleNamespace(status_code=200, text="", json=invalid_json)
    monkeypatch.setattr(jira_module.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="project lookup response is not valid JSON"):
        client.fetch_project_status_names()


def test_fetch_project_status_names_rejects_search_http_failure(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(client, "_fetch_project_id", lambda: "10001")
    response = SimpleNamespace(status_code=401, text="unauthorized")
    monkeypatch.setattr(jira_module.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(RuntimeError, match=r"status search request failed \(401\): unauthorized"):
        client.fetch_project_status_names()


def test_fetch_project_status_names_wraps_search_connection_failure(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(client, "_fetch_project_id", lambda: "10001")

    def fail_get(*_args, **_kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(jira_module.requests, "get", fail_get)

    with pytest.raises(RuntimeError, match="status search request failed: offline"):
        client.fetch_project_status_names()


@pytest.mark.parametrize(
    "payload, expected",
    [
        ([], "must be an object"),
        ({"startAt": 0, "isLast": True}, "values must be an array"),
        ({"startAt": 0, "isLast": True, "values": [None]}, r"status \[0\] must be an object"),
        (
            {"startAt": 0, "isLast": True, "values": [{}]},
            r"status \[0\].name must be a non-empty string",
        ),
        ({"startAt": "0", "isLast": True, "values": []}, "startAt"),
        ({"startAt": 1, "isLast": True, "values": []}, "startAt"),
        ({"startAt": 0, "isLast": "true", "values": []}, "isLast must be a boolean"),
        ({"startAt": 0, "isLast": False, "values": []}, "pagination made no progress"),
        ({"startAt": 0, "isLast": True, "values": []}, "contains no statuses"),
    ],
)
def test_fetch_project_status_names_rejects_malformed_search_payload(
    monkeypatch, payload, expected
):
    client = _client(monkeypatch)
    monkeypatch.setattr(client, "_fetch_project_id", lambda: "10001")
    monkeypatch.setattr(
        jira_module.requests,
        "get",
        lambda *_args, **_kwargs: _response(payload),
    )

    with pytest.raises(ValueError, match=expected):
        client.fetch_project_status_names()


def test_fetch_project_status_names_rejects_invalid_search_json(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(client, "_fetch_project_id", lambda: "10001")

    def invalid_json():
        raise ValueError("invalid")

    response = SimpleNamespace(status_code=200, text="", json=invalid_json)
    monkeypatch.setattr(jira_module.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="status search response is not valid JSON"):
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

    with pytest.raises(WorkflowStateValidationError) as exc_info:
        client.validate_workflow_states(config)

    message = str(exc_info.value)
    assert "phases.plan.states[0]: unknown Jira state 'Missing Trigger'" in message
    assert "phases.plan.transitions.on_start: unknown Jira state 'Missing Start'" in message
    assert "phases.plan.transitions.success: unknown Jira state 'Missing Success'" in message
    assert "phases.plan.transitions.blocked.next: unknown Jira state 'Missing Blocked'" in message
    assert "jira.project_statuses: available Jira states: 'Done', 'To Do'" in message
    assert len(exc_info.value.errors) == 5


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
