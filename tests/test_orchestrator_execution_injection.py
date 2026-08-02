from dataclasses import dataclass

from app.core.orchestrator import SymphonyOrchestrator
from app.models.agent_config import AgentConfig, AgentsRegistry
from app.services.agent import AgentExecutionResult


class FakeJiraClient:
    def __init__(self, issues=None, transition_result=True, transition_error=None, events=None):
        self.issues = issues or []
        self.transition_result = transition_result
        self.transition_error = transition_error
        self.events = events
        self.requested_states = []
        self.comments = []
        self.transitions = []

    def fetch_candidate_issues(self, active_states):
        self.requested_states.append(active_states)
        return self.issues

    def transition_issue(self, issue_key: str, target_status_name: str) -> bool:
        self.transitions.append((issue_key, target_status_name))
        if self.events is not None:
            self.events.append(("transition", issue_key, target_status_name))
        if self.transition_error is not None:
            raise self.transition_error
        return self.transition_result

    def add_comment(self, issue_key: str, body: str) -> bool:
        self.comments.append((issue_key, body))
        return True


class FakeBitbucketService:
    def prepare_workspace(self, _identifier):
        return "/fake/workspace"


@dataclass
class FakeExecutionHandle:
    issue_id: str
    phase_name: str
    structured_output_file: str | None = None


class FakeExecutionController:
    def __init__(self, events=None):
        self.events = events
        self.starts = []
        self.completions = {}

    def start_execution(self, request):
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
    jira_client=None,
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

    return SymphonyOrchestrator(
        workflow_config,
        agents_registry=agents_registry,
        execution_controller=fake_executor,
        jira_client=jira_client or FakeJiraClient(issues=issues),
        bitbucket_service=FakeBitbucketService(),
        issue_writer=issue_writer or (lambda _workspace, _issue: None),
    )



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
    orchestrator = _build_orchestrator(fake_executor, config, jira_client=jira)
    issue = {"id": "ISSUE-START", "identifier": "ISSUE-START", "title": "Test"}

    dispatched = orchestrator._dispatch_phase(issue, "/fake/workspace", "plan")

    assert dispatched is True
    assert events == [
        ("transition", "ISSUE-START", "In Progress"),
        ("start", "ISSUE-START", "planner"),
    ]
    assert jira.comments == []


def test_dispatch_phase_ignores_absent_or_invalid_on_start_transition():
    transition_configs = [
        {},
        {"on_start": ""},
        {"on_start": "   "},
        {"on_start": 123},
    ]

    for index, transitions in enumerate(transition_configs):
        fake_executor = FakeExecutionController()
        jira = FakeJiraClient()
        config = {
            "phases": {
                "plan": {
                    "agent": "planner",
                    "transitions": transitions,
                },
            }
        }
        orchestrator = _build_orchestrator(fake_executor, config, jira_client=jira)
        issue_id = f"ISSUE-NO-START-{index}"
        issue = {"id": issue_id, "identifier": issue_id, "title": "Test"}

        assert orchestrator._dispatch_phase(issue, "/fake/workspace", "plan") is True
        assert jira.transitions == []
        assert fake_executor.starts == [(issue_id, "plan", "planner")]


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
    orchestrator = _build_orchestrator(fake_executor, config, jira_client=jira)
    issue = {"id": "ISSUE-FAILED-START", "identifier": "ISSUE-FAILED-START", "title": "Test"}

    dispatched = orchestrator._dispatch_phase(issue, "/fake/workspace", "plan")

    assert dispatched is False
    assert fake_executor.starts == []
    assert "ISSUE-FAILED-START" not in orchestrator.state.running
    assert "ISSUE-FAILED-START" not in orchestrator.state.claimed
    assert "Failed Jira on_start transition" in orchestrator.state.errors[0].message


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
    orchestrator = _build_orchestrator(fake_executor, config, jira_client=jira)
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
    orchestrator = _build_orchestrator(fake_executor, config, jira_client=jira)
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
    orchestrator = _build_orchestrator(fake_executor, config, jira_client=jira)

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
    orchestrator = _build_orchestrator(fake_executor, config, jira_client=jira)
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
        jira_client=jira,
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
    orchestrator = _build_orchestrator(fake_executor, config, jira_client=jira)

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
    orchestrator = _build_orchestrator(fake_executor, config, jira_client=jira)

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
        jira_client=jira,
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
