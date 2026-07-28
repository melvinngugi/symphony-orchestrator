---
polling:
  interval_ms: 30000

agent:
  max_concurrent_agents: 1

tracker:
  active_states:
    - "To Do"
  terminal_states:
    - "Done"
  required_labels:
    - "AI"

phases:
  plan:
    agent: planner
  implement:
    agent: implementer
  validate:
    agent: tester
---

# Symphony Workflow Definition

This configuration governs the multi-agent orchestration lifecycle for Jira tasks tagged with the "AI" label.
