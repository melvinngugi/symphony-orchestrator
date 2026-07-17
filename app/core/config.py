import os
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseSettings):
    # App Settings
    PROJECT_NAME: str = "Symphony Jira-Bitbucket Orchestrator"
    SERVER_PORT: int = int(os.getenv("SERVER_PORT", 8000))
    
    # Jira Settings
    JIRA_HOST: str = os.getenv("JIRA_HOST", "")
    JIRA_USER_EMAIL: str = os.getenv("JIRA_USER_EMAIL", "")
    JIRA_API_TOKEN: str = os.getenv("JIRA_API_TOKEN", "")
    JIRA_PROJECT_KEY: str = os.getenv("JIRA_PROJECT_KEY", "")
    
    # Bitbucket Settings
    BITBUCKET_WORKSPACE: str = os.getenv("BITBUCKET_WORKSPACE", "")
    BITBUCKET_REPO_SLUG: str = os.getenv("BITBUCKET_REPO_SLUG", "")
    BITBUCKET_USER: str = os.getenv("BITBUCKET_USER", "")
    BITBUCKET_APP_PASSWORD: str = os.getenv("BITBUCKET_APP_PASSWORD", "")
    
    # Workspace Config
    WORKSPACE_ROOT: str = os.getenv("WORKSPACE_ROOT", "./symphony_workspaces")

    def validate_jira(self):
        if not all([self.JIRA_HOST, self.JIRA_USER_EMAIL, self.JIRA_API_TOKEN, self.JIRA_PROJECT_KEY]):
            raise ValueError("Missing one or more critical JIRA configuration variables in .env")

settings = Settings()
