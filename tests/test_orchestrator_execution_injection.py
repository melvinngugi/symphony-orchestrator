from dataclasses import dataclass
import inspect

import pytest

from app.core import orchestrator as orchestrator_module
from app.core.orchestrator import SymphonyOrchestrator
from app.core.workflow_validation import WorkflowValidationError
from app.models.agent_config import AgentConfig, AgentsRegistry
from app.services.agent import AgentExecutionResult
from app.services.actions import ActionRegistry, PhaseResult


class FakeJiraClient:
    DEFAULT_STATUS_NAMES = {
        "To Do",
        "Planning",
        "In Progress",
        "Reopened",
        "In Review",
        "Review",
        "Blocked",
        "Clarification Needed",
        "Done",
    }

    def __init__(
        self,
        issues=None,
        transition_result=True,
        transition_error=None,
        events=None,
        validation_error=None,
    ):
        self.issues = issues or []
        self.transition_result = transition_result
        self.transition_error = transition_error
        self.events = events
        self.requested_states = []
        self.comments = []
        self.transitions = []
        self.validation_error = validation_error
        self.validated_configs = []

    def validate_workflow_states(self, config):
        self.validated_configs.append(config)
        if self.validation_error is not None:
            raise self.validation_error

    def fetch_candidate_issues(self, active_states):
        self.requested_states.append(active_states)
        return self.issues

    def transition_issue(self, issue_identifier: str, target_state: str) -> bool:
        self.transitions.append((issue_identifier, target_state))
        if self.events is not None:
            self.events.append(("transition", issue_identifier, target_state))
        if self.transition_error is not None:
            raise self.transition_error
        return self.transition_result

    def add_comment(self, issue_identifier: str, body: str) -> bool:
        self.comments.append((issue_identifier, body))
        return True


class FakeBitbucketService:
    def prepare_workspace(self, _identifier):
        return "/fake/workspace"

    def create_pull_request_for_phase(self, _phase_result):
        return None

    def register_actions(self, registry):
        registry.register(
            "bitbucket:create-pull-request",
            self.create_pull_request_for_phase,
        )


class ReadOnlyActionResolver:
    def __init__(self, actions=None):
        self._actions = actions or {}

    def contains(self, name):
        return name in self._actions

    def resolve(self, name):
        return self._actions[name]


@dataclass
class FakeExecutionHandle:
    issue_id: str
    phase_name: str
    structured_output_file: str | None = None


class FakeExecutionController:
    def __init__(self, events=None):
        self.events = events
        self.starts = []
        self.requests = []
        self.completions = {}

    def start_execution(self, request):
        self.requests.append(request)
        if self.events is not None:
            self.events.append(("start", request.issue["identifier"], request.agent_name))
        phase_name = _phase_name_from_stdin(request.agent_config.stdin)
        handle = FakeExecutionHandle(
            issue_id=request.issue["id"],
            phase_name=phase_name,
            structured_output_file=request.agent_config.structured,
        )
        self.starts.append((request.issue["id"], phase_name, request.agent_name))
        return handle

    def poll_execution(self, execution):
        return self.completions.pop((execution.issue_id, execution.phase_name), None)



def _phase_name_from_stdin(stdin_value: str) -> str:
    mapping = {
        "issue.json": "plan",
        "plan.md": "implement",
        "implementation_report.json": "validate",
    }
    return mapping.get(stdin_value, "unknown")



