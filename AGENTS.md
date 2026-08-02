# AGENTS Design Decisions

## Executor-Orchestrator Encapsulation Boundary

- The agent executor (`app/services/agent.py`) is responsible for agent-output handling details.
- Structured output parsing, validation, and file extraction are encapsulated in the executor.
- The executor returns a normalized execution result with semantic status (`success` or `blocked`) and optional clarification details.
- The executor result also includes `message`, `needed_clarifications`, and `files` (list of returned file paths) so the orchestrator can forward these details to Jira integrations when needed.
- The orchestrator (`app/core/orchestrator.py`) consumes only the normalized result and must not read agent result files directly.
- Transition routing is phase-based (`phases.<phase>.transitions.<status>`) and applies to all successful process completions, regardless of structured or non-structured output mode.
- A phase may define `phases.<phase>.transitions.on_start`. This Jira transition is a hard pre-launch gate: the agent starts only after the transition succeeds; failure is recorded and leaves the issue eligible for a later polling retry. No start transition occurs when the property is absent.
- Agent selection is phase-based and driven by the issue's current Jira state. Each phase declares the Jira state names it accepts in `phases.<phase>.states`; polling uses the union of those states, and a matching phase supplies the agent for the issue. Tracker label filtering still applies.
- A completed phase is not followed by the next phase based on YAML ordering. The issue must appear in a Jira state configured for another phase before that phase is dispatched.
- Agent-specific file conventions or structured schema internals should not leak into orchestrator logic.
