from dataclasses import dataclass
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.actions import ActionResolver, CompletionTransition


@dataclass(frozen=True)
class WorkflowStateReference:
    path: str
    name: str


@dataclass(frozen=True)
class _WorkflowInspection:
    completion_transitions: dict[tuple[str, str], CompletionTransition]


class WorkflowValidationError(ValueError):
    def __init__(self, errors: Iterable[str]):
        self.errors = tuple(errors)
        details = "\n".join(f"- {error}" for error in self.errors)
        super().__init__(f"Invalid workflow configuration:\n{details}")


class WorkflowStateValidationError(WorkflowValidationError):
    """Raised when configured tracker state names are not available."""


def validate_workflow_config(
    config: object,
    action_registry: ActionResolver,
) -> dict[tuple[str, str], CompletionTransition]:
    """Validate tracker-neutral workflow structure and registered actions."""
    return _inspect_workflow(config, action_registry).completion_transitions


def collect_workflow_state_references(config: object) -> tuple[WorkflowStateReference, ...]:
    """Collect configured tracker state references with diagnostic paths.

    Callers should run static workflow validation first. This helper remains
    defensive so tracker adapters can safely use it independently.
    """
    references: list[WorkflowStateReference] = []
    phases = config.get("phases", {}) if isinstance(config, dict) else {}
    if not isinstance(phases, dict):
        return ()

    for phase_name, phase_config in phases.items():
        if not isinstance(phase_config, dict):
            continue
        phase_path = f"phases.{phase_name}"
        states = phase_config.get("states", [])
        if isinstance(states, list):
            for index, state in enumerate(states):
                state_name = _non_empty_string(state)
                if state_name is not None:
                    references.append(
                        WorkflowStateReference(f"{phase_path}.states[{index}]", state_name)
                    )

        transitions = phase_config.get("transitions", {})
        if not isinstance(transitions, dict):
            continue
        on_start = _non_empty_string(transitions.get("on_start"))
        if on_start is not None:
            references.append(
                WorkflowStateReference(f"{phase_path}.transitions.on_start", on_start)
            )
        for status in ("success", "blocked"):
            if status not in transitions:
                continue
            configured = transitions[status]
            state_name = _non_empty_string(configured)
            path = f"{phase_path}.transitions.{status}"
            if state_name is not None:
                references.append(WorkflowStateReference(path, state_name))
            elif isinstance(configured, dict):
                next_state = _non_empty_string(configured.get("next"))
                if next_state is not None:
                    references.append(WorkflowStateReference(f"{path}.next", next_state))

    scheduled = config.get("scheduled_phases", {}) if isinstance(config, dict) else {}
    if isinstance(scheduled, dict):
        for phase_name, phase_config in scheduled.items():
            if not isinstance(phase_config, dict) or phase_config.get("enabled", True) is False:
                continue
            transitions = phase_config.get("transitions", {})
            if not isinstance(transitions, dict):
                continue
            for status in ("success", "blocked"):
                configured = transitions.get(status)
                if not isinstance(configured, dict):
                    continue
                next_state = _non_empty_string(configured.get("next"))
                if next_state is not None:
                    references.append(
                        WorkflowStateReference(
                            f"scheduled_phases.{phase_name}.transitions.{status}.next",
                            next_state,
                        )
                    )

    return tuple(references)


def _inspect_workflow(config: object, action_registry: ActionResolver) -> _WorkflowInspection:
    errors: list[str] = []
    completion_transitions: dict[tuple[str, str], CompletionTransition] = {}

    if not isinstance(config, dict):
        raise WorkflowValidationError(["workflow: must be a mapping"])

    phases = config.get("phases")
    if not isinstance(phases, dict):
        raise WorkflowValidationError(["phases: must be a mapping"])
    if not phases:
        raise WorkflowValidationError(["phases: must define at least one phase"])

    for phase_name, phase_config in phases.items():
        phase_path = f"phases.{phase_name}"
        if not isinstance(phase_config, dict):
            errors.append(f"{phase_path}: must be a mapping")
            continue

        states = phase_config.get("states")
        if states is not None:
            if not isinstance(states, list):
                errors.append(f"{phase_path}.states: must be a list")
            else:
                for index, state in enumerate(states):
                    path = f"{phase_path}.states[{index}]"
                    state_name = _non_empty_string(state)
                    if state_name is None:
                        errors.append(f"{path}: must be a non-empty string")

        transitions = phase_config.get("transitions")
        if transitions is None:
            continue
        if not isinstance(transitions, dict):
            errors.append(f"{phase_path}.transitions: must be a mapping")
            continue

        if "on_start" in transitions:
            path = f"{phase_path}.transitions.on_start"
            state_name = _non_empty_string(transitions["on_start"])
            if state_name is None:
                errors.append(f"{path}: must be a non-empty string")

        for status in ("success", "blocked"):
            if status not in transitions:
                continue
            path = f"{phase_path}.transitions.{status}"
            normalized = _inspect_completion_transition(
                transitions[status],
                path,
                action_registry,
                errors,
                allow_action_only=False,
            )
            if normalized is not None:
                completion_transitions[(phase_name, status)] = normalized

    scheduled_phases = config.get("scheduled_phases", {})
    if not isinstance(scheduled_phases, dict):
        errors.append("scheduled_phases: must be a mapping")
        scheduled_phases = {}

    for phase_name, phase_config in scheduled_phases.items():
        phase_path = f"scheduled_phases.{phase_name}"
        if phase_name in phases:
            errors.append(f"{phase_path}: phase name is already used under phases")
        if not isinstance(phase_config, dict):
            errors.append(f"{phase_path}: must be a mapping")
            continue
        if phase_config.get("enabled", True) is False:
            continue
        for field_name in ("agent", "daily_at", "timezone", "audit_issue"):
            if _non_empty_string(phase_config.get(field_name)) is None:
                errors.append(f"{phase_path}.{field_name}: must be a non-empty string")
        daily_at = _non_empty_string(phase_config.get("daily_at"))
        if daily_at is not None:
            try:
                hour_text, minute_text = daily_at.split(":")
                valid_time = (
                    len(hour_text) == 2
                    and len(minute_text) == 2
                    and 0 <= int(hour_text) <= 23
                    and 0 <= int(minute_text) <= 59
                )
            except (TypeError, ValueError):
                valid_time = False
            if not valid_time:
                errors.append(f"{phase_path}.daily_at: must use 24-hour HH:MM format")
        timezone_name = _non_empty_string(phase_config.get("timezone"))
        if timezone_name is not None:
            try:
                ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                errors.append(f"{phase_path}.timezone: unknown timezone '{timezone_name}'")

        _inspect_backlog_curation_config(phase_config, phase_path, errors)

        transitions = phase_config.get("transitions")
        if not isinstance(transitions, dict):
            errors.append(f"{phase_path}.transitions: must be a mapping")
            continue
        for status in ("success", "blocked"):
            if status not in transitions:
                continue
            path = f"{phase_path}.transitions.{status}"
            normalized = _inspect_completion_transition(
                transitions[status],
                path,
                action_registry,
                errors,
                allow_action_only=True,
            )
            if normalized is not None:
                completion_transitions[(phase_name, status)] = normalized

    if errors:
        raise WorkflowValidationError(errors)
    return _WorkflowInspection(completion_transitions)


