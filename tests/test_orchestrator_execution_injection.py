from dataclasses import dataclass

from app.core.orchestrator import SymphonyOrchestrator
from app.models.agent_config import AgentConfig, AgentsRegistry
from app.services.agent import AgentExecutionResult


class FakeJiraClient:
    def __init__(self, issues=None):
        self.issues = issues or []

    def fetch_candidate_issues(self, _active_states):
        return self.issues


class FakeBitbucketService:
    def prepare_workspace(self, _identifier):
        return "/fake/workspace"


@dataclass
class FakeExecutionHandle:
    issue_id: str
    phase_name: str


class FakeExecutionController:
    def __init__(self):
        self.starts = []
        self.completions = {}

    def start_execution(self, request):
        phase_name = _phase_name_from_stdin(request.agent_config.stdin)
        handle = FakeExecutionHandle(issue_id=request.issue["id"], phase_name=phase_name)
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



def _build_orchestrator(fake_executor, workflow_config, issues=None, issue_writer=None):
    agents_registry = AgentsRegistry(
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
        jira_client=FakeJiraClient(issues=issues),
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
    config = {
        "phases": {
            "plan": {"agent": "planner"},
            "implement": {"agent": "implementer"},
        }
    }
    orchestrator = _build_orchestrator(fake_executor, config)
    issue = {"id": "ISSUE-2", "identifier": "ISSUE-2", "title": "Test"}

    orchestrator._dispatch_phase(issue, "/fake/workspace", "plan")
    fake_executor.completions[("ISSUE-2", "plan")] = AgentExecutionResult(
        exit_code=0,
        stdout="plan output",
        stderr="",
    )

    orchestrator._reconcile_running_tasks()

    assert fake_executor.starts == [
        ("ISSUE-2", "plan", "planner"),
        ("ISSUE-2", "implement", "implementer"),
    ]
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