def _build_orchestrator(
    fake_executor,
    workflow_config,
    issues=None,
    issue_writer=None,
    agents_registry=None,
    tracker=None,
    action_registry=None,
):
    agents_registry = agents_registry or AgentsRegistry(
        agents={
            "planner": AgentConfig(
                command="fake",
                args=[],
                stdin="issue.json",
                output_file="plan.md",
                sandbox="workspace-write",
                env=[],
            ),
            "implementer": AgentConfig(
                command="fake",
                args=[],
                stdin="plan.md",
                output_file="implementation_report.json",
                sandbox="workspace-write",
                env=[],
            ),
        }
    )

    bitbucket = FakeBitbucketService()
    registry = action_registry or ActionRegistry()
    if action_registry is None:
        bitbucket.register_actions(registry)

    return SymphonyOrchestrator(
        workflow_config,
        agents_registry=agents_registry,
        execution_controller=fake_executor,
        tracker=tracker or FakeJiraClient(issues=issues),
        bitbucket_service=bitbucket,
        issue_writer=issue_writer or (lambda _workspace, _issue: None),
        action_registry=registry,
    )


def test_orchestrator_depends_on_tracker_abstraction_not_jira_client():
    parameters = inspect.signature(SymphonyOrchestrator).parameters

    assert "tracker" in parameters
    assert "jira_client" not in parameters
    assert "JiraClient" not in vars(orchestrator_module)
    assert parameters["action_registry"].default is inspect.Parameter.empty
    assert "ActionRegistry" not in vars(orchestrator_module)


def test_orchestrator_uses_action_registry_through_read_only_resolver():
    calls = []
    resolver = ReadOnlyActionResolver(
        {
            "test:action": lambda phase_result: calls.append(
                (phase_result.workspace_path, phase_result.issue["id"])
            )
        }
    )
    tracker = FakeJiraClient()
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {
                    "success": {
                        "next": "In Review",
                        "do": [{"action": "test:action"}],
                    }
                },
            }
        }
    }
    orchestrator = _build_orchestrator(
        FakeExecutionController(),
        config,
        tracker=tracker,
        action_registry=resolver,
    )
    issue = {"id": "ISSUE-READ-ONLY", "identifier": "ISSUE-READ-ONLY"}

    orchestrator._transition_for_phase_status(
        PhaseResult(
            issue=issue,
            workspace_path="/issues/ISSUE-READ-ONLY",
            repository_path="/issues/ISSUE-READ-ONLY/repository",
            phase_name="plan",
            agent_name="planner",
            agent_config=orchestrator.agents_config.agents["planner"],
            execution=AgentExecutionResult(exit_code=0, stdout="", stderr=""),
        )
    )

    assert not hasattr(resolver, "register")
    assert calls == [("/issues/ISSUE-READ-ONLY", "ISSUE-READ-ONLY")]
    assert tracker.transitions == [("ISSUE-READ-ONLY", "In Review")]


def test_dispatch_phase_uses_injected_execution_controller():
    fake_executor = FakeExecutionController()
    config = {
        "phases": {
            "plan": {"agent": "planner"},
        }
    }
    orchestrator = _build_orchestrator(fake_executor, config)

    issue = {"id": "ISSUE-1", "identifier": "ISSUE-1", "title": "Test"}
    orchestrator._dispatch_phase(issue, "/fake/workspace", "plan")

    assert fake_executor.starts == [("ISSUE-1", "plan", "planner")]
    assert "ISSUE-1" in orchestrator.state.running
    assert orchestrator.state.running["ISSUE-1"]["execution"].phase_name == "plan"
    assert fake_executor.requests[0].workspace_path == "/fake/workspace"
    assert fake_executor.requests[0].repository_path == "/fake/workspace/repository"


def test_dispatch_phase_applies_on_start_transition_before_launching_agent():
    events = []
    fake_executor = FakeExecutionController(events=events)
    jira = FakeJiraClient(events=events)
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {"on_start": "In Progress"},
            },
        }
    }
    orchestrator = _build_orchestrator(fake_executor, config, tracker=jira)
    issue = {"id": "ISSUE-START", "identifier": "ISSUE-START", "title": "Test"}

    dispatched = orchestrator._dispatch_phase(issue, "/fake/workspace", "plan")

    assert dispatched is True
    assert events == [
        ("transition", "ISSUE-START", "In Progress"),
        ("start", "ISSUE-START", "planner"),
    ]
    assert jira.comments == []


