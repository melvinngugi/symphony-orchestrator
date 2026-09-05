from pathlib import Path

import pytest
import yaml

from app.core import config as config_module
from app.core.config import (
    ProjectConfigLoadError,
    ProjectCredentialValidationError,
    load_project_config,
    validate_project_credentials,
)


WEIGHTS = {
    "customerImpact": 0.35,
    "revenueOrCostImpact": 0.25,
    "strategicAlignment": 0.25,
    "riskReduction": 0.15,
}


def _write_parameters(path: Path, payload=None):
    if payload is None:
        payload = {"scoring_weights": WEIGHTS}
    path.write_text(yaml.safe_dump(payload))


def _workflow():
    return {
        "project": {
            "jira": {
                "host": "https://workflow.atlassian.net",
                "key": "FLOW",
                "fields": {
                    "business_value_score": "customfield_10001",
                    "business_value_rationale": "customfield_10002",
                    "epic": "",
                },
                "backlog": {"jql": "status = Open", "ignore_label": "ignore-me"},
            },
            "bitbucket": {"workspace": "workflow-space", "repository": "workflow-repo"},
            "confluence": {
                "host": "https://workflow.atlassian.net",
                "strategy_pages": {
                    "titles": [],
                    "urls": [],
                    "space_keys": [],
                    "fail_on_missing": True,
                },
            },
            "business_value_parameters": "business_value_parameters.yaml",
        }
    }


def test_project_values_override_environment_and_parameters_are_workflow_relative(
    monkeypatch, tmp_path
):
    workflow_path = tmp_path / "nested" / "WORKFLOW.md"
    workflow_path.parent.mkdir()
    _write_parameters(workflow_path.parent / "business_value_parameters.yaml")
    monkeypatch.setenv("JIRA_HOST", "https://environment.atlassian.net")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "ENV")
    monkeypatch.setenv("BITBUCKET_WORKSPACE", "environment-space")
    monkeypatch.setenv("BITBUCKET_REPO_SLUG", "environment-repo")
    monkeypatch.setenv("CONFLUENCE_HOST", "https://environment.atlassian.net")

    project = load_project_config(_workflow(), workflow_path)

    assert project.jira.host == "https://workflow.atlassian.net"
    assert project.jira.key == "FLOW"
    assert project.bitbucket.workspace == "workflow-space"
    assert project.bitbucket.repository == "workflow-repo"
    assert project.confluence.host == "https://workflow.atlassian.net"
    assert project.business_value_parameters.scoring_weights == WEIGHTS


def test_empty_project_identity_uses_environment_fallbacks(monkeypatch, tmp_path):
    config = _workflow()
    config["project"]["jira"]["host"] = ""
    config["project"]["jira"]["key"] = ""
    config["project"]["bitbucket"]["workspace"] = ""
    config["project"]["bitbucket"]["repository"] = ""
    config["project"]["confluence"]["host"] = ""
    _write_parameters(tmp_path / "business_value_parameters.yaml")
    monkeypatch.setenv("JIRA_HOST", "https://environment.atlassian.net")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "ENV")
    monkeypatch.setenv("BITBUCKET_WORKSPACE", "environment-space")
    monkeypatch.setenv("BITBUCKET_REPO_SLUG", "environment-repo")
    monkeypatch.setenv("CONFLUENCE_HOST", "https://environment.atlassian.net")

    project = load_project_config(config, tmp_path / "WORKFLOW.md")

    assert project.jira.key == "ENV"
    assert project.bitbucket.repository == "environment-repo"
    assert project.confluence.host == "https://environment.atlassian.net"


