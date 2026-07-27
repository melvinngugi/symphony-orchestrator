import time
import logging
from datetime import datetime
from app.models.state import OrchestratorState, TicketMetadata, ErrorDetail
from app.services.agent import AgentRunner
from app.services.jira import JiraClient
from app.services.bitbucket import BitbucketService

logger = logging.getLogger("symphony.orchestrator")

class SymphonyOrchestrator:
    def __init__(self, config: dict):
        self.config = config
        self.state = OrchestratorState()
        
        # Instantiate existing services
        self.jira = JiraClient()
        self.bitbucket = BitbucketService()
        self.runner = AgentRunner()

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
        for issue_id, task in self.state.running.items():
            handle = task["handle"]
            metadata = task["metadata"]
            
            exit_code = handle.poll()
            if exit_code is not None:
                completed_ids.append(issue_id)
                if exit_code != 0:
                    stderr_content = ""
                    if handle.stderr:
                        stderr_content = handle.stderr.read()
                    error_msg = f"Agent failure for {metadata.identifier} (Exit {exit_code})"
                    if stderr_content:
                        error_msg += f": {stderr_content.strip()}"
                    self.add_error(error_msg)
                    logger.error(error_msg)
                else:
                    logger.info(f"Agent successfully completed task {metadata.identifier}")
                    self.state.completed.add(issue_id)
        
        for issue_id in completed_ids:
            del self.state.running[issue_id]

    def _tick(self):
        """Main reconciliation and dispatch loop."""
        active_states = self.config.get("tracker", {}).get("active_states", [])
        
        # 1. Fetch using JiraClient's method name
        candidates = self.jira.fetch_candidate_issues(active_states)
        print(f"Fetched candidates from Jira: {candidates}")
        if not candidates:
            return

        # 2. Sort candidates by priority and creation time
        candidates.sort(key=lambda x: (x.get('priority', 999), x.get('created_at', '')))

        # 3. Evaluate and dispatch eligible issues
        for issue in candidates:
            if len(self.state.running) >= self.state.max_concurrent_agents:
                break
                
            if self._should_dispatch(issue):
                self._dispatch(issue)

    def _should_dispatch(self, issue: dict) -> bool:
        """Validates if a Jira ticket meets all criteria (including the 'AI' label) for agent pickup."""
        issue_id = issue["id"]
        
        if issue_id in self.state.claimed or issue_id in self.state.running:
            return False
            
        # Extract required labels from WORKFLOW.md and normalize them to lowercase
        configured_labels = self.config.get("tracker", {}).get("required_labels", ["AI"])
        required_labels = set(label.strip().lower() for label in configured_labels)
        
        issue_labels = set(issue.get("labels", []))
        
        if not required_labels.issubset(issue_labels):
            return False
            
        return True

    def _dispatch(self, issue: dict):
        issue_id = issue["id"]
        identifier = issue.get("identifier")
        title = issue.get("title", "Untitled")
        logger.info(f"Dispatching autonomous workflow for {identifier}")
        
        # Prepares workspace and clones repository via BitbucketService
        workspace_path = self.bitbucket.prepare_workspace(identifier)
        
        # Spawn the agent subprocess
        worker_handle = self.runner.spawn_worker(issue, workspace_path)
        
        ticket_info = TicketMetadata(
            identifier=identifier,
            title=title,
            started_at=time.time()
        )
        
        self.state.running[issue_id] = {
            "handle": worker_handle,
            "metadata": ticket_info
        }
        self.state.claimed[issue_id] = ticket_info