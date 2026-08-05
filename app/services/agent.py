import subprocess
import logging
import os
import re
import json
import base64
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
    repository_path: str


@dataclass
class RunningAgentExecution:
    agent_name: str
    workspace_path: str
    repository_path: str
    output_file: Optional[str]
    structured_output_file: Optional[str]
    process: subprocess.Popen


@dataclass
class AgentExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    status: str = "success"
    message: str = ""
    needed_clarifications: list[str] | None = None
    files: list[str] | None = None


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
        full_command = self._build_command(
            request.agent_config,
            env,
            request.agent_name,
            request.workspace_path,
        )
        stdin_content = self._load_stdin_content(request.workspace_path, request.agent_config.stdin)

        logger.info(f"Spawning agent with command: {' '.join(full_command)}")

        log_file_path = self._log_file_path(request.workspace_path, request.agent_name)
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        with open(log_file_path, "w") as f:
            f.write(f"{' '.join(full_command)}\n")

        stderr_file = open(log_file_path, "a")
        process = subprocess.Popen(
            full_command,
            cwd=request.repository_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            env=env,
        )
        stderr_file.close()

        if process.stdin:
            if stdin_content:
                process.stdin.write(stdin_content)
            process.stdin.close()

        return RunningAgentExecution(
            agent_name=request.agent_name,
            workspace_path=request.workspace_path,
            repository_path=request.repository_path,
            output_file=request.agent_config.output_file,
            structured_output_file=request.agent_config.structured,
            process=process,
        )

    def poll_execution(self, execution: RunningAgentExecution) -> Optional[AgentExecutionResult]:
        exit_code = execution.process.poll()
        if exit_code is None:
            return None

        stdout_content, _ = execution.process.communicate()
        stderr_content = self._read_stderr_content(execution.workspace_path, execution.agent_name)

        status = "success"
        message = ""
        needed_clarifications: list[str] = []
        files: list[str] = []

        if exit_code == 0:
            try:
                status, message, needed_clarifications, files = self._handle_success_output(execution, stdout_content)
            except Exception as e:
                logger.error(f"Structured output handling failed for agent {execution.agent_name}: {e}")
                return AgentExecutionResult(
                    exit_code=1,
                    stdout=stdout_content,
                    stderr=f"{stderr_content}\nStructured output handling failed: {e}".strip(),
                    status="failed",
                    files=[],
                )

        return AgentExecutionResult(
            exit_code=exit_code,
            stdout=stdout_content,
            stderr=stderr_content,
            status=status,
            message=message,
            needed_clarifications=needed_clarifications,
            files=files,
        )

    def _handle_success_output(self, execution: RunningAgentExecution, stdout_content: str) -> tuple[str, str, list[str], list[str]]:
        structured_file = (execution.structured_output_file or "").strip()
        if structured_file:
            payload = self._load_structured_result(execution.workspace_path, structured_file)
            status = payload.get("status")

            if status == "success":
                outputs = payload.get("outputs")
                if not isinstance(outputs, list):
                    raise ValueError("Structured result with status 'success' must contain an 'outputs' array")

                file_names: list[str] = []
                for item in outputs:
                    if isinstance(item, dict):
                        name = item.get("name")
                        if isinstance(name, str) and name.strip():
                            file_names.append(name)
                    self._write_structured_output_file(execution.workspace_path, item)

                return (
                    "success",
                    payload.get("message", "") if isinstance(payload.get("message"), str) else "",
                    [],
                    file_names,
                )

            if status == "blocked":
                message = payload.get("message") if isinstance(payload.get("message"), str) else "Blocked by structured agent result"
                clarifications = payload.get("neededClarifications")
                if not isinstance(clarifications, list):
                    clarifications = []
                return "blocked", message, [str(v) for v in clarifications], []

            raise ValueError(f"Unsupported structured status '{status}'. Expected 'success' or 'blocked'.")

        if execution.output_file:
            output_path = self._resolve_workspace_path(execution.workspace_path, execution.output_file)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w") as f:
                f.write(stdout_content)
            logger.info(f"Wrote agent output to {output_path}")
            return "success", "", [], [execution.output_file]

        return "success", "", [], []

    def _load_structured_result(self, workspace_path: str, structured_file: str) -> dict:
        result_path = self._resolve_workspace_path(workspace_path, structured_file)
        if not os.path.exists(result_path):
            raise FileNotFoundError(f"Structured output file not found: {result_path}")

        with open(result_path, "r") as f:
            try:
                payload = json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid structured JSON in {result_path}: {e}") from e

        if not isinstance(payload, dict):
            raise ValueError("Structured output must be a JSON object")

        status = payload.get("status")
        if not isinstance(status, str):
            raise ValueError("Structured output is missing required string field 'status'")

        return payload

    def _write_structured_output_file(self, workspace_path: str, output_item: dict):
        if not isinstance(output_item, dict):
            raise ValueError("Each structured output item must be an object")

        name = output_item.get("name")
        content = output_item.get("content")
        content_type = output_item.get("contentType")

        if not isinstance(name, str) or not name.strip():
            raise ValueError("Structured output item is missing required string field 'name'")
        if not isinstance(content, str):
            raise ValueError("Structured output item is missing required string field 'content'")
        if content_type not in ("text", "binary"):
            raise ValueError("Structured output item must use contentType 'text' or 'binary'")

        output_path = self._resolve_workspace_path(workspace_path, name)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        if content_type == "text":
            with open(output_path, "w") as f:
                f.write(content)
            logger.info(f"Wrote agent output file to {output_path}")
            return

        try:
            binary_content = base64.b64decode(content, validate=True)
        except Exception as e:
            raise ValueError(f"Invalid base64 binary content for structured output '{name}': {e}") from e

        with open(output_path, "wb") as f:
            f.write(binary_content)
        logger.info(f"Wrote agent output file to {output_path}")

    def _resolve_workspace_path(self, workspace_path: str, relative_path: str) -> str:
        workspace_abs = os.path.abspath(workspace_path)
        target_abs = os.path.abspath(os.path.join(workspace_abs, relative_path))

        if target_abs != workspace_abs and not target_abs.startswith(workspace_abs + os.sep):
            raise ValueError(f"Path escapes workspace root: {relative_path}")

        return target_abs

    def _build_command(
        self,
        agent_config: AgentConfig,
        env: dict[str, str],
        agent_name: str,
        workspace_path: str,
    ) -> list[str]:
        context = {
            "output_file": self._artifact_argument(workspace_path, agent_config.output_file),
            "structured": self._artifact_argument(workspace_path, agent_config.structured),
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

    def _artifact_argument(self, workspace_path: str, configured_path: Optional[str]) -> str:
        if not configured_path:
            return ""
        return self._resolve_workspace_path(workspace_path, configured_path)

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

        stdin_file_path = self._resolve_workspace_path(workspace_path, stdin_file)
        if not os.path.exists(stdin_file_path):
            raise FileNotFoundError(f"Agent stdin file not found: {stdin_file_path}")

        try:
            with open(stdin_file_path, "r") as f:
                content = f.read()
            logger.info(f"Read stdin content from {stdin_file_path}")
            return content
        except OSError as e:
            logger.error(f"Failed to read stdin file {stdin_file_path}: {e}")
            raise

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
