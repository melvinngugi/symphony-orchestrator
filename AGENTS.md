# AGENTS Design Decisions

## Executor-Orchestrator Encapsulation Boundary

- The agent executor (`app/services/agent.py`) is responsible for agent-output handling details.
- Structured output parsing, validation, and file extraction are encapsulated in the executor.
- The executor returns a normalized execution result with semantic status (`success` or `blocked`) and optional clarification details.
- The executor result also includes `message`, `needed_clarifications`, and `files` (list of returned file paths) so the orchestrator can forward these details to Jira integrations when needed.
- The orchestrator (`app/core/orchestrator.py`) consumes only the normalized result and must not read agent result files directly.
- Transition routing is phase-based (`phases.<phase>.transitions.<status>`) and applies to all successful process completions, regardless of structured or non-structured output mode.
- Agent-specific file conventions or structured schema internals should not leak into orchestrator logic.
