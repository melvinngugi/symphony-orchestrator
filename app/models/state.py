from dataclasses import dataclass, field
from typing import Dict, Any, Set, List, Optional
from datetime import datetime

@dataclass
class TicketMetadata:
    identifier: str
    title: str
    started_at: float

@dataclass
class ErrorDetail:
    message: str
    timestamp: str

@dataclass
class OrchestratorState:
    poll_interval_ms: int = 30000
    max_concurrent_agents: int = 10
    running: Dict[str, Any] = field(default_factory=dict)
    claimed: Dict[str, TicketMetadata] = field(default_factory=dict)
    completed: Set[str] = field(default_factory=set)
    errors: List[ErrorDetail] = field(default_factory=list)
    codex_totals: Dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0})