def test_dispatch_phase_ignores_absent_on_start_transition():
    fake_executor = FakeExecutionController()
    jira = FakeJiraClient()
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {},
            },
        }
    }
    orchestrator = _build_orchestrator(fake_executor, config, tracker=jira)
    issue = {"id": "ISSUE-NO-START", "identifier": "ISSUE-NO-START", "title": "Test"}

    assert orchestrator._dispatch_phase(issue, "/fake/workspace", "plan") is True
    assert jira.transitions == []
    assert fake_executor.starts == [("ISSUE-NO-START", "plan", "planner")]


@pytest.mark.parametrize("configured", ["", "   ", 123])
def test_orchestrator_rejects_invalid_on_start_transition(configured):
    jira = FakeJiraClient()
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {"on_start": configured},
            },
        }
    }

    with pytest.raises(ValueError, match="phases.plan.transitions.on_start"):
        _build_orchestrator(FakeExecutionController(), config, tracker=jira)
    assert jira.validated_configs == []


def test_dispatch_phase_does_not_launch_when_on_start_transition_returns_false():
    fake_executor = FakeExecutionController()
    jira = FakeJiraClient(transition_result=False)
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {"on_start": "In Progress"},
            },
        }
    }
    orchestrator = _build_orchestrator(fake_executor, config, tracker=jira)
    issue = {"id": "ISSUE-FAILED-START", "identifier": "ISSUE-FAILED-START", "title": "Test"}

    dispatched = orchestrator._dispatch_phase(issue, "/fake/workspace", "plan")

    assert dispatched is False
    assert fake_executor.starts == []
    assert "ISSUE-FAILED-START" not in orchestrator.state.running
    assert "ISSUE-FAILED-START" not in orchestrator.state.claimed
    assert "Failed tracker on_start transition" in orchestrator.state.errors[0].message


def test_dispatch_phase_does_not_launch_when_on_start_transition_raises():
    fake_executor = FakeExecutionController()
    jira = FakeJiraClient(transition_error=RuntimeError("Jira unavailable"))
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {"on_start": "In Progress"},
            },
        }
    }
    orchestrator = _build_orchestrator(fake_executor, config, tracker=jira)
    issue = {"id": "ISSUE-START-ERROR", "identifier": "ISSUE-START-ERROR", "title": "Test"}

    dispatched = orchestrator._dispatch_phase(issue, "/fake/workspace", "plan")

    assert dispatched is False
    assert fake_executor.starts == []
    assert "ISSUE-START-ERROR" not in orchestrator.state.running
    assert "ISSUE-START-ERROR" not in orchestrator.state.claimed
    assert "Jira unavailable" in orchestrator.state.errors[0].message


def test_dispatch_phase_does_not_launch_on_start_without_issue_identifier():
    fake_executor = FakeExecutionController()
    jira = FakeJiraClient()
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {"on_start": "In Progress"},
            },
        }
    }
    orchestrator = _build_orchestrator(fake_executor, config, tracker=jira)
    issue = {"id": "ISSUE-NO-KEY", "title": "Test"}

    dispatched = orchestrator._dispatch_phase(issue, "/fake/workspace", "plan")

    assert dispatched is False
    assert jira.transitions == []
    assert fake_executor.starts == []
    assert "ISSUE-NO-KEY" not in orchestrator.state.running
    assert "ISSUE-NO-KEY" not in orchestrator.state.claimed
    assert "without identifier" in orchestrator.state.errors[0].message


