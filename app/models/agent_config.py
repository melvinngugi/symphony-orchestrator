from pydantic import BaseModel, Field
from typing import Dict, Optional, List

class AgentConfig(BaseModel):
    command: str = Field(..., description="The executable command (e.g., 'codex')")
    args: List[str] = Field(default_factory=list, description="Command line arguments")
    stdin: str = Field(..., description="Input source for the agent (e.g., 'issue_json' or 'output_from:plan')")
    output_file: Optional[str] = Field(None, description="File where the agent's response should be saved")
    sandbox: Optional[str] = Field("workspace-write", description="Sandbox security policy")
    env: Dict[str, str] = Field(default_factory=dict, description="Environment variables for the agent")

class AgentsRegistry(BaseModel):
    agents: Dict[str, AgentConfig] = Field(..., description="Map of agent IDs to their configurations")
