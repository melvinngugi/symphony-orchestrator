import subprocess
import logging
import json
import os

from app.models.agent_config import AgentConfig

logger = logging.getLogger("symphony.agent")

class AgentRunner:
    def spawn_worker(self, agent_name: str, issue: dict, agent_config: AgentConfig, workspace_path: str, stdin_content: str) -> subprocess.Popen:
        """
        Launches an agent process defined by agents.yaml.
        Pipes stdin_content provided by the orchestrator.
        Uses command and args directly from the config.
        """
        if not agent_config.command:
            raise ValueError("No command defined for agent")

        # Prepare arguments, resolving placeholders
        context = {
            "output_file": agent_config.output_file or "",
            "sandbox": "workspace-write" # Default if none provided
        }
        
        # We can also populate context from agent_config more thoroughly
        if hasattr(agent_config, 'sandbox') and agent_config.sandbox:
            context["sandbox"] = agent_config.sandbox

        processed_args = []
        for arg in agent_config.args:
            for key, val in context.items():
                arg = arg.replace("{" + key + "}", str(val))
            processed_args.append(arg)

        full_command = [agent_config.command] + processed_args

        # Prepare environment variables, resolving placeholders
        env = os.environ.copy()
        for key, value in agent_config.env.items():
            # Simple placeholder substitution for environment variables like ${VAR}
            if value.startswith("${") and value.endswith("}"):
                var_name = value[2:-1]
                env[key] = os.getenv(var_name, "")
            else:
                env[key] = value

        logger.info(f"Spawning agent with command: {' '.join(full_command)}")
        
        # Prepare log directory and file for stderr
        log_dir = os.path.join(workspace_path, "log")
        os.makedirs(log_dir, exist_ok=True)
        log_file_path = os.path.join(log_dir, f"{agent_name}.log")
        
        with open(log_file_path, "w") as f:
            f.write(f"{' '.join(full_command)}\n")

        # Open in append mode for the subprocess to write stderr
        stderr_file = open(log_file_path, "a")
        
        process = subprocess.Popen(
            full_command,
            cwd=workspace_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            env=env
        )
        
        # Close the file handle in the parent process; the child keeps its own copy.
        stderr_file.close()

        if stdin_content and process.stdin:
            process.stdin.write(stdin_content)
            process.stdin.close()

        return process