def test_tick_retries_issue_after_on_start_transition_failure():
    fake_executor = FakeExecutionController()
    issue = {
        "id": "ISSUE-START-RETRY",
        "identifier": "ISSUE-START-RETRY",
        "title": "Retry start",
        "state": "To Do",
        "labels": ["AI"],
    }
    jira = FakeJiraClient(issues=[issue], transition_result=False)
    config = {
        "tracker": {"required_labels": ["AI"]},
        "phases": {
            "plan": {
                "agent": "planner",
                "states": ["To Do"],
                "transitions": {"on_start": "In Progress"},
            },
        },
    }
    orchestrator = _build_orchestrator(fake_executor, config, tracker=jira)

    orchestrator._tick()
    jira.transition_result = True
    orchestrator._tick()

    assert jira.transitions == [
        ("ISSUE-START-RETRY", "In Progress"),
        ("ISSUE-START-RETRY", "In Progress"),
    ]
    assert fake_executor.starts == [("ISSUE-START-RETRY", "plan", "planner")]
    assert "ISSUE-START-RETRY" in orchestrator.state.claimed



def test_reconcile_waits_for_jira_state_before_dispatching_another_phase():
    fake_executor = FakeExecutionController()
    jira = FakeJiraClient()
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {"success": "In Progress", "blocked": "Clarification Needed"},
            },
            "implement": {"agent": "implementer"},
        }
    }
    orchestrator = _build_orchestrator(fake_executor, config, tracker=jira)
    issue = {"id": "ISSUE-2", "identifier": "ISSUE-2", "title": "Test"}

    orchestrator._dispatch_phase(issue, "/fake/workspace", "plan")
    fake_executor.completions[("ISSUE-2", "plan")] = AgentExecutionResult(
        exit_code=0,
        stdout="plan output",
        stderr="",
        message="plan output",
    )

    orchestrator._reconcile_running_tasks()

    assert jira.comments == [
        (
            "ISSUE-2",
            "[agent planner]: plan output",
        )
    ]
    assert fake_executor.starts == [
        ("ISSUE-2", "plan", "planner"),
    ]
    assert jira.transitions == [("ISSUE-2", "In Progress")]
    assert "ISSUE-2" not in orchestrator.state.running



def test_reconcile_records_error_and_stops_on_failure():
    fake_executor = FakeExecutionController()
    config = {
        "phases": {
            "plan": {"agent": "planner"},
            "implement": {"agent": "implementer"},
        }
    }
    orchestrator = _build_orchestrator(fake_executor, config)
    issue = {"id": "ISSUE-3", "identifier": "ISSUE-3", "title": "Test"}

    orchestrator._dispatch_phase(issue, "/fake/workspace", "plan")
    fake_executor.completions[("ISSUE-3", "plan")] = AgentExecutionResult(
        exit_code=2,
        stdout="",
        stderr="bad input",
    )

    orchestrator._reconcile_running_tasks()

    assert len(orchestrator.state.errors) == 1
    assert "Agent failure for ISSUE-3" in orchestrator.state.errors[0].message
    assert "bad input" in orchestrator.state.errors[0].message
    assert fake_executor.starts == [("ISSUE-3", "plan", "planner")]
    assert "ISSUE-3" not in orchestrator.state.running
    assert "ISSUE-3" not in orchestrator.state.completed


def test_reconcile_structured_success_extracts_outputs_and_waits_for_jira(tmp_path):
    fake_executor = FakeExecutionController()
    jira = FakeJiraClient()
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {
                    "on_start": "Planning",
                    "success": "In Progress",
                    "blocked": "Blocked",
                },
            },
            "implement": {"agent": "implementer"},
        }
    }
    agents_registry = AgentsRegistry(
        agents={
            "planner": AgentConfig(
                command="fake",
                args=[],
                stdin="issue.json",
                output_file="plan.md",
                structured="planner-result.json",
                sandbox="workspace-write",
                env=[],
            ),
            "implementer": AgentConfig(
                command="fake",
                args=[],
                stdin="plan.md",
                output_file="implementation_report.json",
                sandbox="workspace-write",
                env=[],
            ),
        }
    )
    orchestrator = _build_orchestrator(
        fake_executor,
        config,
        agents_registry=agents_registry,
        tracker=jira,
    )

    issue = {"id": "ISSUE-10", "identifier": "ISSUE-10", "title": "Structured success"}
    workspace = str(tmp_path)

    orchestrator._dispatch_phase(issue, workspace, "plan")
    fake_executor.completions[("ISSUE-10", "plan")] = AgentExecutionResult(
        exit_code=0,
        stdout="",
        stderr="",
        status="success",
        message="done",
    )

    orchestrator._reconcile_running_tasks()

    assert jira.comments == [
        (
            "ISSUE-10",
            "[agent planner]: done",
        )
    ]
    assert jira.transitions == [
        ("ISSUE-10", "Planning"),
        ("ISSUE-10", "In Progress"),
    ]
    assert fake_executor.starts == [
        ("ISSUE-10", "plan", "planner"),
    ]


