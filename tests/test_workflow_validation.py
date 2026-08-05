import pytest

from app.core.workflow_validation import (
    WorkflowValidationError,
    collect_workflow_state_references,
    validate_workflow_config,
)
from app.core.config import load_config
from app.services.actions import ActionRegistry
from app.services.bitbucket import BitbucketService
from app.services.jira import JiraClient


def test_workflow_validation_normalizes_transitions_without_tracker_knowledge():
    registry = ActionRegistry([("test:action", lambda _phase_result: None)])
    config = {
        "phases": {
            "plan": {
                "states": ["to do"],
                "transitions": {
                    "on_start": "IN PROGRESS",
                    "success": "done",
                    "blocked": {
                        "next": "clarification needed",
                        "do": [{"action": "test:action"}],
                    },
                },
            }
        }
    }

    transitions = validate_workflow_config(config, registry)

    assert transitions[("plan", "success")].next_state == "done"
    assert transitions[("plan", "blocked")].actions == ("test:action",)


def test_collect_workflow_state_references_preserves_paths():
    config = {
        "phases": {
            "plan": {
                "states": ["Missing Trigger"],
                "transitions": {
                    "on_start": "Missing Start",
                    "success": "Missing Success",
                    "blocked": {"next": "Missing Blocked", "do": []},
                },
            }
        }
    }

    references = collect_workflow_state_references(config)

    assert [(reference.path, reference.name) for reference in references] == [
        ("phases.plan.states[0]", "Missing Trigger"),
        ("phases.plan.transitions.on_start", "Missing Start"),
        ("phases.plan.transitions.success", "Missing Success"),
        ("phases.plan.transitions.blocked.next", "Missing Blocked"),
    ]


def test_workflow_validation_collects_multiple_static_errors():
    config = {
        "phases": {
            "plan": {
                "states": ["", 123],
                "transitions": {
                    "on_start": None,
                    "success": {"next": "Done", "do": [{"action": "missing:action"}]},
                },
            }
        }
    }

    with pytest.raises(WorkflowValidationError) as exc_info:
        validate_workflow_config(config, ActionRegistry())

    assert len(exc_info.value.errors) == 4
    assert "phases.plan.states[0]" in str(exc_info.value)
    assert "phases.plan.states[1]" in str(exc_info.value)
    assert "phases.plan.transitions.on_start" in str(exc_info.value)
    assert "unknown transition action 'missing:action'" in str(exc_info.value)


def test_workflow_validation_accepts_registered_jira_attach_outputs_action():
    config = {
        "phases": {
            "plan": {
                "transitions": {
                    "success": {
                        "next": "Review",
                        "do": [{"action": "jira:attach_outputs"}],
                    }
                }
            }
        }
    }

    registry = ActionRegistry(
        [("jira:attach_outputs", lambda _phase_result: None)]
    )

    transitions = validate_workflow_config(config, registry)

    assert transitions[("plan", "success")].actions == ("jira:attach_outputs",)


def test_shipped_workflow_actions_are_registered_by_composed_adapters():
    registry = ActionRegistry()
    JiraClient.__new__(JiraClient).register_actions(registry)
    BitbucketService.__new__(BitbucketService).register_actions(registry)

    transitions = validate_workflow_config(load_config("WORKFLOW.md"), registry)

    assert transitions[("plan", "success")].actions == ("jira:attach_outputs",)
    assert transitions[("implement", "success")].actions == (
        "bitbucket:create-pull-request",
    )
