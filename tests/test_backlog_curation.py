import json
from types import SimpleNamespace

import pytest

from app.models.agent_config import AgentConfig
from app.services.actions import PhaseResult
from app.services.agent import AgentExecutionResult
from app.services.backlog import (
    BacklogCurationInputProvider,
    cyclic_dependency_pairs,
    load_and_validate_curation_result,
)
from app.services.jira import JiraClient
from app.services import jira as jira_module


WEIGHTS = {
    "customerImpact": 0.35,
    "revenueOrCostImpact": 0.25,
    "strategicAlignment": 0.25,
    "riskReduction": 0.15,
}


def _ticket(key, *, updated="v1", links=None, score=None, rationale=None):
    return {
        "key": key,
        "updated": updated,
        "labels": [],
        "links": links or [],
        "businessValue": {"score": score, "rationale": rationale},
    }


def _input(tickets):
    return {
        "runId": "backlog_curation:2026-09-03",
        "sourceSnapshotAt": "2026-09-03T00:00:00+00:00",
        "scoringWeights": WEIGHTS,
        "tickets": tickets,
        "strategyDocuments": [{"id": "42"}],
    }


def _result(**overrides):
    result = {
        "runId": "backlog_curation:2026-09-03",
        "sourceSnapshotAt": "2026-09-03T00:00:00+00:00",
        "ticketValues": [],
        "dependencies": [],
        "clarifications": [],
        "warnings": [],
    }
    result.update(overrides)
    return result


def _value(key, confidence=0.9):
    return {
        "issueKey": key,
        "dimensions": {
            "customerImpact": 80,
            "revenueOrCostImpact": 60,
            "strategicAlignment": 40,
            "riskReduction": 20,
        },
        "score": 56,
        "confidence": confidence,
        "rationale": "Supports the current strategy.",
        "evidence": [f"jira:{key}", "confluence:42"],
    }


def _write_result(tmp_path, payload):
    path = tmp_path / "backlog-curation.json"
    path.write_text(json.dumps(payload))
    return path


def test_result_validation_rejects_wrong_weighted_score_and_unknown_evidence(tmp_path):
    payload = _result(ticketValues=[_value("SHOP-1")])
    payload["ticketValues"][0]["score"] = 57
    path = _write_result(tmp_path, payload)
    with pytest.raises(ValueError, match="weighted score 56"):
        load_and_validate_curation_result(str(path), _input([_ticket("SHOP-1")]))

    payload["ticketValues"][0]["score"] = 56
    payload["ticketValues"][0]["evidence"] = ["jira:OUT-1"]
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Unknown evidence"):
        load_and_validate_curation_result(str(path), _input([_ticket("SHOP-1")]))


def test_cycle_detection_identifies_only_proposal_that_closes_cycle(tmp_path):
    tickets = [
        _ticket("SHOP-1"),
        _ticket(
            "SHOP-2",
            links=[{"type": "Blocks", "direction": "outward", "issueKey": "SHOP-1"}],
        ),
    ]
    payload = _result(
        ticketValues=[_value("SHOP-1"), _value("SHOP-2")],
        dependencies=[
            {
                "blocker": "SHOP-1",
                "blocked": "SHOP-2",
                "confidence": 0.99,
                "evidence": ["jira:SHOP-1", "jira:SHOP-2"],
            }
        ]
    )
    result = load_and_validate_curation_result(
        str(_write_result(tmp_path, payload)), _input(tickets)
    )
    assert cyclic_dependency_pairs(result, {ticket["key"]: ticket for ticket in tickets}) == {
        ("SHOP-1", "SHOP-2")
    }


def test_input_provider_requires_configured_strategy_documents():
    class Tracker:
        def fetch_backlog_issues(self, *_args, **_kwargs):
            return [_ticket("SHOP-1")]

    provider = BacklogCurationInputProvider(Tracker(), None)
    with pytest.raises(ValueError, match="strategy context is configured"):
        provider.build_input(
            "curation",
            "curation:2026-09-03",
            {
                "audit_issue": "SHOP-CURATION",
                "input": {
                    "strategy_pages": {
                        "titles": ["Product Strategy"],
                        "space_keys": ["STRATEGY"],
                    }
                },
                "jira": {
                    "business_value_score_field": "customfield_1",
                    "business_value_rationale_field": "customfield_2",
                },
            },
        )