def test_tick_selects_phase_from_jira_state_and_keeps_label_filter():
    fake_executor = FakeExecutionController()
    jira = FakeJiraClient(
        issues=[
            {
                "id": "ISSUE-20",
                "identifier": "ISSUE-20",
                "title": "Ready to implement",
                "state": "in progress",
                "labels": ["ai"],
            },
            {
                "id": "ISSUE-21",
                "identifier": "ISSUE-21",
                "title": "Wrong label",
                "state": "To Do",
                "labels": ["manual"],
            },
        ]
    )
    config = {
        "tracker": {"required_labels": ["AI"]},
        "phases": {
            "plan": {"agent": "planner", "states": ["To Do"]},
            "implement": {"agent": "implementer", "states": ["In Progress", "Reopened"]},
        },
    }
    orchestrator = _build_orchestrator(fake_executor, config, tracker=jira)

    orchestrator._tick()

    assert jira.requested_states == [["To Do", "In Progress", "Reopened"]]
    assert fake_executor.starts == [("ISSUE-20", "implement", "implementer")]


def test_tick_dispatches_new_phase_after_jira_state_changes_without_recreating_workspace():
    fake_executor = FakeExecutionController()
    issue = {
        "id": "ISSUE-22",
        "identifier": "ISSUE-22",
        "title": "State driven",
        "state": "To Do",
        "labels": ["AI"],
    }
    jira = FakeJiraClient(issues=[issue])
    config = {
        "tracker": {"required_labels": ["AI"]},
        "phases": {
            "plan": {"agent": "planner", "states": ["To Do"]},
            "implement": {"agent": "implementer", "states": ["In Progress"]},
        },
    }
    orchestrator = _build_orchestrator(fake_executor, config, tracker=jira)

    orchestrator._tick()
    fake_executor.completions[("ISSUE-22", "plan")] = AgentExecutionResult(
        exit_code=0,
        stdout="",
        stderr="",
        status="success",
    )
    orchestrator._reconcile_running_tasks()

    # Jira still returning the same state must not run the phase twice.
    orchestrator._tick()
    issue["state"] = "In Progress"
    orchestrator._tick()

    assert fake_executor.starts == [
        ("ISSUE-22", "plan", "planner"),
        ("ISSUE-22", "implement", "implementer"),
    ]


