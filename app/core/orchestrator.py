import time
import logging
from datetime import datetime
import os
import json
from app.models.state import OrchestratorState, TicketMetadata, ErrorDetail
from app.services.agent import AgentRunner
from app.services.jira import JiraClient
from app.services.bitbucket import BitbucketService
from app.core.config import load_agents_config

logger = logging.getLogger("symphony.orchestrator")

class SymphonyOrchestrator:
    def __init__(self, config: dict):
        self.config = config
        self.agents_config = load_agents_config()
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
            handle = task["handle"]
            metadata = task["metadata"]
            workspace_path = task["workspace_path"]
            issue = task["issue"]
            
            exit_code = handle.poll()
            if exit_code is not None:
                completed_ids.append(issue_id)
                stdout_content, _ = handle.communicate()
                
                # Retrieve stderr from the log file (skipping the first line which is the command)
                stderr_content = ""
                phase_config = self.config.get("phases", {}).get(metadata.current_phase)
                if phase_config:
                    agent_name = phase_config.get("agent")
                    log_file_path = os.path.join(workspace_path, "log", f"{agent_name}.log")
                    if os.path.exists(log_file_path):
                        try:
                            with open(log_file_path, "r") as f:
                                # Skip command line
                                f.readline()
                                stderr_content = f.read()
                        except Exception as e:
                            logger.error(f"Failed to read stderr from {log_file_path}: {e}")

                if exit_code != 0:
                    error_msg = f"Agent failure for {metadata.identifier} in phase {metadata.current_phase} (Exit {exit_code})"
                    if stderr_content:
                        error_msg += f": {stderr_content.strip()}"
                    self.add_error(error_msg)
                    logger.error(error_msg)
                else:
                    logger.info(f"Agent successfully completed phase {metadata.current_phase} for {metadata.identifier}")
                    
                    # Orchestrator writes the output file from stdout
                    phase_config = self.config.get("phases", {}).get(metadata.current_phase)
                    if phase_config:
                        agent_name = phase_config.get("agent")
                        agent_config = self.agents_config.agents.get(agent_name)
                        if agent_config and agent_config.output_file:
                            output_path = os.path.join(workspace_path, agent_config.output_file)
                            try:
                                with open(output_path, "w") as f:
                                    f.write(stdout_content)
                                logger.info(f"Wrote agent output to {output_path}")
                            except Exception as e:
                                logger.error(f"Failed to write output file {output_path}: {e}")

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
        
        # Prepare stdin content from the file specified in agent_config.stdin
        stdin_content = ""
        stdin_file = agent_config.stdin
        
        if stdin_file:
            stdin_file_path = os.path.join(workspace_path, stdin_file)
            if os.path.exists(stdin_file_path):
                try:
                    with open(stdin_file_path, 'r') as f:
                        stdin_content = f.read()
                    logger.info(f"Read stdin content from {stdin_file_path}")
                except Exception as e:
                    logger.error(f"Failed to read stdin file {stdin_file_path}: {e}")
            else:
                logger.warning(f"Stdin file {stdin_file_path} not found for phase {phase_name}")

        worker_handle = self.runner.spawn_worker(agent_name, issue, agent_config, workspace_path, stdin_content)
        
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
            "handle": worker_handle,
            "metadata": metadata,
            "workspace_path": workspace_path,
            "issue": issue
        }

    def _dispatch(self, issue: dict):
        identifier = issue.get("identifier")
        workspace_path = self.bitbucket.prepare_workspace(identifier)
        
        # Create issue.json in the workspace
        issue_json_path = os.path.join(workspace_path, "issue.json")
        try:
            with open(issue_json_path, "w") as f:
                json.dump(issue, f, indent=2)
            logger.info(f"Created issue.json at {issue_json_path}")
        except Exception as e:
            logger.error(f"Failed to create issue.json: {e}")

        # Start with the first phase
        phases = list(self.config.get("phases", {}).keys())
        if phases:
            self._dispatch_phase(issue, workspace_path, phases[0])
        else:
            logger.error("No phases defined in WORKFLOW.md")
