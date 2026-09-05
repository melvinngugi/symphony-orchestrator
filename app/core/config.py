import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from pydantic_settings import BaseSettings
from dotenv import load_dotenv
import yaml
from typing import Dict, Any
from app.models.agent_config import AgentsRegistry

load_dotenv(override=True)

class Settings(BaseSettings):
    # App Settings
    PROJECT_NAME: str = "Symphony Jira-Bitbucket Orchestrator"
    SERVER_PORT: int = int(os.getenv("SERVER_PORT", 8000))
    CODEX_USAGE_POLL_SECONDS: float = float(os.getenv("CODEX_USAGE_POLL_SECONDS", 60))
    CODEX_USAGE_STALE_SECONDS: float = float(os.getenv("CODEX_USAGE_STALE_SECONDS", 180))
    AGENT_EXECUTION_TIMEOUT_SECONDS: float = float(os.getenv("AGENT_EXECUTION_TIMEOUT_SECONDS", 3600))
    AGENT_TERMINATION_GRACE_SECONDS: float = float(os.getenv("AGENT_TERMINATION_GRACE_SECONDS", 10))
    HTTP_CONNECT_TIMEOUT_SECONDS: float = float(os.getenv("HTTP_CONNECT_TIMEOUT_SECONDS", 10))
    HTTP_READ_TIMEOUT_SECONDS: float = float(os.getenv("HTTP_READ_TIMEOUT_SECONDS", 60))
    GIT_COMMAND_TIMEOUT_SECONDS: float = float(os.getenv("GIT_COMMAND_TIMEOUT_SECONDS", 300))
    
    # Jira Settings
    JIRA_HOST: str = os.getenv("JIRA_HOST", "")
    JIRA_USER_EMAIL: str = os.getenv("JIRA_USER_EMAIL", "")
    JIRA_API_TOKEN: str = os.getenv("JIRA_API_TOKEN", "")
    JIRA_PROJECT_KEY: str = os.getenv("JIRA_PROJECT_KEY", "")

    # Confluence Settings (read-only strategy context)
    CONFLUENCE_HOST: str = os.getenv("CONFLUENCE_HOST", "")
    CONFLUENCE_USER_EMAIL: str = os.getenv("CONFLUENCE_USER_EMAIL", "")
    CONFLUENCE_API_TOKEN: str = os.getenv("CONFLUENCE_API_TOKEN", "")
    
    # Bitbucket Settings
    BITBUCKET_WORKSPACE: str = os.getenv("BITBUCKET_WORKSPACE", "")
    BITBUCKET_REPO_SLUG: str = os.getenv("BITBUCKET_REPO_SLUG", "")
    BITBUCKET_USER_EMAIL: str = os.getenv("BITBUCKET_USER_EMAIL", "")
    BITBUCKET_API_TOKEN: str = os.getenv("BITBUCKET_API_TOKEN", "")
    
    # LLM Settings (Groq Pilot)
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    
    # Workspace Config
    WORKSPACE_ROOT: str = os.getenv("WORKSPACE_ROOT", "./workspaces")

    def validate_jira(self):
        if not all([self.JIRA_HOST, self.JIRA_USER_EMAIL, self.JIRA_API_TOKEN, self.JIRA_PROJECT_KEY]):
            raise ValueError("Missing one or more critical JIRA configuration variables in .env")

    def validate_confluence(self):
        if not all([
            self.CONFLUENCE_HOST,
            self.CONFLUENCE_USER_EMAIL,
            self.CONFLUENCE_API_TOKEN,
        ]):
            raise ValueError(
                "Missing one or more Confluence configuration variables: "
                "CONFLUENCE_HOST, CONFLUENCE_USER_EMAIL, CONFLUENCE_API_TOKEN"
            )

    def validate_bitbucket(self):
        if not all([self.BITBUCKET_WORKSPACE, self.BITBUCKET_REPO_SLUG, self.BITBUCKET_USER_EMAIL, self.BITBUCKET_API_TOKEN]):
            raise ValueError("Missing one or more critical Bitbucket configuration variables in .env")

settings = Settings()


class WorkflowConfigLoadError(ValueError):
    """Raised when a workflow definition cannot be read or parsed."""

    def __init__(self, file_path: str, reason: str):
        self.file_path = file_path
        self.reason = reason
        super().__init__(f"Unable to load workflow '{file_path}': {reason}")


@dataclass(frozen=True)
class JiraFieldsConfig:
    business_value_score: str
    business_value_rationale: str
    epic: str = ""


@dataclass(frozen=True)
class JiraBacklogConfig:
    jql: str
    ignore_label: str


@dataclass(frozen=True)
class JiraProjectConfig:
    host: str
    key: str
    fields: JiraFieldsConfig
    backlog: JiraBacklogConfig


@dataclass(frozen=True)
class BitbucketProjectConfig:
    workspace: str
    repository: str


