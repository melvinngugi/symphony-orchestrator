---
polling:
  interval_ms: 30000

agent:
  max_concurrent_agents: 1

tracker:
  terminal_states:
    - "Done"
  required_labels:
    - "AI"

phases:
  plan:
    agent: planner
    states:
      - "To Do"
    transitions:
      success: "In Review"
      blocked: "Clarification Needed"
  implement:
    agent: implementer
    states:
      - "In Progress"
    transitions:
      success: "In Review"
      blocked: "Clarification Needed"
  validate:
    agent: tester
    states:
      - "In Review"
    transitions:
      success: "Done"
      blocked: "Clarification Needed"
---

# Symphony Workflow Definition

This configuration governs the multi-agent orchestration lifecycle for Jira tasks tagged with the "AI" label.
