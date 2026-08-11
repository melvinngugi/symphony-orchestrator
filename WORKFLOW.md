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
      - "In Planning"
    transitions:
      on_start: "In Planning"
      success: 
        do:
          - action: "jira:attach_outputs"
        next: "Review Plan"
      blocked: "Clarification Needed"
  implement:
    agent: implementer
    states:
      - "In Progress"
    transitions:
      success: 
        next: "In Review"
        do:
          - action: "bitbucket:create-pull-request"
      blocked: "Clarification Needed"
  review:
    agent: reviewer
    states:
      - "In Review"
    transitions:
      success:
        next: "Done"
        do:
          - action: "bitbucket:publish-review-comment"
      blocked:
        next: "Clarification Needed"
        do:
          - action: "bitbucket:publish-review-comment"
---

# Symphony Workflow Definition

This configuration governs the multi-agent orchestration lifecycle for Jira tasks tagged with the "AI" label.
