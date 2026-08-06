from pydantic import BaseModel, Field, field_validator
from typing import Dict, Optional, List

class AgentConfig(BaseModel):
    command: str = Field(..., description="The executable command (e.g., 'codex')")
    args: List[str] = Field(default_factory=list, description="Command line arguments")
    stdin: str = Field(..., description="Input source for the agent (e.g., 'issue_json' or 'output_from:plan')")
    output_file: Optional[str] = Field(None, description="File where the agent's response should be saved")
    structured: Optional[str] = Field(None, description="Structured output JSON filename written by the agent")
    required_outputs: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Output filenames required for each semantic result status",
    )
    refresh_stdin: bool = Field(
        False,
        description="Fetch stdin from the configured provider before every execution",
    )
    sandbox: Optional[str] = Field("workspace-write", description="Sandbox security policy")
    env: List[str] = Field(default_factory=list, description="Environment variable names to pass through to the agent process")

    @field_validator("required_outputs")
    @classmethod
    def validate_required_outputs(cls, configured: Dict[str, List[str]]) -> Dict[str, List[str]]:
        for status, filenames in configured.items():
            if status not in {"success", "blocked"}:
                raise ValueError("required_outputs keys must be 'success' or 'blocked'")
            if not filenames or any(not name.strip() for name in filenames):
                raise ValueError(
                    f"required_outputs.{status} must contain non-empty filenames"
                )
            if len(filenames) != len(set(filenames)):
                raise ValueError(f"required_outputs.{status} contains duplicate filenames")
        return configured

class AgentsRegistry(BaseModel):
    agents: Dict[str, AgentConfig] = Field(..., description="Map of agent IDs to their configurations")