def test_input_provider_fetches_strategy_documents_by_name():
    class Tracker:
        def fetch_backlog_issues(self, *_args, **_kwargs):
            return [_ticket("SHOP-1")]

    class Documents:
        def fetch_documents_by_name(self, names):
            assert names == ["Product Strategy", "Product Goals 2027"]
            return [{"id": "42", "title": names[0], "text": "Grow retention."}]

        def fetch_documents_by_id(self, _ids):
            raise AssertionError("The curator must use title-based lookup")

        def fetch_documents_by_url(self, urls):
            assert urls == []
            return []

    provider = BacklogCurationInputProvider(Tracker(), {"curation": Documents()})
    payload = json.loads(
        provider.build_input(
            "curation",
            "curation:2026-09-03",
            {
                "audit_issue": "SHOP-CURATION",
                "input": {
                    "strategy_pages": {
                        "titles": ["Product Strategy", "Product Goals 2027"],
                        "space_keys": ["STRATEGY"],
                    }
                },
                "jira": {
                    "business_value_score_field": "customfield_1",
                    "business_value_rationale_field": "customfield_2",
                },
            },
        )
    )
    assert payload["strategyDocuments"] == [
        {"id": "42", "title": "Product Strategy", "text": "Grow retention."}
    ]
    assert "untrusted reference data" in payload["contentPolicy"]


def test_input_provider_combines_titles_and_urls_and_deduplicates_by_page_id():
    class Tracker:
        def fetch_backlog_issues(self, *_args, **_kwargs):
            return [_ticket("SHOP-1")]

    class Documents:
        def fetch_documents_by_name(self, names):
            assert names == ["Product Strategy"]
            return [{"id": "42", "title": names[0], "text": "Title result"}]

        def fetch_documents_by_id(self, _ids):
            raise AssertionError("Strategy references use name and URL lookup")

        def fetch_documents_by_url(self, urls):
            assert urls == [
                "https://example.atlassian.net/wiki/spaces/STRATEGY/pages/42/Product+Strategy",
                "https://example.atlassian.net/wiki/spaces/PRODUCT/overview",
            ]
            return [
                {"id": "42", "title": "Product Strategy", "text": "URL result"},
                {"id": "99", "title": "Product Home", "text": "Goals"},
            ]

    provider = BacklogCurationInputProvider(Tracker(), {"curation": Documents()})
    payload = json.loads(
        provider.build_input(
            "curation",
            "curation:2026-09-03",
            {
                "audit_issue": "SHOP-CURATION",
                "input": {
                    "strategy_pages": {
                        "titles": ["Product Strategy"],
                        "urls": [
                            "https://example.atlassian.net/wiki/spaces/STRATEGY/pages/42/Product+Strategy",
                            "https://example.atlassian.net/wiki/spaces/PRODUCT/overview",
                        ],
                        "space_keys": ["STRATEGY"],
                    }
                },
                "jira": {
                    "business_value_score_field": "customfield_1",
                    "business_value_rationale_field": "customfield_2",
                },
            },
        )
    )

    assert [document["id"] for document in payload["strategyDocuments"]] == [
        "42",
        "99",
    ]
    assert payload["strategyDocuments"][0]["text"] == "Title result"


