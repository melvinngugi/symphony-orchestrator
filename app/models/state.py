from dataclasses import dataclass, field
from typing import Dict, Any, Set

@dataclass
class OrchestratorState:
    poll_interval_ms: int = 30000
    max_concurrent_agents: int = 10
    running: Dict[str, Any] = field(default_factory=dict)
    claimed: Set[str] = field(default_factory=set)
    completed: Set[str] = field(default_factory=set)
    codex_totals: Dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0})