import subprocess
import logging

logger = logging.getLogger("symphony.agent")

class AgentRunner:
    def spawn_worker(self, issue: dict, workspace_path: str) -> subprocess.Popen:
        """
        Launches the agent workflow adhering to the lifecycle:
        plan -> review -> implement -> test -> PR -> agent_review -> ready -> merge
        """
        identifier = issue.get("identifier")
        logger.info(f"Initializing multi-stage agent pipeline for {identifier}")
        
        # Pass the workflow stages configuration down to the execution script
        cmd = [
            "python", "-m", "your_autonomous_agent",
            "--issue", identifier,
            "--stages", "plan,implement,validate,pr,review"
        ]
        
        process = subprocess.Popen(
            cmd,
            cwd=workspace_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return process