from typing import Any, Protocol


class TrackerAdapter(Protocol):
    """Tracker operations required by the orchestration core."""

    def validate_workflow_states(self, config: dict[str, Any]) -> None:
        ...

    def fetch_candidate_issues(self, active_states: list[str]) -> list[dict[str, Any]]:
        ...

    def transition_issue(self, issue_identifier: str, target_state: str) -> bool:
        ...

    def add_comment(self, issue_identifier: str, body: str) -> bool:
        ...
