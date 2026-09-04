import os
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
