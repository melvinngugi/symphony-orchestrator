from dataclasses import dataclass, field
from typing import Dict, Any, Set, List, Optional, TYPE_CHECKING
from datetime import datetime

if TYPE_CHECKING:
    from app.services.actions import PhaseResult

@dataclass
class TicketMetadata:
    identifier: str
    title: str
    started_at: float
    current_phase: Optional[str] = None
    workspace_path: Optional[str] = None

@dataclass
class ErrorDetail:
    message: str
    timestamp: str

@dataclass
class BlockedTicketDetail:
    identifier: str
    title: str
    current_phase: Optional[str]
    blocked_at: str
    message: str
    needed_clarifications: List[str] = field(default_factory=list)

@dataclass
class PendingTransitionDetail:
    phase_result: "PhaseResult"
    target_state: str
    actions: List[str] = field(default_factory=list)
    next_action_index: int = 0
    comment_attempted: bool = False

@dataclass
class OrchestratorState:
    poll_interval_ms: int = 30000
    max_concurrent_agents: int = 10
    running: Dict[str, Any] = field(default_factory=dict)
    claimed: Dict[str, TicketMetadata] = field(default_factory=dict)
    completed: Set[str] = field(default_factory=set)
    blocked: Dict[str, BlockedTicketDetail] = field(default_factory=dict)
    pending_transitions: Dict[str, PendingTransitionDetail] = field(default_factory=dict)
    errors: List[ErrorDetail] = field(default_factory=list)
    codex_totals: Dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0})
