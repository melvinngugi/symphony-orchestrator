import time
import logging
from datetime import datetime
import json
from typing import Callable, Optional

from app.models.agent_config import AgentsRegistry
from app.models.state import (
    OrchestratorState,
    TicketMetadata,
    ErrorDetail,
    BlockedTicketDetail,
    PendingTransitionDetail,
)
from app.models.workspace import repository_path
from app.services.agent import (
    AgentExecutionController,
    AgentExecutionRequest,
    SubprocessAgentExecutionController,
)
from app.services.bitbucket import BitbucketService
from app.services.actions import ActionResolver, PhaseResult
from app.services.tracker import TrackerAdapter
from app.core.config import load_agents_config
from app.core.workflow_validation import (
    validate_workflow_config,
)

logger = logging.getLogger("symphony.orchestrator")

class SymphonyOrchestrator:
    def __init__(
        self,
        config: dict,
        *,
        tracker: TrackerAdapter,
        bitbucket_service: BitbucketService,
        action_registry: ActionResolver,
        agents_registry: Optional[AgentsRegistry] = None,
        execution_controller: Optional[AgentExecutionController] = None,
        issue_writer: Optional[Callable[[str, dict], None]] = None,
    ):
        self.config = config
        self.agents_config = agents_registry or load_agents_config()
        self.state = OrchestratorState()
        
        # Instantiate existing services
        self.tracker = tracker
        self.bitbucket = bitbucket_service
        self.execution_controller = execution_controller or SubprocessAgentExecutionController()
        self.issue_writer = issue_writer or self._write_issue_json
        self.action_registry = action_registry
        self.completion_transitions = validate_workflow_config(
            self.config,
            self.action_registry,
        )
        self.tracker.validate_workflow_states(self.config)

        # Apply config overrides
        if "polling" in self.config:
            self.state.poll_interval_ms = self.config["polling"].get("interval_ms", 30000)
        if "agent" in self.config:
            self.state.max_concurrent_agents = self.config["agent"].get("max_concurrent_agents", 10)

    def start(self):
        logger.info("Starting Symphony Orchestrator daemon...")
        round=0
        while True:
            round=round+1
            try:
                logger.debug(f"Tick round {round}")
                self._reconcile_pending_transitions()
                self._reconcile_running_tasks()
                self._tick()
            except Exception as e:
                logger.error(f"Error during orchestration tick: {e}")
                self.add_error(f"Orchestration tick failure: {str(e)}")
            time.sleep(self.state.poll_interval_ms / 1000.0)

    def _tick(self):
        """Poll the tracker and dispatch each issue to the phase matching its state."""
        tracker_config = self.config.get("tracker", {})
        required_labels = tracker_config.get("required_labels", [])

        active_states = self._configured_phase_states()
        if not active_states:
            logger.error("No tracker states defined under phases.*.states in WORKFLOW.md")
            return

        # 1. Fetch candidates from the configured tracker.
        candidates = self.tracker.fetch_candidate_issues(active_states)
        logger.debug(f"Found {len(candidates)} candidate issues ({[issue["identifier"] for issue in candidates]})")
        
        for issue in candidates:
            # Respect concurrency limits
            if len(self.state.running) >= self.state.max_concurrent_agents:
                logger.debug(f"Max number of agents running already, skipping turn")
                break
                
            issue_id = issue["id"]
            issue_key = issue["identifier"]
            
            # Skip if already being processed OR completed
            if (
                issue_id in self.state.running
                or issue_id in self.state.completed
                or issue_id in self.state.pending_transitions
            ):
                logger.debug(f"Skipping {issue_key} in state {issue.get("state")}")
                continue
            
            # Check labels
            labels = issue.get("labels", [])
            if required_labels:
                # Issue must have AT LEAST ONE of the required labels
                if not any(label.lower() in [l.lower() for l in labels] for label in required_labels):
                    logger.debug(f"Skipping {issue_key} as it is missing the required labels")
                    continue
                
            phase_name = self._phase_for_issue_state(issue.get("state"))
            if not phase_name:
                logger.debug(f"Skipping {issue_key} in state {issue.get("state")}")
                continue

            # Do not execute the same phase repeatedly while the tracker still reports
            # the state that triggered the completed execution.
            metadata = self.state.claimed.get(issue_id)
            if metadata and metadata.current_phase == phase_name:
                logger.debug(f"Skipping {issue_key} as already claimed")
                continue

            logger.info(
                f"Claiming issue {issue.get('identifier')} in tracker state "
                f"{issue.get('state')} for phase {phase_name}"
            )
            self._dispatch(issue, phase_name)

    def _configured_phase_states(self) -> list[str]:
        """Return the unique tracker states configured by phases, in workflow order."""
        states: list[str] = []
        seen: set[str] = set()
        for phase_config in self.config.get("phases", {}).values():
            configured_states = phase_config.get("states", []) if isinstance(phase_config, dict) else []
            if not isinstance(configured_states, list):
                continue
            for state in configured_states:
                if not isinstance(state, str) or not state.strip():
                    continue
                normalized = state.strip().casefold()
                if normalized not in seen:
                    states.append(state.strip())
                    seen.add(normalized)
        return states

    def _phase_for_issue_state(self, issue_state: object) -> Optional[str]:
        """Resolve a normalized tracker state to the first matching workflow phase."""
        if not isinstance(issue_state, str) or not issue_state.strip():
            return None

        normalized_issue_state = issue_state.strip().casefold()
        for phase_name, phase_config in self.config.get("phases", {}).items():
            configured_states = phase_config.get("states", []) if isinstance(phase_config, dict) else []
            if not isinstance(configured_states, list):
                continue
            if any(
                isinstance(state, str) and state.strip().casefold() == normalized_issue_state
                for state in configured_states
            ):
                return phase_name
        return None

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
                    logger.info(f"Agent completed phase {metadata.current_phase} for {metadata.identifier}: result={result.status}")
                    if result.status == "blocked":
                        self.state.blocked[issue_id] = BlockedTicketDetail(
                            identifier=metadata.identifier,
                            title=metadata.title,
                            current_phase=metadata.current_phase,
                            blocked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            message=result.message or "Blocked by agent result",
                            needed_clarifications=result.needed_clarifications or [],
                        )
                    elif result.status != "success":
                        error_msg = (
                            f"Unsupported agent status for {metadata.identifier} in phase "
                            f"{metadata.current_phase}: {result.status}"
                        )
                        self.add_error(error_msg)
                        logger.error(error_msg)
                        continue

                    agent_name = self._agent_name_for_phase(metadata.current_phase)
                    agent_config = self.agents_config.agents[agent_name]
                    self._transition_for_phase_status(
                        PhaseResult(
                            issue=issue,
                            workspace_path=workspace_path,
                            repository_path=repository_path(workspace_path),
                            phase_name=metadata.current_phase,
                            agent_name=agent_name,
                            agent_config=agent_config,
                            execution=result,
                        )
                    )


        for issue_id in completed_ids:
            del self.state.running[issue_id]

    def _dispatch_phase(self, issue: dict, workspace_path: str, phase_name: str) -> bool:
        issue_id = issue["id"]
        identifier = issue.get("identifier")
        
        phase_config = self.config.get("phases", {}).get(phase_name)
        if not phase_config:
            logger.error(f"Phase {phase_name} not found in config")
            return False

        agent_name = phase_config.get("agent")
        agent_config = self.agents_config.agents.get(agent_name)
        if not agent_config:
            logger.error(f"Agent {agent_name} not found in agents.yaml")
            return False

        if not self._transition_on_phase_start(issue, phase_name):
            return False

        logger.info(f"Dispatching phase {phase_name} for {identifier} using agent {agent_name}")
        execution = self.execution_controller.start_execution(
            AgentExecutionRequest(
                agent_name=agent_name,
                issue=issue,
                agent_config=agent_config,
                workspace_path=workspace_path,
                repository_path=repository_path(workspace_path),
            )
        )
        
        if issue_id in self.state.claimed:
            metadata = self.state.claimed[issue_id]
        else:
            metadata = TicketMetadata(
                identifier=identifier,
                title=issue.get("title", "Untitled"),
                started_at=time.time(),
                workspace_path=workspace_path,
            )
            self.state.claimed[issue_id] = metadata

        if not metadata.workspace_path:
            metadata.workspace_path = workspace_path
            
        metadata.current_phase = phase_name
        self.state.blocked.pop(issue_id, None)
        
        self.state.running[issue_id] = {
            "execution": execution,
            "metadata": metadata,
            "workspace_path": workspace_path,
            "issue": issue
        }
        return True

    def _dispatch(self, issue: dict, phase_name: str):
        identifier = issue.get("identifier")
        metadata = self.state.claimed.get(issue["id"])
        workspace_path = metadata.workspace_path if metadata and metadata.workspace_path else None
        if not workspace_path:
            workspace_path = self.bitbucket.prepare_workspace(identifier)

        try:
            self.issue_writer(workspace_path, issue)
        except Exception as e:
            logger.error(f"Failed to create issue.json: {e}")

        self._dispatch_phase(issue, workspace_path, phase_name)

    def _write_issue_json(self, workspace_path: str, issue: dict):
        issue_json_path = f"{workspace_path}/issue.json"
        with open(issue_json_path, "w") as f:
            json.dump(issue, f, indent=2)
        logger.info(f"Created issue.json at {issue_json_path}")

    def _transition_for_phase_status(self, phase_result: PhaseResult):
        transition = self.completion_transitions.get(
            (phase_result.phase_name, phase_result.execution.status)
        )
        if transition is None:
            return

        issue_id = phase_result.issue.get("id")
        issue_key = phase_result.issue.get("identifier")
        if not issue_id or not issue_key:
            error_msg = (
                "Cannot complete transition for issue without id or identifier in phase "
                f"{phase_result.phase_name}"
            )
            self.add_error(error_msg)
            logger.error(error_msg)
            return

        self.state.pending_transitions[issue_id] = PendingTransitionDetail(
            phase_result=phase_result,
            target_state=transition.next_state,
            actions=list(transition.actions),
        )
        self._reconcile_pending_transition(issue_id)

    def _reconcile_pending_transitions(self) -> None:
        for issue_id in list(self.state.pending_transitions):
            self._reconcile_pending_transition(issue_id)

    def _reconcile_pending_transition(self, issue_id: str) -> None:
        pending = self.state.pending_transitions.get(issue_id)
        if pending is None:
            return

        phase_result = pending.phase_result
        issue_key = phase_result.issue.get("identifier")
        while pending.next_action_index < len(pending.actions):
            action_name = pending.actions[pending.next_action_index]
            try:
                action = self.action_registry.resolve(action_name)
                action(phase_result)
            except Exception as e:
                error_msg = (
                    f"Failed transition action '{action_name}' for {issue_key} in phase "
                    f"{phase_result.phase_name}: {e}"
                )
                self.add_error(error_msg)
                logger.error(error_msg)
                return
            pending.next_action_index += 1

        if not pending.comment_attempted:
            pending.comment_attempted = True
            comment_body = self._build_agent_comment(
                phase_result.agent_name,
                phase_result.execution.message,
                phase_result.execution.needed_clarifications or [],
            )
            if comment_body:
                try:
                    self.tracker.add_comment(issue_key, comment_body)
                except Exception as e:
                    logger.error(
                        f"Failed tracker comment for {issue_key} in phase "
                        f"{phase_result.phase_name}: {e}"
                    )

        try:
            transitioned = self.tracker.transition_issue(issue_key, pending.target_state)
        except Exception as e:
            error_msg = (
                f"Failed tracker transition for {issue_key} on status "
                f"'{phase_result.execution.status}' in phase "
                f"{phase_result.phase_name}: {e}"
            )
            self.add_error(error_msg)
            logger.error(error_msg)
            return

        if not transitioned:
            error_msg = (
                f"Failed tracker transition for {issue_key} on status "
                f"'{phase_result.execution.status}' in phase "
                f"{phase_result.phase_name} to '{pending.target_state}'"
            )
            self.add_error(error_msg)
            logger.error(error_msg)
            return

        del self.state.pending_transitions[issue_id]

    def _transition_on_phase_start(self, issue: dict, phase_name: str) -> bool:
        state_name = self._target_state_for_phase_transition(phase_name, "on_start")
        if not state_name:
            return True

        issue_key = issue.get("identifier")
        if not issue_key:
            error_msg = f"Cannot apply on_start transition for issue without identifier in phase {phase_name}"
            self.add_error(error_msg)
            logger.error(error_msg)
            return False

        try:
            transitioned = self.tracker.transition_issue(issue_key, state_name)
        except Exception as e:
            error_msg = (
                f"Failed tracker on_start transition for {issue_key} in phase "
                f"{phase_name} to '{state_name}': {e}"
            )
            self.add_error(error_msg)
            logger.error(error_msg)
            return False

        if not transitioned:
            error_msg = (
                f"Failed tracker on_start transition for {issue_key} in phase "
                f"{phase_name} to '{state_name}'"
            )
            self.add_error(error_msg)
            logger.error(error_msg)
            return False

        return True

    def _target_state_for_phase_transition(self, phase_name: str, transition: str) -> Optional[str]:
        if transition in ("success", "blocked"):
            completion = self.completion_transitions.get((phase_name, transition))
            return completion.next_state if completion else None

        phase_config = self.config.get("phases", {}).get(phase_name, {})
        transitions_cfg = phase_config.get("transitions")
        if not isinstance(transitions_cfg, dict):
            return None

        target = transitions_cfg.get(transition)
        return target.strip() if isinstance(target, str) and target.strip() else None

    def _structured_target_state_for_phase(self, phase_name: str, status: str) -> Optional[str]:
        """Compatibility wrapper for phase completion transition lookup."""
        return self._target_state_for_phase_transition(phase_name, status)

    def _agent_name_for_phase(self, phase_name: str) -> str:
        phase_config = self.config.get("phases", {}).get(phase_name, {})
        agent_name = phase_config.get("agent")
        return agent_name if isinstance(agent_name, str) and agent_name.strip() else "unknown"

    def _build_agent_comment(self, agent_name: str, message: str, needed_clarifications: list[str]) -> str:
        clean_message = message.strip() if isinstance(message, str) else ""
        clean_clarifications = [str(item).strip() for item in needed_clarifications if str(item).strip()]

        if not clean_message and not clean_clarifications:
            return ""

        comment_lines = [f"[agent {agent_name}]: {clean_message}" if clean_message else f"[agent {agent_name}]: "]
        if clean_clarifications:
            comment_lines.append("")
            comment_lines.extend(f"- {clarification}" for clarification in clean_clarifications)

        return "\n".join(comment_lines)