@dataclass(frozen=True)
class StrategyPagesConfig:
    titles: tuple[str, ...]
    urls: tuple[str, ...]
    space_keys: tuple[str, ...]
    fail_on_missing: bool


@dataclass(frozen=True)
class ConfluenceProjectConfig:
    host: str
    strategy_pages: StrategyPagesConfig


@dataclass(frozen=True)
class BusinessValueParameters:
    scoring_weights: dict[str, float]


@dataclass(frozen=True)
class ProjectConfig:
    jira: JiraProjectConfig
    bitbucket: BitbucketProjectConfig
    confluence: ConfluenceProjectConfig
    business_value_parameters: BusinessValueParameters


class ProjectConfigLoadError(ValueError):
    """Raised when project configuration is absent or invalid."""


class ProjectCredentialValidationError(ValueError):
    """Raised when a configured project integration lacks credentials."""


def validate_project_credentials(project: ProjectConfig) -> None:
    """Validate credentials needed by the integrations enabled for this project."""
    strategy = project.confluence.strategy_pages
    if not strategy.titles and not strategy.urls:
        return

    missing = [
        name
        for name, value in (
            ("CONFLUENCE_USER_EMAIL", settings.CONFLUENCE_USER_EMAIL),
            ("CONFLUENCE_API_TOKEN", settings.CONFLUENCE_API_TOKEN),
        )
        if not value.strip()
    ]
    if missing:
        raise ProjectCredentialValidationError(
            "Confluence strategy pages are configured, but required credentials "
            f"are missing. Set {', '.join(missing)} in the environment."
        )


