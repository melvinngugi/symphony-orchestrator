import os
import subprocess
import logging
import urllib.parse
import requests
from requests.auth import HTTPBasicAuth
from app.core.config import settings
from app.models.workspace import repository_path
from app.services.actions import ActionRegistry, PhaseResult

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

    def register_actions(self, registry: ActionRegistry) -> None:
        """Register Bitbucket-owned transition actions."""
        registry.register(
            "bitbucket:create-pull-request",
            self.create_pull_request_for_phase,
        )

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
        Prepares an issue workspace, clones the repository into its repository/
        child, and checks out a dedicated feature branch.
        """
        workspace_path = os.path.join(self.base_workdir, identifier)
        
        # Clean up existing workspace if it lingers from a past run
        if os.path.exists(workspace_path):
            subprocess.run(["rm", "-rf", workspace_path], check=True)

        os.makedirs(workspace_path, exist_ok=True)
        checkout_path = repository_path(workspace_path)

        # Safely URL-encode the scoped API Token
        safe_token = urllib.parse.quote(settings.BITBUCKET_API_TOKEN)

        # Bitbucket's standard static username for API token authentication in Git commands
        repo_url = f"https://x-token-auth:{safe_token}@bitbucket.org/{self.workspace}/{self.repo_slug}.git"

        logger.info(f"Cloning {self.repo_slug} into isolated checkout: {checkout_path}")
        
        # Clone the repository
        subprocess.run(["git", "clone", repo_url, checkout_path], check=True)

        # Create and checkout a dedicated feature branch for the Jira ticket
        branch_name = f"feature/{identifier.lower()}"
        subprocess.run(["git", "checkout", "-b", branch_name], cwd=checkout_path, check=True)
        
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
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Bitbucket create pull request response must be an object")
        return payload

    def find_open_pull_request(self, source_branch: str, target_branch: str) -> dict | None:
        """Return an open PR for the source/destination pair, following pagination."""
        url = f"{self.base_url}/pullrequests"
        pull_requests_url = url
        params = {"state": "OPEN", "pagelen": 50}
        visited_urls: set[str] = set()

        while url:
            if url in visited_urls:
                raise ValueError("Bitbucket pull request list response contains a pagination loop")
            if not url.startswith(pull_requests_url):
                raise ValueError("Bitbucket pull request list response has an invalid next link")
            visited_urls.add(url)
            response = requests.get(url, params=params, auth=self.auth)
            if response.status_code != 200:
                raise RuntimeError(
                    f"Failed to list pull requests ({response.status_code}): {response.text}"
                )

            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("values"), list):
                raise ValueError("Bitbucket pull request list response is malformed")

            for pull_request in payload["values"]:
                if not isinstance(pull_request, dict):
                    continue
                source = pull_request.get("source")
                destination = pull_request.get("destination")
                source_branch_data = source.get("branch") if isinstance(source, dict) else None
                destination_branch_data = destination.get("branch") if isinstance(destination, dict) else None
                source_name = source_branch_data.get("name") if isinstance(source_branch_data, dict) else None
                destination_name = (
                    destination_branch_data.get("name")
                    if isinstance(destination_branch_data, dict)
                    else None
                )
                if source_name == source_branch and destination_name == target_branch:
                    return pull_request

            next_url = payload.get("next")
            if next_url is not None and not isinstance(next_url, str):
                raise ValueError("Bitbucket pull request list response has an invalid next link")
            url = next_url
            params = None

        return None

    def create_pull_request_for_phase(self, phase_result: PhaseResult) -> None:
        """Commit and push issue changes, then create or reuse a Bitbucket PR."""
        workspace_path = phase_result.workspace_path
        issue = phase_result.issue
        checkout_path = repository_path(workspace_path)
        issue_key = str(issue.get("identifier") or issue.get("id") or "issue").strip()
        issue_title = str(issue.get("title") or "Automated changes").strip()
        pull_request_title = f"{issue_key}: {issue_title}"

        subprocess.run(["git", "add", "--all"], cwd=checkout_path, check=True)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=checkout_path,
            check=False,
        )
        if staged.returncode == 1:
            author_email = settings.BITBUCKET_USER_EMAIL or "symphony@localhost"
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Symphony Orchestrator",
                    "-c",
                    f"user.email={author_email}",
                    "commit",
                    "-m",
                    pull_request_title,
                ],
                cwd=checkout_path,
                check=True,
            )
        elif staged.returncode != 0:
            raise subprocess.CalledProcessError(
                staged.returncode,
                ["git", "diff", "--cached", "--quiet"],
            )

        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=checkout_path,
            check=True,
            capture_output=True,
            text=True,
        )
        source_branch = branch_result.stdout.strip()
        if not source_branch or source_branch == "HEAD":
            raise RuntimeError("Cannot create a pull request from a detached HEAD")

        subprocess.run(
            ["git", "push", "--set-upstream", "origin", source_branch],
            cwd=checkout_path,
            check=True,
        )

        target_branch = self.get_default_branch()
        if self.find_open_pull_request(source_branch, target_branch):
            return

        issue_url = issue.get("url")
        description = f"Automated pull request for {issue_url}" if issue_url else ""
        self.create_pull_request(
            title=pull_request_title,
            source_branch=source_branch,
            target_branch=target_branch,
            description=description,
        )
