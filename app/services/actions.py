from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol

from app.models.agent_config import AgentConfig
from app.services.agent import AgentExecutionResult


@dataclass(frozen=True)
class PhaseResult:
    issue: dict
    workspace_path: str
    repository_path: str
    phase_name: str
    agent_name: str
    agent_config: AgentConfig
    execution: AgentExecutionResult


Action = Callable[[PhaseResult], None]


class ActionResolver(Protocol):
    """Read-only transition-action lookup used by orchestration code."""

    def resolve(self, name: str) -> Action:
        ...

    def contains(self, name: str) -> bool:
        ...


@dataclass(frozen=True)
class CompletionTransition:
    next_state: str
    actions: tuple[str, ...] = ()


class ActionRegistry:
    def __init__(self, actions: Optional[Iterable[tuple[str, Action]]] = None):
        self._actions: dict[str, Action] = {}
        for name, handler in actions or ():
            self.register(name, handler)

    def register(self, name: str, handler: Action) -> None:
        normalized_name = name.strip() if isinstance(name, str) else ""
        if not normalized_name:
            raise ValueError("Action name must be a non-empty string")
        if not callable(handler):
            raise ValueError(f"Action handler for '{normalized_name}' must be callable")
        if normalized_name in self._actions:
            raise ValueError(f"Transition action '{normalized_name}' is already registered")
        self._actions[normalized_name] = handler

    def resolve(self, name: str) -> Action:
        try:
            return self._actions[name]
        except KeyError as exc:
            raise ValueError(f"Unknown transition action '{name}'") from exc

    def contains(self, name: str) -> bool:
        return name in self._actions