def test_reconcile_structured_blocked_queues_and_stops(tmp_path):
    fake_executor = FakeExecutionController()
    jira = FakeJiraClient()
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {"success": "In Progress", "blocked": "Blocked"},
            },
            "implement": {"agent": "implementer"},
        }
    }
    agents_registry = AgentsRegistry(
        agents={
            "planner": AgentConfig(
                command="fake",
                args=[],
                stdin="issue.json",
                output_file="plan.md",
                structured="planner-result.json",
                sandbox="workspace-write",
                env=[],
            ),
            "implementer": AgentConfig(
                command="fake",
                args=[],
                stdin="plan.md",
                output_file="implementation_report.json",
                sandbox="workspace-write",
                env=[],
            ),
        }
    )
    orchestrator = _build_orchestrator(
        fake_executor,
        config,
        agents_registry=agents_registry,
        tracker=jira,
    )

    issue = {"id": "ISSUE-11", "identifier": "ISSUE-11", "title": "Structured blocked"}
    workspace = str(tmp_path)

    orchestrator._dispatch_phase(issue, workspace, "plan")
    fake_executor.completions[("ISSUE-11", "plan")] = AgentExecutionResult(
        exit_code=0,
        stdout="",
        stderr="",
        status="blocked",
        message="Need additional input",
        needed_clarifications=["missing acceptance criteria"],
    )

    orchestrator._reconcile_running_tasks()

    assert fake_executor.starts == [("ISSUE-11", "plan", "planner")]
    assert jira.comments == [
        (
            "ISSUE-11",
            "[agent planner]: Need additional input\n\n- missing acceptance criteria",
        )
    ]
    assert "ISSUE-11" in orchestrator.state.blocked
    assert orchestrator.state.blocked["ISSUE-11"].message == "Need additional input"
    assert jira.transitions == [("ISSUE-11", "Blocked")]
    assert "ISSUE-11" not in orchestrator.state.running


def test_reconcile_executor_status_failure_records_error_and_halts(tmp_path):
    fake_executor = FakeExecutionController()
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {"success": "In Progress", "blocked": "Blocked"},
            },
            "implement": {"agent": "implementer"},
        }
    }
    agents_registry = AgentsRegistry(
        agents={
            "planner": AgentConfig(
                command="fake",
                args=[],
                stdin="issue.json",
                output_file="plan.md",
                structured="planner-result.json",
                sandbox="workspace-write",
                env=[],
            ),
            "implementer": AgentConfig(
                command="fake",
                args=[],
                stdin="plan.md",
                output_file="implementation_report.json",
                sandbox="workspace-write",
                env=[],
            ),
        }
    )
    orchestrator = _build_orchestrator(fake_executor, config, agents_registry=agents_registry)

    issue = {"id": "ISSUE-12", "identifier": "ISSUE-12", "title": "Executor failure"}
    workspace = str(tmp_path)

    orchestrator._dispatch_phase(issue, workspace, "plan")
    fake_executor.completions[("ISSUE-12", "plan")] = AgentExecutionResult(
        exit_code=1,
        stdout="",
        stderr="Structured output handling failed",
        status="failed",
    )

    orchestrator._reconcile_running_tasks()

    assert fake_executor.starts == [("ISSUE-12", "plan", "planner")]
    assert "ISSUE-12" not in orchestrator.state.running
    assert "ISSUE-12" not in orchestrator.state.blocked
    assert len(orchestrator.state.errors) == 1
    assert "Agent failure for ISSUE-12" in orchestrator.state.errors[0].message


@pytest.mark.parametrize(
    "configured, expected_message",
    [
        ({"do": []}, "exactly 'next' and 'do'"),
        ({"next": "Review"}, "exactly 'next' and 'do'"),
        ({"next": "Review", "do": [], "extra": True}, "exactly 'next' and 'do'"),
        ({"next": "", "do": []}, ".next: must be a non-empty string"),
        ({"next": "Review", "do": "action"}, ".do: must be a list"),
        ({"next": "Review", "do": [{}]}, "must contain exactly 'action'"),
        ({"next": "Review", "do": [{"action": ""}]}, ".action: must be a non-empty string"),
        (123, "must be a non-empty string or an expanded transition"),
    ],
)
def test_orchestrator_rejects_malformed_expanded_transitions(configured, expected_message):
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {"success": configured},
            }
        }
    }

    with pytest.raises(ValueError, match=expected_message):
        _build_orchestrator(FakeExecutionController(), config)