def test_apply_curation_routes_confidence_cycles_and_clarifications(tmp_path):
    tickets = [
        _ticket("SHOP-1"),
        _ticket("SHOP-2"),
        _ticket(
            "SHOP-3",
            links=[{"type": "Blocks", "direction": "outward", "issueKey": "SHOP-2"}],
        ),
    ]
    input_payload = _input(tickets)
    (tmp_path / "backlog-curation-input.json").write_text(json.dumps(input_payload))
    result_payload = _result(
        ticketValues=[
            _value("SHOP-1"),
            _value("SHOP-2", confidence=0.5),
            _value("SHOP-3", confidence=0.5),
        ],
        dependencies=[
            {
                "blocker": "SHOP-1", "blocked": "SHOP-2", "confidence": 0.95,
                "evidence": ["jira:SHOP-1", "jira:SHOP-2"],
            },
            {
                "blocker": "SHOP-2", "blocked": "SHOP-3", "confidence": 0.95,
                "evidence": ["jira:SHOP-2", "jira:SHOP-3"],
            },
        ],
        clarifications=[
            {
                "issueKey": "SHOP-1",
                "questions": ["Which customer segment is in scope?"],
                "confidence": 0.95,
                "evidence": ["jira:SHOP-1"],
            }
        ],
    )
    _write_result(tmp_path, result_payload)

    client = JiraClient.__new__(JiraClient)
    fresh = {ticket["key"]: dict(ticket) for ticket in tickets}
    calls = {"fields": [], "labels": [], "links": [], "comments": [], "properties": []}
    client._fetch_curation_issue = lambda key, *_args: fresh[key]
    client._get_curator_property = lambda _key: {}
    client._update_issue_fields = lambda key, fields: calls["fields"].append((key, fields))
    client._put_curator_property = lambda key, value: calls["properties"].append((key, value))
    client._add_label = lambda key, label: calls["labels"].append((key, label))
    client._create_issue_link = lambda blocker, blocked, kind: calls["links"].append(
        (blocker, blocked, kind)
    )
    client._comment_contains = lambda *_args: False
    client.add_comment = lambda key, body: calls["comments"].append((key, body)) or True

    execution = AgentExecutionResult(0, "", "", files=["backlog-curation.json"])
    phase_result = PhaseResult(
        issue={"id": "scheduled", "identifier": "SHOP-CURATION", "run_id": result_payload["runId"]},
        workspace_path=str(tmp_path),
        repository_path=str(tmp_path),
        phase_name="backlog_curation",
        agent_name="backlog_curator",
        agent_config=AgentConfig(command="fake", stdin="backlog-curation-input.json"),
        execution=execution,
        phase_config={
            "dry_run": False,
            "jira": {
                "business_value_score_field": "customfield_1",
                "business_value_rationale_field": "customfield_2",
                "clarification_label": "needs-clarification",
                "review_label": "backlog-agent-review",
                "dependency_link_type": "Blocks",
            },
            "confidence": {"business_value": 0.85, "dependency": 0.9, "clarification": 0.9},
        },
    )

    client.apply_backlog_curation(phase_result)

    assert calls["fields"][0][0] == "SHOP-1"
    assert calls["links"] == [("SHOP-1", "SHOP-2", "Blocks")]
    assert ("SHOP-2", "backlog-agent-review") in calls["labels"]
    assert ("SHOP-3", "backlog-agent-review") in calls["labels"]
    assert ("SHOP-1", "needs-clarification") in calls["labels"]
    assert "Which customer segment" in calls["comments"][0][1]
    assert "backlog-curation-report.json" in execution.files
    report = json.loads((tmp_path / "backlog-curation-report.json").read_text())
    assert any(item["reason"] == "dependency-cycle" for item in report["reviewRequired"])


def test_jira_backlog_search_pages_and_excludes_audit_and_ignore_label(monkeypatch):
    client = JiraClient.__new__(JiraClient)
    client.base_url = "https://example.atlassian.net"
    client.headers = {"Accept": "application/json"}
    client.auth = object()
    client.request_timeout = (1, 2)
    calls = []

    def raw_issue(key, labels=None):
        return {
            "id": key.split("-")[-1],
            "key": key,
            "fields": {
                "summary": key,
                "labels": labels or [],
                "issuelinks": [],
                "components": [],
                "updated": "v1",
            },
        }

    pages = [
        {"issues": [raw_issue("SHOP-1"), raw_issue("SHOP-CURATION")], "nextPageToken": "next"},
        {"issues": [raw_issue("SHOP-2", ["skip"]), raw_issue("SHOP-3")], "isLast": True},
    ]

    def fake_get(_url, **kwargs):
        calls.append(kwargs["params"])
        return SimpleNamespace(status_code=200, text="", json=lambda: pages[len(calls) - 1])

    monkeypatch.setattr(jira_module.requests, "get", fake_get)
    issues = client.fetch_backlog_issues(
        "project = SHOP",
        score_field="customfield_1",
        rationale_field="customfield_2",
        audit_issue="SHOP-CURATION",
        ignore_label="skip",
    )
    assert [issue["key"] for issue in issues] == ["SHOP-1", "SHOP-3"]
    assert calls[1]["nextPageToken"] == "next"
    assert "issuelinks" in calls[0]["fields"]


def test_apply_curation_protects_stale_and_human_values(tmp_path):
    snapshot = [_ticket("SHOP-1"), _ticket("SHOP-2", score=20, rationale="Human")]
    (tmp_path / "backlog-curation-input.json").write_text(json.dumps(_input(snapshot)))
    _write_result(
        tmp_path,
        _result(ticketValues=[_value("SHOP-1"), _value("SHOP-2")]),
    )
    client = JiraClient.__new__(JiraClient)
    fresh = {
        "SHOP-1": _ticket("SHOP-1", updated="v2"),
        "SHOP-2": _ticket("SHOP-2", score=20, rationale="Human"),
    }
    labels = []
    client._fetch_curation_issue = lambda key, *_args: fresh[key]
    client._get_curator_property = lambda _key: {}
    client._add_label = lambda key, label: labels.append((key, label))
    client._update_issue_fields = lambda *_args: pytest.fail("Protected values must not update")
    client._put_curator_property = lambda *_args: None
    execution = AgentExecutionResult(0, "", "", files=["backlog-curation.json"])
    phase_result = PhaseResult(
        issue={"id": "scheduled", "identifier": "SHOP-CURATION"},
        workspace_path=str(tmp_path),
        repository_path=str(tmp_path),
        phase_name="backlog_curation",
        agent_name="backlog_curator",
        agent_config=AgentConfig(command="fake", stdin="backlog-curation-input.json"),
        execution=execution,
        phase_config={
            "dry_run": False,
            "jira": {
                "business_value_score_field": "customfield_1",
                "business_value_rationale_field": "customfield_2",
                "clarification_label": "needs-clarification",
                "review_label": "review",
                "dependency_link_type": "Blocks",
            },
            "confidence": {"business_value": 0.85, "dependency": 0.9, "clarification": 0.9},
        },
    )
    client.apply_backlog_curation(phase_result)
    assert labels == [("SHOP-1", "review"), ("SHOP-2", "review")]
    report = json.loads((tmp_path / "backlog-curation-report.json").read_text())
    assert {item["reason"] for item in report["reviewRequired"]} == {
        "ticket-changed-since-snapshot",
        "human-value-protected",
    }


