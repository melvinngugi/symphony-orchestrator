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

workflow_stages:
  - plan
  - human_review_plan
  - implement
  - validate_test
  - pull_request
  - agent_review
  - ready
  - human_review_merge
---

# Symphony Workflow Definition
This configuration governs the multi-agent orchestration lifecycle for Jira tasks tagged with the "AI" label.