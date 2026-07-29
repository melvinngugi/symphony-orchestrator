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
    transitions:
      success: "In Progress"
      blocked: "Clarification Needed"
  implement:
    agent: implementer
    transitions:
      success: "In Progress"
      blocked: "Clarification Needed"
  validate:
    agent: tester
    transitions:
      success: "Done"
      blocked: "Clarification Needed"
---

# Symphony Workflow Definition

This configuration governs the multi-agent orchestration lifecycle for Jira tasks tagged with the "AI" label.
