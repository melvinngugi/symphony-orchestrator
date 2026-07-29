from pydantic import BaseModel, Field
from typing import Dict, Optional, List

class AgentConfig(BaseModel):
    command: str = Field(..., description="The executable command (e.g., 'codex')")
    args: List[str] = Field(default_factory=list, description="Command line arguments")
    stdin: str = Field(..., description="Input source for the agent (e.g., 'issue_json' or 'output_from:plan')")
    output_file: Optional[str] = Field(None, description="File where the agent's response should be saved")
    structured: Optional[str] = Field(None, description="Structured output JSON filename written by the agent")
    sandbox: Optional[str] = Field("workspace-write", description="Sandbox security policy")
    env: List[str] = Field(default_factory=list, description="Environment variable names to pass through to the agent process")

class AgentsRegistry(BaseModel):
    agents: Dict[str, AgentConfig] = Field(..., description="Map of agent IDs to their configurations")