def _inspect_completion_transition(
    configured: object,
    path: str,
    action_registry: ActionResolver,
    errors: list[str],
    *,
    allow_action_only: bool,
) -> CompletionTransition | None:
    state_name = _non_empty_string(configured)
    if state_name is not None:
        return CompletionTransition(next_state=state_name)

    if not isinstance(configured, dict):
        errors.append(f"{path}: must be a non-empty string or an expanded transition")
        return None
    expected_keys = ({"do"},) if allow_action_only else ({"next", "do"},)
    if set(configured) not in expected_keys:
        expectation = "exactly 'do'" if allow_action_only else "exactly 'next' and 'do'"
        errors.append(f"{path}: must contain {expectation}")
        return None

    next_path = f"{path}.next"
    next_state = _non_empty_string(configured.get("next"))
    if "next" in configured and next_state is None:
        errors.append(f"{next_path}: must be a non-empty string")

    action_names: list[str] = []
    configured_actions = configured.get("do")
    if not isinstance(configured_actions, list):
        errors.append(f"{path}.do: must be a list")
    else:
        for index, action_config in enumerate(configured_actions):
            action_path = f"{path}.do[{index}]"
            if not isinstance(action_config, dict) or set(action_config) != {"action"}:
                errors.append(f"{action_path}: must contain exactly 'action'")
                continue
            action_name = _non_empty_string(action_config.get("action"))
            if action_name is None:
                errors.append(f"{action_path}.action: must be a non-empty string")
                continue
            if not action_registry.contains(action_name):
                errors.append(f"{action_path}.action: unknown transition action '{action_name}'")
                continue
            action_names.append(action_name)

    if ("next" in configured and next_state is None) or not isinstance(configured_actions, list):
        return None
    return CompletionTransition(next_state=next_state, actions=tuple(action_names))


def _inspect_backlog_curation_config(
    configured: dict,
    path: str,
    errors: list[str],
) -> None:
    jira_config = configured.get("jira")
    confidence_config = configured.get("confidence")
    if "input" in configured:
        errors.append(
            f"{path}.input: replaced by project.jira.backlog and "
            "project.confluence.strategy_pages"
        )
    if not isinstance(jira_config, dict):
        errors.append(f"{path}.jira: must be a mapping")
    else:
        allowed_jira_fields = {
            "clarification_label",
            "review_label",
            "dependency_link_type",
        }
        for field_name in sorted(set(jira_config) - allowed_jira_fields):
            destination = "project.jira.fields" if field_name in {
                "business_value_score_field",
                "business_value_rationale_field",
                "epic_field",
            } else f"{path}.jira"
            errors.append(
                f"{path}.jira.{field_name}: unknown configuration field; use {destination}"
            )
        for field_name in (
            "clarification_label",
            "review_label",
            "dependency_link_type",
        ):
            if _non_empty_string(jira_config.get(field_name)) is None:
                errors.append(f"{path}.jira.{field_name}: must be a non-empty string")
        link_type = _non_empty_string(jira_config.get("dependency_link_type"))
        if link_type is not None and link_type != "Blocks":
            errors.append(f"{path}.jira.dependency_link_type: only 'Blocks' is supported")
    if not isinstance(confidence_config, dict):
        errors.append(f"{path}.confidence: must be a mapping")
    else:
        for confidence_name in ("business_value", "dependency", "clarification"):
            value = confidence_config.get(confidence_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                errors.append(f"{path}.confidence.{confidence_name}: must be between 0 and 1")
    dry_run = configured.get("dry_run", True)
    if not isinstance(dry_run, bool):
        errors.append(f"{path}.dry_run: must be a boolean")



def _non_empty_string(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
