import requests
from requests.auth import HTTPBasicAuth
from app.core.config import settings

class BitbucketService:
    def __init__(self):
        settings.validate_bitbucket()
        self.workspace = settings.BITBUCKET_WORKSPACE
        self.repo_slug = settings.BITBUCKET_REPO_SLUG
        self.auth = HTTPBasicAuth(settings.BITBUCKET_USER_EMAIL, settings.BITBUCKET_API_TOKEN)
        self.base_url = f"https://api.bitbucket.org/2.0/repositories/{self.workspace}/{self.repo_slug}"

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