def test_missing_identity_fails_after_workflow_and_environment(monkeypatch, tmp_path):
    config = _workflow()
    config["project"]["jira"]["key"] = ""
    _write_parameters(tmp_path / "business_value_parameters.yaml")
    monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)

    with pytest.raises(ProjectConfigLoadError, match="JIRA_PROJECT_KEY must be set"):
        load_project_config(config, tmp_path / "WORKFLOW.md")


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({}, "scoring_weights: must be a mapping"),
        ({"scoring_weights": {"customerImpact": 1}}, "four scoring dimensions"),
        ({"scoring_weights": {**WEIGHTS, "customerImpact": -0.1}}, "sum to 1"),
        ({"scoring_weights": {**WEIGHTS, "customerImpact": "high"}}, "sum to 1"),
        ({"scoring_weights": {**WEIGHTS, "customerImpact": float("nan")}}, "sum to 1"),
    ],
)
def test_business_value_parameter_schema_is_strict(tmp_path, payload, expected):
    _write_parameters(tmp_path / "business_value_parameters.yaml", payload)

    with pytest.raises(ProjectConfigLoadError, match=expected):
        load_project_config(_workflow(), tmp_path / "WORKFLOW.md")


def test_missing_parameter_file_reports_resolved_path(tmp_path):
    missing = tmp_path / "business_value_parameters.yaml"

    with pytest.raises(ProjectConfigLoadError, match=str(missing)):
        load_project_config(_workflow(), tmp_path / "WORKFLOW.md")


def test_absolute_parameter_path_is_supported(tmp_path):
    parameter_path = tmp_path / "shared-parameters.yaml"
    _write_parameters(parameter_path)
    config = _workflow()
    config["project"]["business_value_parameters"] = str(parameter_path)

    project = load_project_config(config, tmp_path / "config" / "WORKFLOW.md")

    assert project.business_value_parameters.scoring_weights == WEIGHTS


def test_malformed_parameter_file_is_rejected(tmp_path):
    (tmp_path / "business_value_parameters.yaml").write_text("scoring_weights: [")

    with pytest.raises(ProjectConfigLoadError, match="unable to load"):
        load_project_config(_workflow(), tmp_path / "WORKFLOW.md")


def test_project_credentials_are_rejected(tmp_path):
    config = _workflow()
    config["project"]["jira"]["api_token"] = "must-not-be-here"
    _write_parameters(tmp_path / "business_value_parameters.yaml")

    with pytest.raises(ProjectConfigLoadError, match="api_token: unknown configuration field"):
        load_project_config(config, tmp_path / "WORKFLOW.md")


def test_jira_field_ids_are_validated(tmp_path):
    config = _workflow()
    config["project"]["jira"]["fields"]["business_value_score"] = "Business Value"
    _write_parameters(tmp_path / "business_value_parameters.yaml")

    with pytest.raises(ProjectConfigLoadError, match="must be a Jira customfield_<id>"):
        load_project_config(config, tmp_path / "WORKFLOW.md")


def test_confluence_credentials_are_required_when_strategy_pages_are_configured(
    monkeypatch, tmp_path
):
    config = _workflow()
    config["project"]["confluence"]["strategy_pages"]["urls"] = [
        "https://workflow.atlassian.net/wiki/spaces/PRODUCT/overview"
    ]
    _write_parameters(tmp_path / "business_value_parameters.yaml")
    project = load_project_config(config, tmp_path / "WORKFLOW.md")
    monkeypatch.setattr(config_module.settings, "CONFLUENCE_USER_EMAIL", "")
    monkeypatch.setattr(config_module.settings, "CONFLUENCE_API_TOKEN", "")

    with pytest.raises(
        ProjectCredentialValidationError,
        match="Set CONFLUENCE_USER_EMAIL, CONFLUENCE_API_TOKEN",
    ):
        validate_project_credentials(project)


def test_empty_strategy_pages_do_not_require_confluence_credentials(monkeypatch, tmp_path):
    _write_parameters(tmp_path / "business_value_parameters.yaml")
    project = load_project_config(_workflow(), tmp_path / "WORKFLOW.md")
    monkeypatch.setattr(config_module.settings, "CONFLUENCE_USER_EMAIL", "")
    monkeypatch.setattr(config_module.settings, "CONFLUENCE_API_TOKEN", "")

    validate_project_credentials(project)
