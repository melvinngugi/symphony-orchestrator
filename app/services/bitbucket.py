import logging
import json
import os
import re
import subprocess
from pathlib import Path
import requests
from requests.auth import HTTPBasicAuth
from app.core.config import settings
from app.models.workspace import repository_path
from app.services.actions import ActionRegistry, PhaseResult

logger = logging.getLogger("symphony.bitbucket")

class BitbucketService:
    request_timeout = (
        settings.HTTP_CONNECT_TIMEOUT_SECONDS,
        settings.HTTP_READ_TIMEOUT_SECONDS,
    )
    git_timeout = settings.GIT_COMMAND_TIMEOUT_SECONDS
    _REVIEW_MARKER_PATTERN = re.compile(
        r"<!--\s*symphony-review:([^:>]+):([0-9A-Za-z._-]+)\s*-->"
    )

    def __init__(self):
        settings.validate_bitbucket()
        self.workspace = settings.BITBUCKET_WORKSPACE
        self.repo_slug = settings.BITBUCKET_REPO_SLUG
        self.auth = HTTPBasicAuth(settings.BITBUCKET_USER_EMAIL, settings.BITBUCKET_API_TOKEN)
        self.base_url = f"https://api.bitbucket.org/2.0/repositories/{self.workspace}/{self.repo_slug}"
        self.base_workdir = settings.WORKSPACE_ROOT
        self.request_timeout = (
            settings.HTTP_CONNECT_TIMEOUT_SECONDS,
            settings.HTTP_READ_TIMEOUT_SECONDS,
        )
        self.git_timeout = settings.GIT_COMMAND_TIMEOUT_SECONDS
        
        os.makedirs(self.base_workdir, exist_ok=True)

    def _git_auth_env(self) -> dict[str, str]:
        """Build a non-interactive Git environment without exposing credentials in argv."""
        env = os.environ.copy()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": str(Path(__file__).with_name("git_askpass.sh")),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "",
                "SYMPHONY_GIT_USERNAME": "x-bitbucket-api-token-auth",
                "SYMPHONY_GIT_PASSWORD": settings.BITBUCKET_API_TOKEN,
            }
        )
        return env

    def register_actions(self, registry: ActionRegistry) -> None:
        """Register Bitbucket-owned transition actions."""
        registry.register(
            "bitbucket:create-pull-request",
            self.create_pull_request_for_phase,
        )
        registry.register(
            "bitbucket:publish-review-comment",
            self.publish_review_comment_for_phase,
        )

    def _run_git(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        """Run one Git command with the configured upper time bound."""
        return subprocess.run(command, timeout=self.git_timeout, **kwargs)

    def verify_repository(self) -> dict:
        """Verifies access to the target Bitbucket repository."""
        response = requests.get(
            self.base_url,
            auth=self.auth,
            timeout=self.request_timeout,
        )
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

        # Persist the non-secret static username, while askpass supplies the token.
        repo_url = (
            f"https://x-bitbucket-api-token-auth@bitbucket.org/"
            f"{self.workspace}/{self.repo_slug}.git"
        )
        logger.info(f"Cloning {self.repo_slug} into isolated checkout: {checkout_path}")
        
        # Clone the repository
        self._run_git(
            ["git", "clone", repo_url, checkout_path],
            check=True,
            env=self._git_auth_env(),
        )

        # Reuse a previously pushed issue branch when resuming on a fresh host.
        branch_name = f"feature/{identifier.lower()}"
        remote_branch = f"refs/remotes/origin/{branch_name}"
        remote_exists = self._run_git(
            ["git", "show-ref", "--verify", "--quiet", remote_branch],
            cwd=checkout_path,
            check=False,
        )
        if remote_exists.returncode == 0:
            self._run_git(
                ["git", "checkout", "--track", "-b", branch_name, f"origin/{branch_name}"],
                cwd=checkout_path,
                check=True,
            )
        elif remote_exists.returncode == 1:
            self._run_git(["git", "checkout", "-b", branch_name], cwd=checkout_path, check=True)
        else:
            raise subprocess.CalledProcessError(
                remote_exists.returncode,
                ["git", "show-ref", "--verify", "--quiet", remote_branch],
            )
        
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
        
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            auth=self.auth,
            timeout=self.request_timeout,
        )
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
            response = requests.get(
                url,
                params=params,
                auth=self.auth,
                timeout=self.request_timeout,
            )
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

    def fetch_attachment(self, issue_identifier: str, filename: str) -> bytes | None:
        """Restore Bitbucket-owned agent input when a phase starts independently."""
        if filename not in {"pull-request.json", "pull-request-comments.json"}:
            return None

        pull_request = self._find_issue_pull_request(issue_identifier)
        if pull_request is None:
            return None
        if filename == "pull-request-comments.json":
            return self._serialize_pull_request_comments(pull_request)
        return self._serialize_pull_request(pull_request)

    def _find_issue_pull_request(self, issue_identifier: str) -> dict | None:
        source_branch = f"feature/{issue_identifier.lower()}"
        target_branch = self.get_default_branch()
        return self.find_open_pull_request(source_branch, target_branch)

    def list_pull_request_comments(self, pull_request_id: int) -> list[dict]:
        """Return every PR comment while following trusted Bitbucket pagination."""
        comments_url = f"{self.base_url}/pullrequests/{pull_request_id}/comments"
        url: str | None = comments_url
        params: dict | None = {"pagelen": 100}
        visited_urls: set[str] = set()
        comments: list[dict] = []

        while url:
            if url in visited_urls:
                raise ValueError("Bitbucket pull request comments response contains a pagination loop")
            if not url.startswith(comments_url):
                raise ValueError("Bitbucket pull request comments response has an invalid next link")
            visited_urls.add(url)

            response = requests.get(
                url,
                params=params,
                auth=self.auth,
                timeout=self.request_timeout,
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Failed to list pull request comments ({response.status_code}): "
                    f"{response.text}"
                )
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("values"), list):
                raise ValueError("Bitbucket pull request comments response is malformed")
            if not all(isinstance(comment, dict) for comment in payload["values"]):
                raise ValueError("Bitbucket pull request comments response contains a non-object")
            comments.extend(payload["values"])

            next_url = payload.get("next")
            if next_url is not None and not isinstance(next_url, str):
                raise ValueError("Bitbucket pull request comments response has an invalid next link")
            url = next_url
            params = None

        return comments

    def create_pull_request_comment(self, pull_request_id: int, markdown: str) -> dict:
        url = f"{self.base_url}/pullrequests/{pull_request_id}/comments"
        response = requests.post(
            url,
            json={"content": {"raw": markdown}},
            headers={"Content-Type": "application/json"},
            auth=self.auth,
            timeout=self.request_timeout,
        )
        if response.status_code != 201:
            raise RuntimeError(
                f"Failed to create pull request comment ({response.status_code}): "
                f"{response.text}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Bitbucket create pull request comment response must be an object")
        return payload

    def update_pull_request_comment(
        self,
        pull_request_id: int,
        comment_id: int,
        markdown: str,
    ) -> dict:
        url = f"{self.base_url}/pullrequests/{pull_request_id}/comments/{comment_id}"
        response = requests.put(
            url,
            json={"content": {"raw": markdown}},
            headers={"Content-Type": "application/json"},
            auth=self.auth,
            timeout=self.request_timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to update pull request comment ({response.status_code}): "
                f"{response.text}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Bitbucket update pull request comment response must be an object")
        return payload

    def publish_review_comment_for_phase(self, phase_result: PhaseResult) -> None:
        """Publish an idempotent, human-readable review summary on the issue PR."""
        issue_identifier = str(
            phase_result.issue.get("identifier") or phase_result.issue.get("id") or ""
        ).strip()
        if not issue_identifier:
            raise ValueError("Cannot publish a review comment without an issue identifier")

        pull_request = self._find_issue_pull_request(issue_identifier)
        if pull_request is None:
            raise RuntimeError(f"No open pull request found for {issue_identifier}")
        pull_request_id = pull_request.get("id")
        if not isinstance(pull_request_id, int) or isinstance(pull_request_id, bool):
            raise ValueError("Bitbucket pull request is missing an integer id")

        commit_hash = self._pull_request_commit_hash(
            pull_request,
            phase_result.repository_path,
        )
        marker = f"<!-- symphony-review:{issue_identifier}:{commit_hash} -->"
        markdown = self._format_review_comment(phase_result, marker)

        for comment in self.list_pull_request_comments(pull_request_id):
            content = comment.get("content")
            raw = content.get("raw") if isinstance(content, dict) else None
            if not isinstance(raw, str) or marker not in raw:
                continue
            comment_id = comment.get("id")
            if not isinstance(comment_id, int) or isinstance(comment_id, bool):
                raise ValueError("Bitbucket pull request comment is missing an integer id")
            self.update_pull_request_comment(pull_request_id, comment_id, markdown)
            logger.info(
                "Updated Symphony review comment %s on pull request %s",
                comment_id,
                pull_request_id,
            )
            return

        comment = self.create_pull_request_comment(pull_request_id, markdown)
        logger.info(
            "Created Symphony review comment %s on pull request %s",
            comment.get("id", "unknown"),
            pull_request_id,
        )

    def _pull_request_commit_hash(self, pull_request: dict, checkout_path: str) -> str:
        source = pull_request.get("source")
        commit = source.get("commit") if isinstance(source, dict) else None
        commit_hash = commit.get("hash") if isinstance(commit, dict) else None
        if isinstance(commit_hash, str) and commit_hash.strip():
            return commit_hash.strip()

        result = self._run_git(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout_path,
            check=True,
            capture_output=True,
            text=True,
        )
        commit_hash = result.stdout.strip()
        if not commit_hash:
            raise ValueError("Cannot determine the reviewed commit hash")
        return commit_hash

    @staticmethod
    def _format_review_comment(phase_result: PhaseResult, marker: str) -> str:
        blocked = phase_result.execution.status == "blocked"
        heading = "Changes requested" if blocked else "Review passed"
        message = (phase_result.execution.message or "").strip()
        lines = [f"## Symphony automated review — {heading}"]
        if message:
            lines.extend(["", message])

        clarifications = [
            str(item).strip()
            for item in (phase_result.execution.needed_clarifications or [])
            if str(item).strip()
        ]
        if clarifications:
            lines.extend(["", "### Required changes", ""])
            lines.extend(f"- {item}" for item in clarifications)

        lines.extend(["", marker])
        return "\n".join(lines)

    def _serialize_pull_request_comments(self, pull_request: dict) -> bytes:
        pull_request_id = pull_request.get("id")
        if not isinstance(pull_request_id, int) or isinstance(pull_request_id, bool):
            raise ValueError("Bitbucket pull request is missing an integer id")

        source = pull_request.get("source")
        commit = source.get("commit") if isinstance(source, dict) else None
        commit_hash = commit.get("hash") if isinstance(commit, dict) else None
        current_commit = commit_hash.strip() if isinstance(commit_hash, str) else ""
        active_comments = []
        for comment in self.list_pull_request_comments(pull_request_id):
            if comment.get("deleted") is True or comment.get("resolution") is not None:
                continue
            content = comment.get("content")
            raw = content.get("raw") if isinstance(content, dict) else None
            if not isinstance(raw, str) or not raw.strip():
                continue

            marker_match = self._REVIEW_MARKER_PATTERN.search(raw)
            if marker_match and current_commit and marker_match.group(2) != current_commit:
                continue

            user = comment.get("user")
            inline = comment.get("inline")
            active_comments.append(
                {
                    "id": comment.get("id"),
                    "author": user.get("display_name") if isinstance(user, dict) else None,
                    "createdOn": comment.get("created_on"),
                    "content": raw,
                    "inline": inline if isinstance(inline, dict) else None,
                }
            )

        source_branch_data = source.get("branch") if isinstance(source, dict) else None
        destination = pull_request.get("destination")
        destination_branch_data = (
            destination.get("branch") if isinstance(destination, dict) else None
        )
        payload = {
            "pullRequest": {
                "id": pull_request_id,
                "title": pull_request.get("title"),
                "sourceBranch": (
                    source_branch_data.get("name")
                    if isinstance(source_branch_data, dict)
                    else None
                ),
                "destinationBranch": (
                    destination_branch_data.get("name")
                    if isinstance(destination_branch_data, dict)
                    else None
                ),
                "sourceCommit": current_commit or None,
            },
            "activeComments": active_comments,
        }
        return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def _serialize_pull_request(self, pull_request: dict) -> bytes:
        if not isinstance(pull_request, dict):
            raise TypeError("Bitbucket pull request must be an object")
        return (json.dumps(pull_request, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def _write_pull_request_input(self, workspace_path: str, pull_request: dict) -> None:
        output_path = os.path.join(workspace_path, "pull-request.json")
        temporary_path = f"{output_path}.tmp"
        with open(temporary_path, "wb") as output_file:
            output_file.write(self._serialize_pull_request(pull_request))
        os.replace(temporary_path, output_path)
        logger.info("Wrote pull request input to %s", output_path)

    def create_pull_request_for_phase(self, phase_result: PhaseResult) -> None:
        """Commit and push issue changes, then create or reuse a Bitbucket PR."""
        workspace_path = phase_result.workspace_path
        issue = phase_result.issue
        checkout_path = repository_path(workspace_path)
        issue_key = str(issue.get("identifier") or issue.get("id") or "issue").strip()
        issue_title = str(issue.get("title") or "Automated changes").strip()
        pull_request_title = f"{issue_key}: {issue_title}"

        self._run_git(["git", "add", "--all"], cwd=checkout_path, check=True)
        staged = self._run_git(
            ["git", "diff", "--cached", "--quiet"],
            cwd=checkout_path,
            check=False,
        )
        if staged.returncode == 1:
            author_email = settings.BITBUCKET_USER_EMAIL or "symphony@localhost"
            self._run_git(
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

        branch_result = self._run_git(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=checkout_path,
            check=True,
            capture_output=True,
            text=True,
        )
        source_branch = branch_result.stdout.strip()
        if not source_branch or source_branch == "HEAD":
            raise RuntimeError("Cannot create a pull request from a detached HEAD")

        self._run_git(
            ["git", "push", "--set-upstream", "origin", source_branch],
            cwd=checkout_path,
            check=True,
            env=self._git_auth_env(),
        )

        target_branch = self.get_default_branch()
        pull_request = self.find_open_pull_request(source_branch, target_branch)
        if pull_request is None:
            issue_url = issue.get("url")
            description = f"Automated pull request for {issue_url}" if issue_url else ""
            pull_request = self.create_pull_request(
                title=pull_request_title,
                source_branch=source_branch,
                target_branch=target_branch,
                description=description,
            )

        self._write_pull_request_input(workspace_path, pull_request)
