import subprocess
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional, Protocol

from app.models.agent_config import AgentConfig

logger = logging.getLogger("symphony.agent")

@dataclass
class AgentExecutionRequest:
    agent_name: str
    issue: dict
    agent_config: AgentConfig
    workspace_path: str


@dataclass
class RunningAgentExecution:
    agent_name: str
    workspace_path: str
    output_file: Optional[str]
    process: subprocess.Popen


@dataclass
class AgentExecutionResult:
    exit_code: int
    stdout: str
    stderr: str


class AgentExecutionController(Protocol):
    def start_execution(self, request: AgentExecutionRequest) -> RunningAgentExecution:
        """Starts an agent execution and returns a handle for polling."""

    def poll_execution(self, execution: RunningAgentExecution) -> Optional[AgentExecutionResult]:
        """Returns completion result when finished, otherwise None."""


class SubprocessAgentExecutionController:
    _ENV_VAR_PATTERN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

    def start_execution(self, request: AgentExecutionRequest) -> RunningAgentExecution:
        if not request.agent_config.command:
            raise ValueError("No command defined for agent")

        env = self._build_env(request.agent_config)
        full_command = self._build_command(request.agent_config, env, request.agent_name)
        stdin_content = self._load_stdin_content(request.workspace_path, request.agent_config.stdin)

        logger.info(f"Spawning agent with command: {' '.join(full_command)}")

        log_file_path = self._log_file_path(request.workspace_path, request.agent_name)
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        with open(log_file_path, "w") as f:
            f.write(f"{' '.join(full_command)}\n")

        stderr_file = open(log_file_path, "a")
        process = subprocess.Popen(
            full_command,
            cwd=request.workspace_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            env=env,
        )
        stderr_file.close()

        if stdin_content and process.stdin:
            process.stdin.write(stdin_content)
            process.stdin.close()

        return RunningAgentExecution(
            agent_name=request.agent_name,
            workspace_path=request.workspace_path,
            output_file=request.agent_config.output_file,
            process=process,
        )

    def poll_execution(self, execution: RunningAgentExecution) -> Optional[AgentExecutionResult]:
        exit_code = execution.process.poll()
        if exit_code is None:
            return None

        stdout_content, _ = execution.process.communicate()
        stderr_content = self._read_stderr_content(execution.workspace_path, execution.agent_name)

        if exit_code == 0 and execution.output_file:
            output_path = os.path.join(execution.workspace_path, execution.output_file)
            try:
                with open(output_path, "w") as f:
                    f.write(stdout_content)
                logger.info(f"Wrote agent output to {output_path}")
            except Exception as e:
                logger.error(f"Failed to write output file {output_path}: {e}")

        return AgentExecutionResult(
            exit_code=exit_code,
            stdout=stdout_content,
            stderr=stderr_content,
        )

    def _build_command(self, agent_config: AgentConfig, env: dict[str, str], agent_name: str) -> list[str]:
        context = {
            "output_file": agent_config.output_file or "",
            "sandbox": agent_config.sandbox or "workspace-write",
        }

        processed_args = []
        for arg in agent_config.args:
            rendered = arg
            for key, val in context.items():
                rendered = rendered.replace("{" + key + "}", str(val))
            rendered = self._expand_env_vars(rendered, env, agent_name)
            processed_args.append(rendered)

        return [agent_config.command] + processed_args

    def _expand_env_vars(self, value: str, env: dict[str, str], agent_name: str) -> str:
        missing_vars: set[str] = set()

        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1) or match.group(2)
            resolved = env.get(var_name)
            if resolved:
                return resolved
            missing_vars.add(var_name)
            return match.group(0)

        expanded = self._ENV_VAR_PATTERN.sub(_replace, value)
        if missing_vars:
            missing_list = ", ".join(sorted(missing_vars))
            raise ValueError(
                f"Missing environment variable(s) in args for agent '{agent_name}': {missing_list}"
            )
        return expanded

    def _build_env(self, agent_config: AgentConfig) -> dict[str, str]:
        env = os.environ.copy()
        for var_name in agent_config.env:
            env[var_name] = os.getenv(var_name, "")
        return env

    def _load_stdin_content(self, workspace_path: str, stdin_file: str) -> str:
        if not stdin_file:
            return ""

        stdin_file_path = os.path.join(workspace_path, stdin_file)
        if not os.path.exists(stdin_file_path):
            logger.warning(f"Stdin file {stdin_file_path} not found")
            return ""

        try:
            with open(stdin_file_path, "r") as f:
                content = f.read()
            logger.info(f"Read stdin content from {stdin_file_path}")
            return content
        except Exception as e:
            logger.error(f"Failed to read stdin file {stdin_file_path}: {e}")
            return ""

    def _read_stderr_content(self, workspace_path: str, agent_name: str) -> str:
        log_file_path = self._log_file_path(workspace_path, agent_name)
        if not os.path.exists(log_file_path):
            return ""

        try:
            with open(log_file_path, "r") as f:
                f.readline()
                return f.read()
        except Exception as e:
            logger.error(f"Failed to read stderr from {log_file_path}: {e}")
            return ""

    def _log_file_path(self, workspace_path: str, agent_name: str) -> str:
        return os.path.join(workspace_path, "log", f"{agent_name}.log")


# Backward-compatible alias for existing imports/usages.
AgentRunner = SubprocessAgentExecutionController