def test_orchestrator_rejects_unknown_transition_action_before_querying_jira():
    jira = FakeJiraClient()
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {
                    "success": {
                        "next": "Review",
                        "do": [{"action": "unknown:action"}],
                    }
                },
            }
        }
    }

    with pytest.raises(ValueError, match="unknown transition action 'unknown:action'"):
        _build_orchestrator(FakeExecutionController(), config, tracker=jira)
    assert jira.validated_configs == []


def test_orchestrator_delegates_workflow_state_validation_to_tracker():
    tracker = FakeJiraClient()
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "states": ["Tracker State"],
                "transitions": {"success": "Tracker Target"},
            }
        }
    }

    _build_orchestrator(FakeExecutionController(), config, tracker=tracker)

    assert tracker.validated_configs == [config]


def test_orchestrator_propagates_tracker_workflow_validation_failure():
    validation_error = WorkflowValidationError(
        ["phases.plan.states[0]: unknown Jira state 'Missing'"]
    )
    tracker = FakeJiraClient(validation_error=validation_error)
    config = {"phases": {"plan": {"agent": "planner", "states": ["To Do"]}}}

    with pytest.raises(WorkflowValidationError) as exc_info:
        _build_orchestrator(FakeExecutionController(), config, tracker=tracker)

    assert exc_info.value is validation_error
    assert tracker.validated_configs == [config]


def test_expanded_transition_actions_retry_from_failed_action():
    executor = FakeExecutionController()
    jira = FakeJiraClient()
    calls = []
    flaky_attempts = 0

    phase_results = []

    def first(phase_result):
        phase_results.append(phase_result)
        calls.append(
            ("first", phase_result.workspace_path, phase_result.issue["identifier"])
        )

    def flaky(phase_result):
        nonlocal flaky_attempts
        flaky_attempts += 1
        phase_results.append(phase_result)
        calls.append(
            ("flaky", phase_result.workspace_path, phase_result.issue["identifier"])
        )
        if flaky_attempts == 1:
            raise RuntimeError("temporary failure")

    def last(phase_result):
        phase_results.append(phase_result)
        calls.append(
            ("last", phase_result.workspace_path, phase_result.issue["identifier"])
        )

    registry = ActionRegistry([
        ("test:first", first),
        ("test:flaky", flaky),
        ("test:last", last),
    ])
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {
                    "success": {
                        "next": "In Review",
                        "do": [
                            {"action": "test:first"},
                            {"action": "test:flaky"},
                            {"action": "test:last"},
                        ],
                    }
                },
            }
        }
    }
    orchestrator = _build_orchestrator(
        executor,
        config,
        tracker=jira,
        action_registry=registry,
    )
    issue = {"id": "ISSUE-ACTION", "identifier": "ISSUE-ACTION", "title": "Actions"}

    orchestrator._dispatch_phase(issue, "/issues/ISSUE-ACTION", "plan")
    executor.completions[("ISSUE-ACTION", "plan")] = AgentExecutionResult(
        exit_code=0,
        stdout="agent stdout",
        stderr="agent stderr",
        status="success",
        message="done",
        needed_clarifications=["retained detail"],
        files=["plan.md"],
    )
    orchestrator._reconcile_running_tasks()

    assert [call[0] for call in calls] == ["first", "flaky"]
    assert all(call[1:] == ("/issues/ISSUE-ACTION", "ISSUE-ACTION") for call in calls)
    assert jira.transitions == []
    assert orchestrator.state.pending_transitions["ISSUE-ACTION"].next_action_index == 1
    assert "ISSUE-ACTION" not in orchestrator.state.running

    orchestrator._reconcile_pending_transitions()

    assert [call[0] for call in calls] == ["first", "flaky", "flaky", "last"]
    assert all(result is phase_results[0] for result in phase_results)
    assert phase_results[0].phase_name == "plan"
    assert phase_results[0].agent_name == "planner"
    assert phase_results[0].agent_config is orchestrator.agents_config.agents["planner"]
    assert phase_results[0].repository_path == "/issues/ISSUE-ACTION/repository"
    assert phase_results[0].execution.message == "done"
    assert phase_results[0].execution.stdout == "agent stdout"
    assert phase_results[0].execution.stderr == "agent stderr"
    assert phase_results[0].execution.needed_clarifications == ["retained detail"]
    assert phase_results[0].execution.files == ["plan.md"]
    assert jira.comments == [
        ("ISSUE-ACTION", "[agent planner]: done\n\n- retained detail")
    ]
    assert jira.transitions == [("ISSUE-ACTION", "In Review")]
    assert "ISSUE-ACTION" not in orchestrator.state.pending_transitions


