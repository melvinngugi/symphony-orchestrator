from contextlib import ExitStack
import mimetypes
import os
import requests
from requests.auth import HTTPBasicAuth
from typing import List, Dict, Any, Optional
from urllib.parse import quote
from app.core.config import settings
from app.core.workflow_validation import (
    WorkflowValidationError,
    collect_workflow_state_references,
)
from app.services.actions import ActionRegistry, PhaseResult

class JiraClient:
    def __init__(self):
        settings.validate_jira()
        self.auth = HTTPBasicAuth(settings.JIRA_USER_EMAIL, settings.JIRA_API_TOKEN)
        self.headers = {"Accept": "application/json"}
        self.base_url = settings.JIRA_HOST.rstrip("/")

    def register_actions(self, registry: ActionRegistry) -> None:
        """Register Jira-owned transition actions."""
        registry.register("jira:attach_outputs", self.attach_outputs)

    def attach_outputs(self, phase_result: PhaseResult) -> None:
        """Attach every normalized phase output to its Jira issue."""
        output_names = phase_result.execution.files or []
        if not output_names:
            return

        issue_identifier = phase_result.issue.get("identifier")
        if not isinstance(issue_identifier, str) or not issue_identifier.strip():
            raise ValueError("Cannot attach outputs for an issue without an identifier")

        attachments = [
            (name, self._resolve_output_path(phase_result.workspace_path, name))
            for name in output_names
        ]
        url = (
            f"{self.base_url}/rest/api/3/issue/"
            f"{quote(issue_identifier.strip(), safe='')}/attachments"
        )
        headers = {
            "Accept": "application/json",
            "X-Atlassian-Token": "no-check",
        }

        with ExitStack() as stack:
            files = []
            for name, path in attachments:
                file_handle = stack.enter_context(open(path, "rb"))
                content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                files.append(("file", (name, file_handle, content_type)))

            try:
                response = requests.post(
                    url,
                    headers=headers,
                    auth=self.auth,
                    files=files,
                )
            except requests.RequestException as exc:
                raise RuntimeError(f"Jira attachment request failed: {exc}") from exc

            if response.status_code != 200:
                raise RuntimeError(
                    f"Jira attachment request failed ({response.status_code}): {response.text}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError("Jira attachment response is not valid JSON") from exc
            if not isinstance(payload, list) or not payload or not all(
                isinstance(item, dict) for item in payload
            ):
                raise ValueError("Jira attachment response must be a non-empty attachment array")

    @staticmethod
    def _resolve_output_path(workspace_path: str, output_name: object) -> str:
        if not isinstance(output_name, str) or not output_name.strip():
            raise ValueError("Phase output file name must be a non-empty string")

        workspace_root = os.path.realpath(workspace_path)
        output_path = os.path.realpath(os.path.join(workspace_root, output_name))
        try:
            contained = os.path.commonpath([workspace_root, output_path]) == workspace_root
        except ValueError:
            contained = False
        if not contained or output_path == workspace_root:
            raise ValueError(f"Phase output path escapes workspace root: {output_name}")
        if not os.path.exists(output_path):
            raise FileNotFoundError(f"Phase output file not found: {output_path}")
        if not os.path.isfile(output_path):
            raise ValueError(f"Phase output path is not a file: {output_path}")
        if not os.access(output_path, os.R_OK):
            raise PermissionError(f"Phase output file is not readable: {output_path}")
        return output_path

    def fetch_project_status_names(self) -> set[str]:
        """Return case-insensitively deduplicated statuses for the configured project."""
        project_key = quote(settings.JIRA_PROJECT_KEY, safe="")
        url = f"{self.base_url}/rest/api/3/project/{project_key}/statuses"
        try:
            response = requests.get(url, headers=self.headers, auth=self.auth)
        except requests.RequestException as exc:
            raise RuntimeError(f"Jira project status request failed: {exc}") from exc

        if response.status_code != 200:
            raise RuntimeError(
                f"Jira project status request failed ({response.status_code}): {response.text}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Jira project status response is not valid JSON") from exc
        if not isinstance(payload, list):
            raise ValueError("Jira project status response must be an array")

        normalized_names: dict[str, str] = {}
        for issue_type_index, issue_type in enumerate(payload):
            if not isinstance(issue_type, dict):
                raise ValueError(
                    f"Jira project status response issue type [{issue_type_index}] must be an object"
                )
            statuses = issue_type.get("statuses")
            if not isinstance(statuses, list):
                raise ValueError(
                    f"Jira project status response issue type [{issue_type_index}].statuses must be an array"
                )
            for status_index, status in enumerate(statuses):
                if not isinstance(status, dict):
                    raise ValueError(
                        "Jira project status response status "
                        f"[{issue_type_index}][{status_index}] must be an object"
                    )
                name = status.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(
                        "Jira project status response status "
                        f"[{issue_type_index}][{status_index}].name must be a non-empty string"
                    )
                clean_name = name.strip()
                normalized_names.setdefault(clean_name.casefold(), clean_name)

        if not normalized_names:
            raise ValueError("Jira project status response contains no statuses")
        return set(normalized_names.values())

    def validate_workflow_states(self, config: dict[str, Any]) -> None:
        """Validate configured workflow states against this Jira project."""
        try:
            valid_states = self.fetch_project_status_names()
        except Exception as exc:
            raise WorkflowValidationError(
                [f"jira.project_statuses: failed to load Jira project statuses: {exc}"]
            ) from exc

        valid_names = {state.strip().casefold() for state in valid_states if state.strip()}
        errors = [
            f"{reference.path}: unknown Jira state '{reference.name}'"
            for reference in collect_workflow_state_references(config)
            if reference.name.casefold() not in valid_names
        ]
        if errors:
            raise WorkflowValidationError(errors)

    def _normalize_issue(self, jira_issue: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transforms raw Jira API responses into the stable, normalized 
        Symphony Core Domain Model.
        """
        fields = jira_issue.get("fields", {})
        
        # Ensure labels are safely extracted whether they are strings or objects
        raw_labels = fields.get("labels", [])
        labels = [str(label).strip().lower() for label in raw_labels]
        
        # Jira API v3 returns the key directly on the issue object, not inside fields
        issue_key = jira_issue.get("key")
        
        return {
            "id": jira_issue.get("id"),
            "identifier": issue_key,  # e.g., "DFLW-38"
            "title": fields.get("summary"),
            "description": fields.get("description"),
            "priority": int(fields.get("priority", {}).get("id", 3)) if fields.get("priority") else None,
            "state": fields.get("status", {}).get("name", "Unknown").lower(),
            "branch_name": f"symphony/{issue_key}",
            "url": f"{self.base_url}/browse/{issue_key}",
            "labels": labels,
            "blocked_by": [],
            "created_at": fields.get("created"),
            "updated_at": fields.get("updated")
        }

    def fetch_candidate_issues(self, active_states: List[str]) -> List[Dict[str, Any]]:
        """Return issues in the phase-configured states using the modern /jql endpoint."""
        # Escape statuses with single quotes
        states_jql = ", ".join([f"'{state}'" for state in active_states])
        jql = f"project = '{settings.JIRA_PROJECT_KEY}' AND status IN ({states_jql}) ORDER BY priority ASC, created ASC"
        
        # Appended /jql to migrate to the mandatory Atlassian endpoint
        url = f"{self.base_url}/rest/api/3/search/jql"
        params = {
            "jql": jql, 
            "maxResults": 50,
            "fields": "summary,description,priority,status,labels,created,updated"
        }
        
        response = requests.get(url, headers=self.headers, auth=self.auth, params=params)
        if response.status_code != 200:
            print(f"Jira API Candidate Fetch Error: {response.status_code} - {response.text}")
            return []
            
        issues = response.json().get("issues", [])
        return [self._normalize_issue(i) for i in issues]

    def fetch_issue_states_by_ids(self, issue_ids: List[str]) -> List[Dict[str, Any]]:
        """Spec 11.1.3: Active-run reconciliation loops utilizing the /jql endpoint"""
        if not issue_ids:
            return []
            
        ids_jql = ", ".join([f"'{id_}'" for id_ in issue_ids])
        jql = f"id IN ({ids_jql})"
        
        # Appended /jql to prevent 410 Gone errors
        url = f"{self.base_url}/rest/api/3/search/jql"
        params = {"jql": jql, "fields": "status,summary,description,priority,labels,created,updated"}
        
        response = requests.get(url, headers=self.headers, auth=self.auth, params=params)
        if response.status_code != 200:
            return []
            
        issues = response.json().get("issues", [])
        return [self._normalize_issue(i) for i in issues]

    def transition_issue(self, issue_identifier: str, target_state: str) -> bool:
        """
        Transitions a Jira issue to a target status by looking up its available transitions.
        """
        # Reuse self.headers but ensure Content-Type is added for the upcoming POST request
        headers = self.headers.copy()
        headers["Content-Type"] = "application/json"
        
        # Use self.base_url and self.auth which are already configured in __init__
        transitions_url = f"{self.base_url}/rest/api/3/issue/{issue_identifier}/transitions"
        
        response = requests.get(transitions_url, headers=headers, auth=self.auth)
        if response.status_code != 200:
            print(f"Failed to fetch transitions for {issue_identifier}: {response.text}")
            return False
            
        transitions = response.json().get("transitions", [])
        transition_id = None
        
        # Find the transition ID that matches our target status name (case-insensitive)
        for t in transitions:
            if t.get("to", {}).get("name", "").lower() == target_state.lower():
                transition_id = t.get("id")
                break
                
        if not transition_id:
            available = [t.get("to", {}).get("name") for t in transitions]
            print(f"No valid transition found to '{target_state}' for {issue_identifier}. Available states: {available}")
            return False
            
        # Execute the transition
        payload = {"transition": {"id": transition_id}}
        
        print(f"Transitioning {issue_identifier} to '{target_state}' (ID: {transition_id})...")
        post_response = requests.post(transitions_url, headers=headers, auth=self.auth, json=payload)
        
        if post_response.status_code == 204:
            print(f"Successfully updated {issue_identifier} to '{target_state}'!")
            return True
        else:
            print(f"Transition failed: {post_response.status_code} - {post_response.text}")
            return False

    def add_comment(self, issue_identifier: str, body: str) -> bool:
        """
        Adds a comment to a Jira issue.
        """
        headers = self.headers.copy()
        headers["Content-Type"] = "application/json"

        comments_url = f"{self.base_url}/rest/api/3/issue/{issue_identifier}/comment"
        payload = {"body": self._build_adf_comment(body)}

        response = requests.post(comments_url, headers=headers, auth=self.auth, json=payload)
        if response.status_code in (200, 201):
            print(f"Successfully added comment to {issue_identifier}!")
            return True

        print(f"Comment failed: {response.status_code} - {response.text}")
        return False

    def _build_adf_comment(self, body: str) -> Dict[str, Any]:
        """
        Converts plain-text comment content into Atlassian Document Format (ADF).
        """
        lines = body.splitlines()
        content: List[Dict[str, Any]] = []
        bullet_items: List[str] = []

        def flush_bullets():
            nonlocal bullet_items
            if not bullet_items:
                return
            content.append(
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": item},
                                    ],
                                }
                            ],
                        }
                        for item in bullet_items
                    ],
                }
            )
            bullet_items = []

        for raw_line in lines:
            if not raw_line.strip():
                flush_bullets()
                continue

            stripped_line = raw_line.lstrip()
            if stripped_line.startswith("- ") or stripped_line.startswith("* "):
                bullet_text = stripped_line[2:].strip()
                if bullet_text:
                    bullet_items.append(bullet_text)
                continue

            flush_bullets()
            content.append(
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": raw_line},
                    ],
                }
            )

        flush_bullets()

        if not content:
            content = [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": " "}],
                }
            ]

        return {
            "type": "doc",
            "version": 1,
            "content": content,
        }
