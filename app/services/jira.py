from contextlib import ExitStack
import hashlib
import json
import mimetypes
import os
import re
import shutil
import requests
from requests.auth import HTTPBasicAuth
from typing import List, Dict, Any, Optional
from urllib.parse import quote
from app.core.config import JiraBacklogConfig, JiraFieldsConfig, JiraProjectConfig, settings
from app.core.workflow_validation import (
    WorkflowStateValidationError,
    WorkflowValidationError,
    collect_workflow_state_references,
)
from app.services.actions import ActionRegistry, PhaseResult
from app.services.backlog import (
    cyclic_dependency_pairs,
    load_and_validate_curation_result,
    load_curation_input,
)

class JiraClient:
    STATUS_SEARCH_PAGE_SIZE = 100

    def __init__(self, project: JiraProjectConfig | None = None):
        if not settings.JIRA_USER_EMAIL or not settings.JIRA_API_TOKEN:
            raise ValueError("Missing Jira user or API token in environment")
        project = project or JiraProjectConfig(
            host=settings.JIRA_HOST,
            key=settings.JIRA_PROJECT_KEY,
            fields=JiraFieldsConfig("", ""),
            backlog=JiraBacklogConfig("", "backlog-curation-ignore"),
        )
        self.project = project
        self.auth = HTTPBasicAuth(settings.JIRA_USER_EMAIL, settings.JIRA_API_TOKEN)
        self.headers = {"Accept": "application/json"}
        self.base_url = project.host.rstrip("/")
        self.request_timeout = (
            settings.HTTP_CONNECT_TIMEOUT_SECONDS,
            settings.HTTP_READ_TIMEOUT_SECONDS,
        )

    def register_actions(self, registry: ActionRegistry) -> None:
        """Register Jira-owned transition actions."""
        registry.register("jira:attach_outputs", self.attach_outputs)
        registry.register("jira:apply-backlog-curation", self.apply_backlog_curation)
        registry.register("jira:attach-curation-outputs", self.attach_curation_outputs)

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
                    timeout=self.request_timeout,
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

    def fetch_backlog_issues(
        self,
        jql: str,
        *,
        score_field: str,
        rationale_field: str,
        epic_field: str | None = None,
        audit_issue: str,
        ignore_label: str,
    ) -> list[dict[str, Any]]:
        """Fetch and normalize every issue in the configured curation scope."""
        effective_jql = jql.strip() if isinstance(jql, str) and jql.strip() else (
            f"project = '{self.project.key}' AND status = 'To Do'"
        )
        fields = [
            "summary",
            "description",
            "issuetype",
            "components",
            "priority",
            "status",
            "labels",
            "issuelinks",
            "parent",
            "created",
            "updated",
            score_field,
            rationale_field,
        ]
        if epic_field:
            fields.append(epic_field)
        raw_issues = self._search_all(effective_jql, fields)
        normalized = [
            self._normalize_curation_issue(
                issue,
                score_field=score_field,
                rationale_field=rationale_field,
                epic_field=epic_field,
            )
            for issue in raw_issues
        ]
        ignored_label = ignore_label.strip().casefold()
        return [
            issue
            for issue in normalized
            if issue["key"].casefold() != audit_issue.strip().casefold()
            and ignored_label not in {label.casefold() for label in issue["labels"]}
        ]

    def apply_backlog_curation(self, phase_result: PhaseResult) -> None:
        """Validate and safely apply a curator-produced Jira change set."""
        from app.services.backlog import (
            cyclic_dependency_pairs,
            load_and_validate_curation_result,
            load_curation_input,
        )

        phase_config = phase_result.phase_config or {}
        jira_config = phase_config.get("jira", {})
        fields = self.project.fields
        confidence = phase_config.get("confidence", {})
        dry_run = bool(phase_config.get("dry_run", True))
        input_path = self._resolve_output_path(
            phase_result.workspace_path,
            phase_result.agent_config.stdin,
        )
        result_path = self._resolve_output_path(
            phase_result.workspace_path,
            "backlog-curation.json",
        )
        input_payload = load_curation_input(input_path)
        result = load_and_validate_curation_result(result_path, input_payload)
        tickets = {ticket["key"]: ticket for ticket in input_payload["tickets"]}
        cyclic_pairs = cyclic_dependency_pairs(result, tickets)
        report: dict[str, Any] = {
            "runId": result.runId,
            "dryRun": dry_run,
            "sourceSnapshotAt": result.sourceSnapshotAt,
            "applied": [],
            "wouldApply": [],
            "reviewRequired": [],
            "skipped": [],
            "warnings": result.warnings,
        }
        fresh_cache: dict[str, dict[str, Any]] = {}
        provenance_cache: dict[str, dict[str, Any]] = {}

        def fresh(issue_key: str) -> dict[str, Any]:
            if issue_key not in fresh_cache:
                fresh_cache[issue_key] = self._fetch_curation_issue(
                    issue_key,
                    fields.business_value_score,
                    fields.business_value_rationale,
                )
            return fresh_cache[issue_key]

        def provenance(issue_key: str) -> dict[str, Any]:
            if issue_key not in provenance_cache:
                provenance_cache[issue_key] = self._get_curator_property(issue_key)
            return provenance_cache[issue_key]

        def stale(issue_key: str) -> bool:
            current_updated = fresh(issue_key).get("updated")
            if current_updated == tickets[issue_key].get("updated"):
                return False
            recorded = provenance(issue_key)
            return not (
                recorded.get("lastRunId") == result.runId
                and recorded.get("lastKnownUpdated") == current_updated
            )

        def record_intent(issue_key: str, operation_hash: str) -> None:
            recorded = provenance(issue_key)
            recorded["operationHashes"] = self._append_operation_hash(
                recorded.get("operationHashes"), operation_hash
            )
            self._put_curator_property(issue_key, recorded)

        def mark_known_update(issue_key: str) -> None:
            fresh_cache.pop(issue_key, None)
            current_updated = fresh(issue_key).get("updated")
            recorded = provenance(issue_key)
            recorded["lastRunId"] = result.runId
            recorded["sourceUpdated"] = tickets[issue_key].get("updated")
            recorded["lastKnownUpdated"] = current_updated
            self._put_curator_property(issue_key, recorded)

        def require_review(issue_keys: list[str], reason: str, operation: dict) -> None:
            report["reviewRequired"].append({"reason": reason, **operation})
            if not dry_run:
                for issue_key in issue_keys:
                    operation_hash = self._operation_hash(
                        result.runId, issue_key, "review", {"reason": reason, **operation}
                    )
                    record_intent(issue_key, operation_hash)
                    self._add_label(issue_key, jira_config["review_label"])
                    mark_known_update(issue_key)

        def record_applied(operation: dict) -> None:
            report["wouldApply" if dry_run else "applied"].append(operation)

        for value in result.ticketValues:
            operation = {"type": "businessValue", "issueKey": value.issueKey}
            if value.confidence < confidence["business_value"]:
                require_review([value.issueKey], "confidence-below-threshold", operation)
                continue
            current = fresh(value.issueKey)
            recorded = provenance(value.issueKey)
            operation_hash = self._operation_hash(
                result.runId,
                value.issueKey,
                "businessValue",
                {"score": value.score, "rationale": value.rationale},
            )
            current_score = current.get("businessValue", {}).get("score")
            current_rationale = current.get("businessValue", {}).get("rationale")
            if (
                operation_hash in recorded.get("operationHashes", [])
                and current_score == value.score
                and current_rationale == value.rationale
            ):
                mark_known_update(value.issueKey)
                report["skipped"].append({"reason": "value-already-applied", **operation})
                continue
            if stale(value.issueKey):
                require_review([value.issueKey], "ticket-changed-since-snapshot", operation)
                continue
            previous = recorded.get("businessValue", {})
            protected = (
                (current_score is not None or current_rationale not in (None, ""))
                and not (
                    previous.get("score") == current_score
                    and previous.get("rationale") == current_rationale
                )
            )
            if protected:
                require_review([value.issueKey], "human-value-protected", operation)
                continue
            if not dry_run:
                recorded["businessValue"] = {
                    "score": value.score,
                    "rationale": value.rationale,
                    "runId": result.runId,
                }
                record_intent(value.issueKey, operation_hash)
                self._update_issue_fields(
                    value.issueKey,
                    {
                        fields.business_value_score: value.score,
                        fields.business_value_rationale: value.rationale,
                    },
                )
                mark_known_update(value.issueKey)
            record_applied(operation | {"score": value.score})

        for dependency in result.dependencies:
            operation = {
                "type": "dependency",
                "blocker": dependency.blocker,
                "blocked": dependency.blocked,
            }
            targets = [dependency.blocker, dependency.blocked]
            if dependency.confidence < confidence["dependency"]:
                require_review(targets, "confidence-below-threshold", operation)
                continue
            if (dependency.blocker, dependency.blocked) in cyclic_pairs:
                require_review(targets, "dependency-cycle", operation)
                continue
            operation_hash = self._operation_hash(
                result.runId,
                dependency.blocker,
                "dependency",
                [dependency.blocker, dependency.blocked],
            )
            if self._has_blocking_link(fresh(dependency.blocker), dependency.blocked):
                if all(operation_hash in provenance(key).get("operationHashes", []) for key in targets):
                    for key in targets:
                        mark_known_update(key)
                report["skipped"].append({"reason": "link-already-exists", **operation})
                continue
            if any(stale(key) for key in targets):
                require_review(targets, "ticket-changed-since-snapshot", operation)
                continue
            if not dry_run:
                for key in targets:
                    record_intent(key, operation_hash)
                self._create_issue_link(
                    dependency.blocker,
                    dependency.blocked,
                    jira_config["dependency_link_type"],
                )
                for key in targets:
                    mark_known_update(key)
            record_applied(operation)

        for clarification in result.clarifications:
            operation = {"type": "clarification", "issueKey": clarification.issueKey}
            if clarification.confidence < confidence["clarification"]:
                require_review([clarification.issueKey], "confidence-below-threshold", operation)
                continue
            questions = [question.strip() for question in clarification.questions]
            operation_hash = self._operation_hash(
                result.runId,
                clarification.issueKey,
                "clarification",
                questions,
            )
            marker = f"[symphony-backlog-curator:{operation_hash}]"
            if self._comment_contains(clarification.issueKey, marker):
                if operation_hash in provenance(clarification.issueKey).get("operationHashes", []):
                    mark_known_update(clarification.issueKey)
                report["skipped"].append({"reason": "clarification-already-posted", **operation})
                continue
            label_already_applied = (
                operation_hash in provenance(clarification.issueKey).get("operationHashes", [])
                and jira_config["clarification_label"] in fresh(clarification.issueKey).get("labels", [])
            )
            if label_already_applied:
                # Recover a prior attempt that added the label but failed before
                # (or while receiving the response from) the comment operation.
                mark_known_update(clarification.issueKey)
            if stale(clarification.issueKey):
                require_review([clarification.issueKey], "ticket-changed-since-snapshot", operation)
                continue
            if not dry_run:
                record_intent(clarification.issueKey, operation_hash)
                if not label_already_applied:
                    self._add_label(clarification.issueKey, jira_config["clarification_label"])
                    mark_known_update(clarification.issueKey)
                body = marker + "\nClarification requested:\n" + "\n".join(
                    f"- {question}" for question in questions
                )
                if not self.add_comment(clarification.issueKey, body):
                    raise RuntimeError(
                        f"Failed to add clarification comment to {clarification.issueKey}"
                    )
                mark_known_update(clarification.issueKey)
            record_applied(operation | {"questions": questions})

        report_name = "backlog-curation-report.json"
        report_path = os.path.join(phase_result.workspace_path, report_name)
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
        if phase_result.execution.files is None:
            phase_result.execution.files = []
        if report_name not in phase_result.execution.files:
            phase_result.execution.files.append(report_name)

    def attach_curation_outputs(self, phase_result: PhaseResult) -> None:
        """Attach run-qualified curator outputs without duplicating retry uploads."""
        issue_key = phase_result.issue.get("identifier")
        run_id = phase_result.issue.get("run_id")
        if not isinstance(issue_key, str) or not issue_key.strip():
            raise ValueError("Cannot attach curator outputs without an audit issue")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("Cannot attach curator outputs without a run id")
        safe_run_id = re.sub(r"[^A-Za-z0-9._-]+", "-", run_id).strip("-")
        existing = self._attachment_names(issue_key)
        for source_name in phase_result.execution.files or []:
            source_path = self._resolve_output_path(phase_result.workspace_path, source_name)
            stem, extension = os.path.splitext(os.path.basename(source_name))
            attachment_name = f"{stem}-{safe_run_id}{extension}"
            if attachment_name in existing:
                continue
            attachment_path = os.path.join(phase_result.workspace_path, attachment_name)
            shutil.copyfile(source_path, attachment_path)
            self._upload_single_attachment(issue_key, attachment_name, attachment_path)
            existing.add(attachment_name)

    def _attachment_names(self, issue_key: str) -> set[str]:
        encoded_key = quote(issue_key.strip(), safe="")
        response = requests.get(
            f"{self.base_url}/rest/api/3/issue/{encoded_key}",
            headers=self.headers,
            auth=self.auth,
            params={"fields": "attachment"},
            timeout=self.request_timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Jira attachment lookup failed for {issue_key} "
                f"({response.status_code}): {response.text}"
            )
        payload = response.json()
        fields = payload.get("fields") if isinstance(payload, dict) else None
        attachments = fields.get("attachment") if isinstance(fields, dict) else None
        if not isinstance(attachments, list):
            raise ValueError("Jira attachment lookup must contain an attachment array")
        return {
            attachment["filename"]
            for attachment in attachments
            if isinstance(attachment, dict) and isinstance(attachment.get("filename"), str)
        }

    def _upload_single_attachment(
        self,
        issue_key: str,
        name: str,
        path: str,
    ) -> None:
        encoded_key = quote(issue_key.strip(), safe="")
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with open(path, "rb") as handle:
            response = requests.post(
                f"{self.base_url}/rest/api/3/issue/{encoded_key}/attachments",
                headers={"Accept": "application/json", "X-Atlassian-Token": "no-check"},
                auth=self.auth,
                files=[("file", (name, handle, content_type))],
                timeout=self.request_timeout,
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"Jira attachment upload failed for {issue_key} "
                f"({response.status_code}): {response.text}"
            )

    def fetch_attachment(self, issue_identifier: str, filename: str) -> bytes | None:
        """Download the newest Jira attachment with the requested filename."""
        return self._fetch_named_attachment(issue_identifier, filename)

    def _fetch_named_attachment(self, issue_identifier: str, filename: str) -> bytes | None:
        """Download the newest Jira attachment with an exact filename match."""
        issue_key = quote(issue_identifier.strip(), safe="")
        issue_url = f"{self.base_url}/rest/api/3/issue/{issue_key}"
        try:
            response = requests.get(
                issue_url,
                headers=self.headers,
                auth=self.auth,
                params={"fields": "attachment"},
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Jira attachment metadata request failed: {exc}") from exc

        if response.status_code != 200:
            raise RuntimeError(
                f"Jira attachment metadata request failed ({response.status_code}): "
                f"{response.text}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Jira attachment metadata response is not valid JSON") from exc
        fields = payload.get("fields") if isinstance(payload, dict) else None
        attachments = fields.get("attachment") if isinstance(fields, dict) else None
        if not isinstance(attachments, list):
            raise ValueError("Jira attachment metadata response must contain an attachment array")

        matching: list[tuple[int, dict[str, Any]]] = [
            (index, attachment)
            for index, attachment in enumerate(attachments)
            if isinstance(attachment, dict) and attachment.get("filename") == filename
        ]
        if not matching:
            return None

        def attachment_order(entry: tuple[int, dict[str, Any]]) -> tuple[str, int, int]:
            index, attachment = entry
            created = attachment.get("created")
            attachment_id = attachment.get("id")
            numeric_id = int(attachment_id) if str(attachment_id).isdigit() else -1
            return created if isinstance(created, str) else "", numeric_id, index

        _, latest = max(matching, key=attachment_order)
        attachment_id = latest.get("id")
        if not isinstance(attachment_id, (str, int)) or not str(attachment_id).strip():
            raise ValueError("Jira attachment metadata is missing an attachment id")

        content_url = (
            f"{self.base_url}/rest/api/3/attachment/content/"
            f"{quote(str(attachment_id).strip(), safe='')}"
        )
        try:
            content_response = requests.get(
                content_url,
                headers=self.headers,
                auth=self.auth,
                params={"redirect": "false"},
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Jira attachment content request failed: {exc}") from exc

        if content_response.status_code != 200:
            raise RuntimeError(
                f"Jira attachment content request failed ({content_response.status_code}): "
                f"{content_response.text}"
            )
        return content_response.content

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
        project_id = self._fetch_project_id()
        url = f"{self.base_url}/rest/api/3/statuses/search"
        start_at = 0
        normalized_names: dict[str, str] = {}

        while True:
            params = {
                "projectId": project_id,
                "includeGlobalStatuses": True,
                "startAt": start_at,
                "maxResults": self.STATUS_SEARCH_PAGE_SIZE,
            }
            try:
                response = requests.get(
                    url,
                    headers=self.headers,
                    auth=self.auth,
                    params=params,
                    timeout=self.request_timeout,
                )
            except requests.RequestException as exc:
                raise RuntimeError(f"Jira status search request failed: {exc}") from exc

            if response.status_code != 200:
                raise RuntimeError(
                    f"Jira status search request failed ({response.status_code}): {response.text}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError("Jira status search response is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("Jira status search response must be an object")

            page_start = payload.get("startAt")
            if (
                not isinstance(page_start, int)
                or isinstance(page_start, bool)
                or page_start < 0
                or page_start != start_at
            ):
                raise ValueError(
                    "Jira status search response startAt must be the requested non-negative integer"
                )
            is_last = payload.get("isLast")
            if not isinstance(is_last, bool):
                raise ValueError("Jira status search response isLast must be a boolean")
            statuses = payload.get("values")
            if not isinstance(statuses, list):
                raise ValueError("Jira status search response values must be an array")

            for status_index, status in enumerate(statuses):
                if not isinstance(status, dict):
                    raise ValueError(
                        f"Jira status search response status [{status_index}] must be an object"
                    )
                name = status.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(
                        "Jira status search response status "
                        f"[{status_index}].name must be a non-empty string"
                    )
                clean_name = name.strip()
                normalized_names.setdefault(clean_name.casefold(), clean_name)

            if is_last:
                break
            if not statuses:
                raise ValueError("Jira status search response pagination made no progress")
            start_at = page_start + len(statuses)

        if not normalized_names:
            raise ValueError("Jira status search response contains no statuses")
        return set(normalized_names.values())

    def _fetch_project_id(self) -> str:
        """Resolve the configured Jira project key to its numeric project ID."""
        project_key = quote(self.project.key, safe="")
        url = f"{self.base_url}/rest/api/3/project/{project_key}"
        try:
            response = requests.get(
                url,
                headers=self.headers,
                auth=self.auth,
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Jira project lookup request failed: {exc}") from exc

        if response.status_code != 200:
            raise RuntimeError(
                f"Jira project lookup request failed ({response.status_code}): {response.text}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Jira project lookup response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Jira project lookup response must be an object")
        project_id = payload.get("id")
        if isinstance(project_id, bool) or not isinstance(project_id, (str, int)):
            raise ValueError("Jira project lookup response id must be a numeric value")
        clean_project_id = str(project_id).strip()
        if not clean_project_id.isdigit():
            raise ValueError("Jira project lookup response id must be a numeric value")
        return clean_project_id

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
            available_states = ", ".join(
                f"'{state}'" for state in sorted(valid_states, key=str.casefold)
            )
            errors.append(f"jira.project_statuses: available Jira states: {available_states}")
            raise WorkflowStateValidationError(errors)
        self._validate_scheduled_curation_targets(config)

    def _validate_scheduled_curation_targets(self, config: dict[str, Any]) -> None:
        scheduled = config.get("scheduled_phases", {})
        enabled = [
            (name, phase)
            for name, phase in scheduled.items()
            if isinstance(phase, dict) and phase.get("enabled", True) is not False
        ] if isinstance(scheduled, dict) else []
        if not enabled:
            return

        fields_response = requests.get(
            f"{self.base_url}/rest/api/3/field",
            headers=self.headers,
            auth=self.auth,
            timeout=self.request_timeout,
        )
        if fields_response.status_code != 200:
            raise WorkflowValidationError([
                "jira.fields: failed to validate backlog-curation custom fields: "
                f"{fields_response.status_code} {fields_response.text}"
            ])
        fields_payload = fields_response.json()
        if not isinstance(fields_payload, list):
            raise WorkflowValidationError(["jira.fields: Jira field response must be an array"])
        available_fields = {
            field.get("id")
            for field in fields_payload
            if isinstance(field, dict) and isinstance(field.get("id"), str)
        }

        link_response = requests.get(
            f"{self.base_url}/rest/api/3/issueLinkType",
            headers=self.headers,
            auth=self.auth,
            timeout=self.request_timeout,
        )
        if link_response.status_code != 200:
            raise WorkflowValidationError([
                "jira.issue_link_types: failed to validate backlog-curation link type: "
                f"{link_response.status_code} {link_response.text}"
            ])
        link_payload = link_response.json()
        link_types = link_payload.get("issueLinkTypes") if isinstance(link_payload, dict) else None
        if not isinstance(link_types, list):
            raise WorkflowValidationError([
                "jira.issue_link_types: Jira issue-link-type response must contain an array"
            ])
        available_link_types = {
            link_type.get("name")
            for link_type in link_types
            if isinstance(link_type, dict) and isinstance(link_type.get("name"), str)
        }

        errors: list[str] = []
        for phase_name, phase in enabled:
            path = f"scheduled_phases.{phase_name}"
            jira_config = phase["jira"]
            fields_config = self.project.fields
            field_ids = [
                fields_config.business_value_score,
                fields_config.business_value_rationale,
            ]
            if fields_config.epic:
                field_ids.append(fields_config.epic)
            for field_id in field_ids:
                if field_id not in available_fields:
                    errors.append(f"project.jira.fields: unknown Jira field '{field_id}'")
            link_type = jira_config["dependency_link_type"]
            if link_type not in available_link_types:
                errors.append(f"{path}.jira.dependency_link_type: unknown Jira link type '{link_type}'")

            audit_issue = phase["audit_issue"]
            audit_response = requests.get(
                f"{self.base_url}/rest/api/3/issue/{quote(audit_issue, safe='')}",
                headers=self.headers,
                auth=self.auth,
                params={"fields": "summary"},
                timeout=self.request_timeout,
            )
            if audit_response.status_code != 200:
                errors.append(
                    f"{path}.audit_issue: Jira issue '{audit_issue}' is unavailable "
                    f"({audit_response.status_code})"
                )
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

    def _search_all(self, jql: str, fields: list[str]) -> list[dict[str, Any]]:
        url = f"{self.base_url}/rest/api/3/search/jql"
        next_page_token: str | None = None
        start_at = 0
        issues: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {
                "jql": jql,
                "maxResults": 100,
                "fields": ",".join(fields),
            }
            if next_page_token:
                params["nextPageToken"] = next_page_token
            elif start_at:
                params["startAt"] = start_at
            try:
                response = requests.get(
                    url,
                    headers=self.headers,
                    auth=self.auth,
                    params=params,
                    timeout=self.request_timeout,
                )
            except requests.RequestException as exc:
                raise RuntimeError(f"Jira backlog search request failed: {exc}") from exc
            if response.status_code != 200:
                raise RuntimeError(
                    f"Jira backlog search request failed ({response.status_code}): {response.text}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError("Jira backlog search response is not valid JSON") from exc
            page = payload.get("issues") if isinstance(payload, dict) else None
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise ValueError("Jira backlog search response must contain an issues array")
            issues.extend(page)

            token = payload.get("nextPageToken")
            if isinstance(token, str) and token:
                if token == next_page_token:
                    raise ValueError("Jira backlog search pagination made no progress")
                next_page_token = token
                continue
            if payload.get("isLast") is True:
                break
            total = payload.get("total")
            page_start = payload.get("startAt", start_at)
            if isinstance(total, int) and isinstance(page_start, int) and page_start + len(page) < total:
                if not page:
                    raise ValueError("Jira backlog search pagination made no progress")
                start_at = page_start + len(page)
                continue
            break
        return issues

    def _normalize_curation_issue(
        self,
        jira_issue: dict[str, Any],
        *,
        score_field: str,
        rationale_field: str,
        epic_field: str | None = None,
    ) -> dict[str, Any]:
        fields = jira_issue.get("fields")
        if not isinstance(fields, dict):
            raise ValueError("Jira curation issue is missing fields")
        key = jira_issue.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("Jira curation issue is missing a key")
        links: list[dict[str, Any]] = []
        for link in fields.get("issuelinks") or []:
            if not isinstance(link, dict):
                continue
            link_type = link.get("type") if isinstance(link.get("type"), dict) else {}
            if isinstance(link.get("outwardIssue"), dict):
                linked_key = link["outwardIssue"].get("key")
                direction = "outward"
            elif isinstance(link.get("inwardIssue"), dict):
                linked_key = link["inwardIssue"].get("key")
                direction = "inward"
            else:
                continue
            if isinstance(linked_key, str) and linked_key:
                links.append(
                    {
                        "type": link_type.get("name"),
                        "direction": direction,
                        "issueKey": linked_key,
                    }
                )
        parent = fields.get("parent") if isinstance(fields.get("parent"), dict) else {}
        return {
            "id": str(jira_issue.get("id", "")),
            "key": key,
            "summary": fields.get("summary"),
            "description": fields.get("description"),
            "issueType": (fields.get("issuetype") or {}).get("name"),
            "components": [
                component.get("name")
                for component in fields.get("components") or []
                if isinstance(component, dict) and isinstance(component.get("name"), str)
            ],
            "priority": (fields.get("priority") or {}).get("name"),
            "status": (fields.get("status") or {}).get("name"),
            "labels": [str(label) for label in fields.get("labels") or []],
            "links": links,
            "parentKey": parent.get("key"),
            "epic": fields.get(epic_field) if epic_field else None,
            "businessValue": {
                "score": fields.get(score_field),
                "rationale": fields.get(rationale_field),
            },
            "created": fields.get("created"),
            "updated": fields.get("updated"),
        }

    def _fetch_curation_issue(
        self,
        issue_key: str,
        score_field: str,
        rationale_field: str,
    ) -> dict[str, Any]:
        encoded_key = quote(issue_key.strip(), safe="")
        url = f"{self.base_url}/rest/api/3/issue/{encoded_key}"
        fields = [
            "summary", "description", "issuetype", "components", "priority",
            "status", "labels", "issuelinks", "parent", "created", "updated",
            score_field, rationale_field,
        ]
        try:
            response = requests.get(
                url,
                headers=self.headers,
                auth=self.auth,
                params={"fields": ",".join(fields)},
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Jira issue refresh failed for {issue_key}: {exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"Jira issue refresh failed for {issue_key} ({response.status_code}): {response.text}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Jira issue refresh for {issue_key} is not valid JSON") from exc
        return self._normalize_curation_issue(
            payload,
            score_field=score_field,
            rationale_field=rationale_field,
            epic_field=None,
        )

    def _update_issue_fields(self, issue_key: str, fields: dict[str, Any]) -> None:
        encoded_key = quote(issue_key.strip(), safe="")
        response = requests.put(
            f"{self.base_url}/rest/api/3/issue/{encoded_key}",
            headers={**self.headers, "Content-Type": "application/json"},
            auth=self.auth,
            json={"fields": fields},
            timeout=self.request_timeout,
        )
        if response.status_code != 204:
            raise RuntimeError(
                f"Jira issue update failed for {issue_key} ({response.status_code}): {response.text}"
            )

    def _add_label(self, issue_key: str, label: str) -> None:
        encoded_key = quote(issue_key.strip(), safe="")
        response = requests.put(
            f"{self.base_url}/rest/api/3/issue/{encoded_key}",
            headers={**self.headers, "Content-Type": "application/json"},
            auth=self.auth,
            json={"update": {"labels": [{"add": label}]}},
            timeout=self.request_timeout,
        )
        if response.status_code != 204:
            # Jira's label add operation is idempotent for a value already present.
            raise RuntimeError(
                f"Jira label update failed for {issue_key} ({response.status_code}): {response.text}"
            )

    def _create_issue_link(
        self,
        blocker: str,
        blocked: str,
        link_type: str,
    ) -> None:
        response = requests.post(
            f"{self.base_url}/rest/api/3/issueLink",
            headers={**self.headers, "Content-Type": "application/json"},
            auth=self.auth,
            json={
                "type": {"name": link_type},
                "outwardIssue": {"key": blocker},
                "inwardIssue": {"key": blocked},
            },
            timeout=self.request_timeout,
        )
        if response.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"Jira issue link failed for {blocker}->{blocked} "
                f"({response.status_code}): {response.text}"
            )

    @staticmethod
    def _has_blocking_link(issue: dict[str, Any], blocked_key: str) -> bool:
        return any(
            link.get("type") == "Blocks"
            and link.get("direction") == "outward"
            and link.get("issueKey") == blocked_key
            for link in issue.get("links", [])
            if isinstance(link, dict)
        )

    def _get_curator_property(self, issue_key: str) -> dict[str, Any]:
        encoded_key = quote(issue_key.strip(), safe="")
        url = (
            f"{self.base_url}/rest/api/3/issue/{encoded_key}/properties/"
            "symphony.backlog-curator"
        )
        response = requests.get(
            url,
            headers=self.headers,
            auth=self.auth,
            timeout=self.request_timeout,
        )
        if response.status_code == 404:
            return {}
        if response.status_code != 200:
            raise RuntimeError(
                f"Jira curator property read failed for {issue_key} "
                f"({response.status_code}): {response.text}"
            )
        payload = response.json()
        value = payload.get("value") if isinstance(payload, dict) else None
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"Jira curator property for {issue_key} must be an object")
        return value

    def _put_curator_property(self, issue_key: str, value: dict[str, Any]) -> None:
        encoded_key = quote(issue_key.strip(), safe="")
        url = (
            f"{self.base_url}/rest/api/3/issue/{encoded_key}/properties/"
            "symphony.backlog-curator"
        )
        response = requests.put(
            url,
            headers={**self.headers, "Content-Type": "application/json"},
            auth=self.auth,
            json=value,
            timeout=self.request_timeout,
        )
        if response.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"Jira curator property write failed for {issue_key} "
                f"({response.status_code}): {response.text}"
            )

    def _record_curator_operation(self, issue_key: str, operation_hash: str) -> None:
        provenance = self._get_curator_property(issue_key)
        provenance["operationHashes"] = self._append_operation_hash(
            provenance.get("operationHashes"), operation_hash
        )
        self._put_curator_property(issue_key, provenance)

    @staticmethod
    def _append_operation_hash(existing: Any, operation_hash: str) -> list[str]:
        hashes = [item for item in existing if isinstance(item, str)] if isinstance(existing, list) else []
        if operation_hash not in hashes:
            hashes.append(operation_hash)
        # Bound issue-property growth while retaining recent retry/audit history.
        return hashes[-200:]

    def _comment_contains(self, issue_key: str, marker: str) -> bool:
        encoded_key = quote(issue_key.strip(), safe="")
        response = requests.get(
            f"{self.base_url}/rest/api/3/issue/{encoded_key}/comment",
            headers=self.headers,
            auth=self.auth,
            params={"maxResults": 100, "orderBy": "-created"},
            timeout=self.request_timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Jira comment lookup failed for {issue_key} "
                f"({response.status_code}): {response.text}"
            )
        payload = response.json()
        comments = payload.get("comments") if isinstance(payload, dict) else None
        if not isinstance(comments, list):
            raise ValueError("Jira comment lookup response must contain a comments array")
        return any(marker in self._adf_text(comment.get("body")) for comment in comments if isinstance(comment, dict))

    @classmethod
    def _adf_text(cls, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(cls._adf_text(item) for item in value)
        if isinstance(value, dict):
            own = value.get("text") if isinstance(value.get("text"), str) else ""
            return " ".join(part for part in (own, cls._adf_text(value.get("content", []))) if part)
        return ""

    @staticmethod
    def _operation_hash(run_id: str, issue_key: str, kind: str, value: Any) -> str:
        encoded = json.dumps(
            [run_id, issue_key, kind, value],
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:20]

    def fetch_candidate_issues(self, active_states: List[str]) -> List[Dict[str, Any]]:
        """Return issues in the phase-configured states using the modern /jql endpoint."""
        # Escape statuses with single quotes
        states_jql = ", ".join([f"'{state}'" for state in active_states])
        jql = f"project = '{self.project.key}' AND status IN ({states_jql}) ORDER BY priority ASC, created ASC"
        
        # Appended /jql to migrate to the mandatory Atlassian endpoint
        url = f"{self.base_url}/rest/api/3/search/jql"
        params = {
            "jql": jql, 
            "maxResults": 50,
            "fields": "summary,description,priority,status,labels,created,updated"
        }
        
        response = requests.get(
            url,
            headers=self.headers,
            auth=self.auth,
            params=params,
            timeout=self.request_timeout,
        )
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
        
        response = requests.get(
            url,
            headers=self.headers,
            auth=self.auth,
            params=params,
            timeout=self.request_timeout,
        )
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
        
        response = requests.get(
            transitions_url,
            headers=headers,
            auth=self.auth,
            timeout=self.request_timeout,
        )
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
        post_response = requests.post(
            transitions_url,
            headers=headers,
            auth=self.auth,
            json=payload,
            timeout=self.request_timeout,
        )
        
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

        response = requests.post(
            comments_url,
            headers=headers,
            auth=self.auth,
            json=payload,
            timeout=self.request_timeout,
        )
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
