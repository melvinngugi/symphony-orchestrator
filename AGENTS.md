# AGENTS Design Decisions

## Executor-Orchestrator Encapsulation Boundary

- The agent executor (`app/services/agent.py`) is responsible for agent-output handling details.
- Structured output parsing, validation, and file extraction are encapsulated in the executor.
- The executor returns a normalized execution result with semantic status (`success` or `blocked`) and optional clarification details.
- The executor result also includes `message`, `needed_clarifications`, and `files` (list of returned file paths). The orchestrator wraps it with issue, workspace, phase, agent, and configuration context in an immutable `PhaseResult` for transition actions.
- The orchestrator (`app/core/orchestrator.py`) consumes only the normalized result and must not read agent result files directly.
- Transition routing is phase-based (`phases.<phase>.transitions.<status>`) and applies to all successful process completions, regardless of structured or non-structured output mode.
- A phase may define `phases.<phase>.transitions.on_start`. This tracker transition is a hard pre-launch gate: the agent starts only after the transition succeeds; failure is recorded and leaves the issue eligible for a later polling retry. No start transition occurs when the property is absent.
- Agent selection is phase-based and driven by the issue's current tracker state. Each phase declares the state names it accepts in `phases.<phase>.states`; polling uses the union of those states, and a matching phase supplies the agent for the issue. Tracker label filtering still applies.
- A completed phase is not followed by the next phase based on YAML ordering. The issue must appear in a tracker state configured for another phase before that phase is dispatched.
- Agent-specific file conventions or structured schema internals should not leak into orchestrator logic.

## Orchestrator-Tracker Encapsulation Boundary

- The orchestrator depends only on the `TrackerAdapter` protocol and must not import, construct, or branch on a concrete tracker implementation.
- The application composition root selects and injects the production tracker adapter. Jira is the current production adapter.
- Tracker-specific authentication, API calls, status discovery, and workflow-state-name validation belong to the tracker adapter.
- Workflow structure, completion-transition normalization, and registered action-name validation remain tracker-neutral responsibilities outside the adapter.
- Workflow state strings are opaque to the orchestrator. The adapter determines whether configured state references are valid for its backing tracker.
- New tracker integrations must implement `TrackerAdapter` without adding tracker-specific behavior to the orchestrator.

## Composition Root-Action Registry Boundary

- The application composition root constructs the mutable `ActionRegistry`, asks action-providing adapters to register their handlers, and injects the completed registry into the orchestrator.
- Adapters own their action names and bound handlers. They register directly and must not preflight names or implement collision handling.
- `ActionRegistry` is the sole authority for action-name normalization and uniqueness. Duplicate registrations are fatal and never overwrite an existing handler.
- The orchestrator depends only on the read-only `ActionResolver` interface. It may validate, resolve, and execute configured actions but must never register or replace them.
- Workflow action validation remains tracker-neutral and also depends only on `ActionResolver`.
- There is one action interface: `Action = Callable[[PhaseResult], None]`. Adapters must not introduce alternate action signatures or argument-specific compatibility wrappers.
- Pending transitions retain the complete `PhaseResult`, and every retry of an unfinished action receives that same result object.
