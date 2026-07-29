import time
import logging
from datetime import datetime
import json
from typing import Callable, Optional

from app.models.agent_config import AgentsRegistry
from app.models.state import OrchestratorState, TicketMetadata, ErrorDetail
from app.services.agent import (
    AgentExecutionController,
    AgentExecutionRequest,
    SubprocessAgentExecutionController,
)
from app.services.jira import JiraClient
from app.services.bitbucket import BitbucketService
from app.core.config import load_agents_config

logger = logging.getLogger("symphony.orchestrator")

class SymphonyOrchestrator:
    def __init__(
        self,
        config: dict,
        *,
        agents_registry: Optional[AgentsRegistry] = None,
        execution_controller: Optional[AgentExecutionController] = None,
        jira_client: Optional[JiraClient] = None,
        bitbucket_service: Optional[BitbucketService] = None,
        issue_writer: Optional[Callable[[str, dict], None]] = None,
    ):
        self.config = config
        self.agents_config = agents_registry or load_agents_config()
        self.state = OrchestratorState()
        
        # Instantiate existing services
        self.jira = jira_client or JiraClient()
        self.bitbucket = bitbucket_service or BitbucketService()
        self.execution_controller = execution_controller or SubprocessAgentExecutionController()
        self.issue_writer = issue_writer or self._write_issue_json

        # Apply config overrides
        if "polling" in self.config:
            self.state.poll_interval_ms = self.config["polling"].get("interval_ms", 30000)
        if "agent" in self.config:
            self.state.max_concurrent_agents = self.config["agent"].get("max_concurrent_agents", 10)

    def start(self):
        logger.info("Starting Symphony Orchestrator daemon...")
        while True:
            try:
                self._reconcile_running_tasks()
                self._tick()
            except Exception as e:
                logger.error(f"Error during orchestration tick: {e}")
                self.add_error(f"Orchestration tick failure: {str(e)}")
            time.sleep(self.state.poll_interval_ms / 1000.0)

    def _tick(self):
        """Polls for new work from Jira based on the tracker config."""
        tracker_config = self.config.get("tracker", {})
        active_states = tracker_config.get("active_states", ["To Do"])
        required_labels = tracker_config.get("required_labels", [])
        
        # 1. Fetch candidates from tracker (Jira)
        candidates = self.jira.fetch_candidate_issues(active_states)
        
        for issue in candidates:
            # Respect concurrency limits
            if len(self.state.running) >= self.state.max_concurrent_agents:
                break
                
            issue_id = issue["id"]
            
            # Skip if already being processed OR completed
            if issue_id in self.state.running or issue_id in self.state.completed:
                continue
            
            # Check labels
            labels = issue.get("labels", [])
            if required_labels:
                # Issue must have AT LEAST ONE of the required labels
                if not any(label.lower() in [l.lower() for l in labels] for label in required_labels):
                    continue
                
            # If not already claimed, claim and start dispatch
            if issue_id not in self.state.claimed:
                logger.info(f"Claiming and starting issue {issue.get('identifier')}")
                self._dispatch(issue)

    def add_error(self, message: str):
        """Adds an error to the orchestrator state with a timestamp."""
        error = ErrorDetail(
            message=message,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        self.state.errors.insert(0, error)
        # Keep only the last 50 errors
        if len(self.state.errors) > 50:
            self.state.errors.pop()

    def _reconcile_running_tasks(self):
        """Monitors running agent processes and handles completions or failures."""
        completed_ids = []
        to_dispatch_next = [] # List of (issue_id, next_phase)
        
        for issue_id, task in self.state.running.items():
            metadata = task["metadata"]
            workspace_path = task["workspace_path"]
            issue = task["issue"]
            execution = task["execution"]
            
            result = self.execution_controller.poll_execution(execution)
            if result is not None:
                completed_ids.append(issue_id)

                if result.exit_code != 0:
                    error_msg = f"Agent failure for {metadata.identifier} in phase {metadata.current_phase} (Exit {result.exit_code})"
                    if result.stderr:
                        error_msg += f": {result.stderr.strip()}"
                    self.add_error(error_msg)
                    logger.error(error_msg)
                else:
                    logger.info(f"Agent successfully completed phase {metadata.current_phase} for {metadata.identifier}")

                    next_phase = self._get_next_phase(metadata.current_phase)
                    if next_phase:
                        to_dispatch_next.append((issue, workspace_path, next_phase))
                    else:
                        self.state.completed.add(issue_id)
        
        for issue_id in completed_ids:
            del self.state.running[issue_id]
            
        for issue, workspace_path, next_phase in to_dispatch_next:
            self._dispatch_phase(issue, workspace_path, next_phase)

    def _get_next_phase(self, current_phase: str):
        phases = list(self.config.get("phases", {}).keys())
        try:
            current_index = phases.index(current_phase)
            if current_index + 1 < len(phases):
                return phases[current_index + 1]
        except ValueError:
            pass
        return None

    def _dispatch_phase(self, issue: dict, workspace_path: str, phase_name: str):
        issue_id = issue["id"]
        identifier = issue.get("identifier")
        
        phase_config = self.config.get("phases", {}).get(phase_name)
        if not phase_config:
            logger.error(f"Phase {phase_name} not found in config")
            return

        agent_name = phase_config.get("agent")
        agent_config = self.agents_config.agents.get(agent_name)
        if not agent_config:
            logger.error(f"Agent {agent_name} not found in agents.yaml")
            return

        logger.info(f"Dispatching phase {phase_name} for {identifier} using agent {agent_name}")
        execution = self.execution_controller.start_execution(
            AgentExecutionRequest(
                agent_name=agent_name,
                issue=issue,
                agent_config=agent_config,
                workspace_path=workspace_path,
            )
        )
        
        if issue_id in self.state.claimed:
            metadata = self.state.claimed[issue_id]
        else:
            metadata = TicketMetadata(
                identifier=identifier,
                title=issue.get("title", "Untitled"),
                started_at=time.time()
            )
            self.state.claimed[issue_id] = metadata
            
        metadata.current_phase = phase_name
        
        self.state.running[issue_id] = {
            "execution": execution,
            "metadata": metadata,
            "workspace_path": workspace_path,
            "issue": issue
        }

    def _dispatch(self, issue: dict):
        identifier = issue.get("identifier")
        workspace_path = self.bitbucket.prepare_workspace(identifier)

        try:
            self.issue_writer(workspace_path, issue)
        except Exception as e:
            logger.error(f"Failed to create issue.json: {e}")

        # Start with the first phase
        phases = list(self.config.get("phases", {}).keys())
        if phases:
            self._dispatch_phase(issue, workspace_path, phases[0])
        else:
            logger.error("No phases defined in WORKFLOW.md")

    def _write_issue_json(self, workspace_path: str, issue: dict):
        issue_json_path = f"{workspace_path}/issue.json"
        with open(issue_json_path, "w") as f:
            json.dump(issue, f, indent=2)
        logger.info(f"Created issue.json at {issue_json_path}")
