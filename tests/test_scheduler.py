import json
from datetime import datetime

from app.core import orchestrator as orchestrator_module
from app.core.orchestrator import SymphonyOrchestrator
from app.models.agent_config import AgentConfig, AgentsRegistry
from app.services.actions import ActionRegistry
from app.services.agent import AgentExecutionResult
from app.services.scheduler import JsonScheduledRunStore, latest_daily_run_id


def _scheduled_config(action="test:record"):
    return {
        "phases": {"plan": {"agent": "unused", "states": ["To Do"]}},
        "scheduled_phases": {
            "backlog_curation": {
                "agent": "backlog_curator",
                "daily_at": "02:00",
                "timezone": "Europe/Vienna",
                "audit_issue": "SHOP-CURATION",
                "dry_run": True,
                "input": {
                    "jql": "",
                    "ignore_label": "backlog-curation-ignore",
                    "strategy_pages": {
                        "titles": [],
                        "urls": [],
                        "space_keys": [],
                        "fail_on_missing": True,
                    },
                    "scoring_weights": {
                        "customerImpact": 0.35,
                        "revenueOrCostImpact": 0.25,
                        "strategicAlignment": 0.25,
                        "riskReduction": 0.15,
                    },
                },
                "jira": {
                    "business_value_score_field": "customfield_10001",
                    "business_value_rationale_field": "customfield_10002",
                    "clarification_label": "needs-clarification",
                    "review_label": "backlog-agent-review",
                    "dependency_link_type": "Blocks",
                },
                "confidence": {
                    "business_value": 0.85,
                    "dependency": 0.9,
                    "clarification": 0.9,
                },
                "transitions": {"success": {"do": [{"action": action}]}},
            }
        },
    }


def test_latest_daily_run_uses_latest_due_window():
    assert latest_daily_run_id(
        "curation", "02:00", "Europe/Vienna", datetime(2026, 9, 3, 1, 59)
    ) == "curation:2026-09-02"
    assert latest_daily_run_id(
        "curation", "02:00", "Europe/Vienna", datetime(2026, 9, 3, 2, 0)
    ) == "curation:2026-09-03"


def test_json_schedule_store_persists_completion(tmp_path):
    path = tmp_path / "runs.json"
    store = JsonScheduledRunStore(str(path))
    assert not store.is_complete("curation", "curation:2026-09-03")
    store.mark_complete("curation", "curation:2026-09-03")
    assert JsonScheduledRunStore(str(path)).is_complete(
        "curation", "curation:2026-09-03"
    )


def test_orchestrator_runs_scheduled_phase_without_repository_or_transition(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator_module.settings, "WORKSPACE_ROOT", str(tmp_path))
    starts = []
    action_results = []

    class Tracker:
        def validate_workflow_states(self, _config):
            return None

        def fetch_candidate_issues(self, active_states):
            assert active_states == ["To Do"]
            return []

        def add_comment(self, *_args):
            raise AssertionError("Empty agent messages must not create comments")

        def transition_issue(self, *_args):
            raise AssertionError("Action-only completion must not transition Jira")

    class Bitbucket:
        def prepare_workspace(self, *_args):
            raise AssertionError("Scheduled phases must not clone a repository")

    class InputProvider:
        def build_input(self, schedule_name, run_id, phase_config):
            assert schedule_name == "backlog_curation"
            assert phase_config["audit_issue"] == "SHOP-CURATION"
            return json.dumps({"runId": run_id, "tickets": [{}]}).encode()

    class Executor:
        def start_execution(self, request):
            starts.append(request)
            assert request.workspace_path == request.repository_path
            return object()

        def poll_execution(self, _execution):
            return AgentExecutionResult(0, "", "", files=[], message="")

    registry = ActionRegistry([("test:record", action_results.append)])
    store = JsonScheduledRunStore(str(tmp_path / "ledger.json"))
    orchestrator = SymphonyOrchestrator(
        _scheduled_config(),
        tracker=Tracker(),
        bitbucket_service=Bitbucket(),
        action_registry=registry,
        agents_registry=AgentsRegistry(
            agents={
                "backlog_curator": AgentConfig(
                    command="fake",
                    stdin="backlog-curation-input.json",
                )
            }
        ),
        execution_controller=Executor(),
        scheduled_input_provider=InputProvider(),
        scheduled_run_store=store,
        clock=lambda: datetime(2026, 9, 3, 3, 0),
    )

    orchestrator._tick()
    assert len(starts) == 1
    assert (tmp_path / "scheduled" / "backlog_curation").is_dir()
    orchestrator._reconcile_running_tasks()
    assert len(action_results) == 1
    assert store.is_complete("backlog_curation", "backlog_curation:2026-09-03")

    orchestrator._tick()
    assert len(starts) == 1