def test_curator_attachments_are_run_qualified_and_retry_safe(tmp_path):
    (tmp_path / "backlog-curation.json").write_text("{}")
    (tmp_path / "backlog-curation-report.json").write_text("{}")
    client = JiraClient.__new__(JiraClient)
    existing = {"backlog-curation-backlog_curation-2026-09-03.json"}
    uploads = []
    client._attachment_names = lambda _key: set(existing)

    def upload(_key, name, path):
        uploads.append((name, path))
        existing.add(name)

    client._upload_single_attachment = upload
    phase_result = PhaseResult(
        issue={
            "identifier": "SHOP-CURATION",
            "run_id": "backlog_curation:2026-09-03",
        },
        workspace_path=str(tmp_path),
        repository_path=str(tmp_path),
        phase_name="backlog_curation",
        agent_name="backlog_curator",
        agent_config=AgentConfig(command="fake", stdin="backlog-curation-input.json"),
        execution=AgentExecutionResult(
            0,
            "",
            "",
            files=["backlog-curation.json", "backlog-curation-report.json"],
        ),
    )
    client.attach_curation_outputs(phase_result)
    client.attach_curation_outputs(phase_result)
    assert [name for name, _path in uploads] == [
        "backlog-curation-report-backlog_curation-2026-09-03.json"
    ]


def test_business_value_retry_recovers_lost_update_response(tmp_path):
    snapshot = [_ticket("SHOP-1")]
    (tmp_path / "backlog-curation-input.json").write_text(json.dumps(_input(snapshot)))
    _write_result(tmp_path, _result(ticketValues=[_value("SHOP-1")]))
    current = _ticket("SHOP-1")
    stored_property = {}
    update_calls = 0
    client = JiraClient.__new__(JiraClient)
    client._fetch_curation_issue = lambda *_args: dict(current)
    client._get_curator_property = lambda _key: dict(stored_property)

    def put_property(_key, value):
        stored_property.clear()
        stored_property.update(value)

    def update_fields(_key, _fields):
        nonlocal update_calls
        update_calls += 1
        current["businessValue"] = {
            "score": 56,
            "rationale": "Supports the current strategy.",
        }
        current["updated"] = "v2"
        raise RuntimeError("response lost")

    client._put_curator_property = put_property
    client._update_issue_fields = update_fields
    phase_result = PhaseResult(
        issue={"id": "scheduled", "identifier": "SHOP-CURATION"},
        workspace_path=str(tmp_path),
        repository_path=str(tmp_path),
        phase_name="backlog_curation",
        agent_name="backlog_curator",
        agent_config=AgentConfig(command="fake", stdin="backlog-curation-input.json"),
        execution=AgentExecutionResult(0, "", "", files=["backlog-curation.json"]),
        phase_config={
            "dry_run": False,
            "jira": {
                "business_value_score_field": "customfield_1",
                "business_value_rationale_field": "customfield_2",
                "clarification_label": "needs-clarification",
                "review_label": "review",
                "dependency_link_type": "Blocks",
            },
            "confidence": {"business_value": 0.85, "dependency": 0.9, "clarification": 0.9},
        },
    )

    with pytest.raises(RuntimeError, match="response lost"):
        client.apply_backlog_curation(phase_result)
    client.apply_backlog_curation(phase_result)

    assert update_calls == 1
    assert stored_property["lastKnownUpdated"] == "v2"
    report = json.loads((tmp_path / "backlog-curation-report.json").read_text())
    assert report["skipped"] == [
        {"issueKey": "SHOP-1", "reason": "value-already-applied", "type": "businessValue"}
    ]