def test_jira_transition_retries_without_repeating_actions_or_comments():
    executor = FakeExecutionController()
    jira = FakeJiraClient(transition_result=False)
    action_calls = []
    registry = ActionRegistry(
        [("test:action", lambda phase_result: action_calls.append(phase_result.issue["id"]))]
    )
    config = {
        "phases": {
            "plan": {
                "agent": "planner",
                "transitions": {
                    "success": {
                        "next": "In Review",
                        "do": [{"action": "test:action"}],
                    }
                },
            }
        }
    }
    orchestrator = _build_orchestrator(
        executor,
        config,
        tracker=jira,
        action_registry=registry,
    )
    issue = {"id": "ISSUE-JIRA", "identifier": "ISSUE-JIRA", "title": "Jira retry"}

    orchestrator._dispatch_phase(issue, "/issues/ISSUE-JIRA", "plan")
    executor.completions[("ISSUE-JIRA", "plan")] = AgentExecutionResult(
        exit_code=0,
        stdout="",
        stderr="",
        message="complete",
    )
    orchestrator._reconcile_running_tasks()
    jira.transition_result = True
    orchestrator._reconcile_pending_transitions()

    assert action_calls == ["ISSUE-JIRA"]
    assert jira.comments == [("ISSUE-JIRA", "[agent planner]: complete")]
    assert jira.transitions == [
        ("ISSUE-JIRA", "In Review"),
        ("ISSUE-JIRA", "In Review"),
    ]
    assert "ISSUE-JIRA" not in orchestrator.state.pending_transitions


def test_pending_blocked_transition_stays_visible_and_is_not_redispatched():
    executor = FakeExecutionController()
    issue = {
        "id": "ISSUE-BLOCKED-ACTION",
        "identifier": "ISSUE-BLOCKED-ACTION",
        "title": "Blocked action",
        "state": "To Do",
        "labels": ["AI"],
    }
    jira = FakeJiraClient(issues=[issue])

    def fail_action(_phase_result):
        raise RuntimeError("still unavailable")

    registry = ActionRegistry([("test:fail", fail_action)])
    config = {
        "tracker": {"required_labels": ["AI"]},
        "phases": {
            "plan": {
                "agent": "planner",
                "states": ["To Do"],
                "transitions": {
                    "blocked": {
                        "next": "Clarification Needed",
                        "do": [{"action": "test:fail"}],
                    }
                },
            }
        },
    }
    orchestrator = _build_orchestrator(
        executor,
        config,
        tracker=jira,
        action_registry=registry,
    )

    orchestrator._tick()
    executor.completions[("ISSUE-BLOCKED-ACTION", "plan")] = AgentExecutionResult(
        exit_code=0,
        stdout="",
        stderr="",
        status="blocked",
        message="Need input",
    )
    orchestrator._reconcile_running_tasks()
    orchestrator._tick()

    assert "ISSUE-BLOCKED-ACTION" in orchestrator.state.blocked
    assert "ISSUE-BLOCKED-ACTION" in orchestrator.state.pending_transitions
    assert len(orchestrator.state.running) == 0
    assert executor.starts == [("ISSUE-BLOCKED-ACTION", "plan", "planner")]
    assert jira.transitions == []
