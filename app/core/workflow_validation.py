from dataclasses import dataclass
from typing import Iterable

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
) -> CompletionTransition | None:
    state_name = _non_empty_string(configured)
    if state_name is not None:
        return CompletionTransition(next_state=state_name)

    if not isinstance(configured, dict):
        errors.append(f"{path}: must be a non-empty string or an expanded transition")
        return None
    if set(configured) != {"next", "do"}:
        errors.append(f"{path}: must contain exactly 'next' and 'do'")
        return None

    next_path = f"{path}.next"
    next_state = _non_empty_string(configured.get("next"))
    if next_state is None:
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

    if next_state is None or not isinstance(configured_actions, list):
        return None
    return CompletionTransition(next_state=next_state, actions=tuple(action_names))


def _non_empty_string(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
