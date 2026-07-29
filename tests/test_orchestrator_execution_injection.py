from dataclasses import dataclass

from app.core.orchestrator import SymphonyOrchestrator
from app.models.agent_config import AgentConfig, AgentsRegistry
from app.services.agent import AgentExecutionResult


class FakeJiraClient:
    def __init__(self, issues=None):
        self.issues = issues or []
        self.comments = []
        self.transitions = []

    def fetch_candidate_issues(self, _active_states):
        return self.issues

    def transition_issue(self, issue_key: str, target_status_name: str) -> bool:
        self.transitions.append((issue_key, target_status_name))
        return True

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
    def __init__(self):
        self.starts = []
        self.completions = {}

    def start_execution(self, request):
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



def test_reconcile_advances_to_next_phase_on_success():
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
        ("ISSUE-2", "implement", "implementer"),
    ]
    assert jira.transitions == [("ISSUE-2", "In Progress")]
    assert "ISSUE-2" in orchestrator.state.running
    assert orchestrator.state.running["ISSUE-2"]["metadata"].current_phase == "implement"



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


def test_reconcile_structured_success_extracts_outputs_and_advances(tmp_path):
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
    assert jira.transitions == [("ISSUE-10", "In Progress")]
    assert fake_executor.starts == [
        ("ISSUE-10", "plan", "planner"),
        ("ISSUE-10", "implement", "implementer"),
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