def _mapping(value: object, path: str, *, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectConfigLoadError(f"{path}: must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ProjectConfigLoadError(f"{path}: field names must be strings")
    extras = sorted(set(value) - allowed)
    if extras:
        raise ProjectConfigLoadError(
            f"{path}.{extras[0]}: unknown configuration field"
        )
    return value


def _configured_or_env(value: object, environment_name: str, path: str) -> str:
    if value is not None and not isinstance(value, str):
        raise ProjectConfigLoadError(f"{path}: must be a string")
    configured = value.strip() if isinstance(value, str) else ""
    resolved = configured or os.getenv(environment_name, "").strip()
    if not resolved:
        raise ProjectConfigLoadError(
            f"{path}: must be a non-empty string or {environment_name} must be set"
        )
    return resolved


def _string(value: object, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise ProjectConfigLoadError(f"{path}: must be {qualifier}")
    return value.strip()


def _string_list(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ProjectConfigLoadError(f"{path}: must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _jira_field(value: object, path: str, *, allow_empty: bool = False) -> str:
    field = _string(value, path, allow_empty=allow_empty)
    if field and re.fullmatch(r"customfield_\d+", field) is None:
        raise ProjectConfigLoadError(f"{path}: must be a Jira customfield_<id>")
    return field


def _load_business_value_parameters(path: Path) -> BusinessValueParameters:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProjectConfigLoadError(
            f"project.business_value_parameters: unable to load '{path}': {exc}"
        ) from exc
    root = _mapping(payload, "business_value_parameters", allowed={"scoring_weights"})
    weights = _mapping(
        root.get("scoring_weights"),
        "business_value_parameters.scoring_weights",
        allowed={
            "customerImpact",
            "revenueOrCostImpact",
            "strategicAlignment",
            "riskReduction",
        },
    )
    expected = {
        "customerImpact",
        "revenueOrCostImpact",
        "strategicAlignment",
        "riskReduction",
    }
    if set(weights) != expected:
        raise ProjectConfigLoadError(
            "business_value_parameters.scoring_weights: must define the four scoring dimensions"
        )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in weights.values()
    ) or abs(sum(float(value) for value in weights.values()) - 1.0) > 0.000001:
        raise ProjectConfigLoadError(
            "business_value_parameters.scoring_weights: values must be non-negative and sum to 1"
        )
    return BusinessValueParameters(
        scoring_weights={name: float(value) for name, value in weights.items()}
    )


def load_project_config(config: dict[str, Any], workflow_path: str | Path) -> ProjectConfig:
    """Load strict project configuration and resolve environment fallbacks."""
    project = _mapping(
        config.get("project"),
        "project",
        allowed={"jira", "bitbucket", "confluence", "business_value_parameters"},
    )
    jira = _mapping(
        project.get("jira"),
        "project.jira",
        allowed={"host", "key", "fields", "backlog"},
    )
    fields = _mapping(
        jira.get("fields"),
        "project.jira.fields",
        allowed={"business_value_score", "business_value_rationale", "epic"},
    )
    backlog = _mapping(
        jira.get("backlog"),
        "project.jira.backlog",
        allowed={"jql", "ignore_label"},
    )
    bitbucket = _mapping(
        project.get("bitbucket"),
        "project.bitbucket",
        allowed={"workspace", "repository"},
    )
    confluence = _mapping(
        project.get("confluence"),
        "project.confluence",
        allowed={"host", "strategy_pages"},
    )
    strategy = _mapping(
        confluence.get("strategy_pages"),
        "project.confluence.strategy_pages",
        allowed={"titles", "urls", "space_keys", "fail_on_missing"},
    )

    titles = _string_list(strategy.get("titles", []), "project.confluence.strategy_pages.titles")
    urls = _string_list(strategy.get("urls", []), "project.confluence.strategy_pages.urls")
    space_keys = _string_list(
        strategy.get("space_keys", []), "project.confluence.strategy_pages.space_keys"
    )
    if titles and not space_keys:
        raise ProjectConfigLoadError(
            "project.confluence.strategy_pages.space_keys: must contain at least one space key when titles are configured"
        )
    fail_on_missing = strategy.get("fail_on_missing", True)
    if not isinstance(fail_on_missing, bool):
        raise ProjectConfigLoadError(
            "project.confluence.strategy_pages.fail_on_missing: must be a boolean"
        )

    parameter_reference = _string(
        project.get("business_value_parameters"),
        "project.business_value_parameters",
    )
    parameter_path = Path(parameter_reference).expanduser()
    if not parameter_path.is_absolute():
        parameter_path = Path(workflow_path).resolve().parent / parameter_path

    return ProjectConfig(
        jira=JiraProjectConfig(
            host=_configured_or_env(jira.get("host"), "JIRA_HOST", "project.jira.host"),
            key=_configured_or_env(jira.get("key"), "JIRA_PROJECT_KEY", "project.jira.key"),
            fields=JiraFieldsConfig(
                business_value_score=_jira_field(
                    fields.get("business_value_score"),
                    "project.jira.fields.business_value_score",
                ),
                business_value_rationale=_jira_field(
                    fields.get("business_value_rationale"),
                    "project.jira.fields.business_value_rationale",
                ),
                epic=_jira_field(
                    fields.get("epic", ""),
                    "project.jira.fields.epic",
                    allow_empty=True,
                ),
            ),
            backlog=JiraBacklogConfig(
                jql=_string(backlog.get("jql", ""), "project.jira.backlog.jql", allow_empty=True),
                ignore_label=_string(
                    backlog.get("ignore_label", "backlog-curation-ignore"),
                    "project.jira.backlog.ignore_label",
                ),
            ),
        ),
        bitbucket=BitbucketProjectConfig(
            workspace=_configured_or_env(
                bitbucket.get("workspace"), "BITBUCKET_WORKSPACE", "project.bitbucket.workspace"
            ),
            repository=_configured_or_env(
                bitbucket.get("repository"), "BITBUCKET_REPO_SLUG", "project.bitbucket.repository"
            ),
        ),
        confluence=ConfluenceProjectConfig(
            host=_configured_or_env(
                confluence.get("host"), "CONFLUENCE_HOST", "project.confluence.host"
            ),
            strategy_pages=StrategyPagesConfig(
                titles=titles,
                urls=urls,
                space_keys=space_keys,
                fail_on_missing=fail_on_missing,
            ),
        ),
        business_value_parameters=_load_business_value_parameters(parameter_path),
    )


def load_agents_config(file_path: str = "agents.yaml") -> AgentsRegistry:
    """Loads agent definitions from agents.yaml and validates against Pydantic model."""
    if not os.path.exists(file_path):
        return AgentsRegistry(agents={})
    with open(file_path, 'r') as f:
        data = yaml.safe_load(f) or {}
        registry = AgentsRegistry.model_validate(data)

    for agent_name, agent_config in registry.agents.items():
        if agent_config.structured and not any(
            "{structured}" in argument for argument in agent_config.args
        ):
            raise ValueError(
                f"Structured agent '{agent_name}' must include the "
                "'{structured}' placeholder in its args"
            )

    return registry


def load_config(file_path: str = "WORKFLOW.md") -> Dict[str, Any]:
    """Parses YAML configuration from the workflow definition."""
    try:
        with open(file_path, 'r') as f:
            content = f.read()
    except (OSError, UnicodeError) as exc:
        raise WorkflowConfigLoadError(file_path, str(exc)) from exc

    if not content.startswith("---"):
        raise WorkflowConfigLoadError(
            file_path,
            "expected YAML front matter starting with '---'",
        )

    parts = content.split("---", 2)
    if len(parts) < 3:
        raise WorkflowConfigLoadError(
            file_path,
            "YAML front matter is missing its closing '---' delimiter",
        )

    try:
        config = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise WorkflowConfigLoadError(file_path, f"invalid YAML: {exc}") from exc

    if config is None:
        raise WorkflowConfigLoadError(
            file_path,
            "YAML front matter must not be empty",
        )

    if not isinstance(config, dict):
        raise WorkflowConfigLoadError(
            file_path,
            "YAML front matter must contain a mapping",
        )

    return config
