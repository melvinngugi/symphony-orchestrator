import os
import subprocess
import logging
import urllib.parse
import requests
from requests.auth import HTTPBasicAuth
from app.core.config import settings

logger = logging.getLogger("symphony.bitbucket")

class BitbucketService:
    def __init__(self):
        settings.validate_bitbucket()
        self.workspace = settings.BITBUCKET_WORKSPACE
        self.repo_slug = settings.BITBUCKET_REPO_SLUG
        self.auth = HTTPBasicAuth(settings.BITBUCKET_USER_EMAIL, settings.BITBUCKET_API_TOKEN)
        self.base_url = f"https://api.bitbucket.org/2.0/repositories/{self.workspace}/{self.repo_slug}"
        self.base_workdir = "/tmp/symphony_workspaces"
        
        os.makedirs(self.base_workdir, exist_ok=True)

    def verify_repository(self) -> dict:
        """Verifies access to the target Bitbucket repository."""
        response = requests.get(self.base_url, auth=self.auth)
        if response.status_code != 200:
            raise Exception(f"Bitbucket Connection Error ({response.status_code}): {response.text}")
        return response.json()

    def get_default_branch(self) -> str:
        """Retrieves the default branch name (main or master)."""
        repo_info = self.verify_repository()
        mainbranch = repo_info.get("mainbranch", {})
        return mainbranch.get("name", "main")

    def prepare_workspace(self, identifier: str) -> str:
        """
        Prepares an isolated workspace directory, clones the Bitbucket repository 
        using a scoped API token, and checks out a dedicated feature branch.
        """
        workspace_path = os.path.join(self.base_workdir, identifier)
        
        # Clean up existing workspace if it lingers from a past run
        if os.path.exists(workspace_path):
            subprocess.run(["rm", "-rf", workspace_path], check=True)

        # Safely URL-encode the scoped API Token
        safe_token = urllib.parse.quote(settings.BITBUCKET_API_TOKEN)

        # Bitbucket's standard static username for API token authentication in Git commands
        repo_url = f"https://x-bitbucket-api-token-auth:{safe_token}@bitbucket.org/{self.workspace}/{self.repo_slug}.git"

        logger.info(f"Cloning {self.repo_slug} into isolated workspace: {workspace_path}")
        
        # Clone the repository
        subprocess.run(["git", "clone", repo_url, workspace_path], check=True)

        # Create and checkout a dedicated feature branch for the Jira ticket
        branch_name = f"feature/{identifier.lower()}"
        subprocess.run(["git", "checkout", "-b", branch_name], cwd=workspace_path, check=True)
        
        logger.info(f"Successfully checked out branch {branch_name} for {identifier}")
        return workspace_path

    def create_pull_request(self, title: str, source_branch: str, target_branch: str = None, description: str = "") -> dict:
        """Creates a Pull Request from a feature branch to target branch."""
        if not target_branch:
            target_branch = self.get_default_branch()

        url = f"{self.base_url}/pullrequests"
        payload = {
            "title": title,
            "description": description,
            "source": {"branch": {"name": source_branch}},
            "destination": {"branch": {"name": target_branch}}
        }
        headers = {"Content-Type": "application/json"}
        
        response = requests.post(url, json=payload, headers=headers, auth=self.auth)
        if response.status_code not in (200, 201):
            raise Exception(f"Failed to create PR ({response.status_code}): {response.text}")
        return response.json()

bitbucket_service = BitbucketService()