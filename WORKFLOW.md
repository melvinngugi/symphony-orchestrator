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

project:
  jira:
    host: "https://myproject.atlassian.net"
    key: "MYPROJ"
    fields:
      business_value_score: "customfield_00000"
      business_value_rationale: "customfield_00001"
    backlog:
      ignore_label: "no-ai"
  bitbucket:
    workspace: "myusername"
    repository: "myrepo"
  confluence:
    host: "https://myproject.atlassian.net"
    strategy_pages:
      titles: []
      urls: []
      space_keys: []
      fail_on_missing: true
  business_value_parameters: "business_value_parameters.yaml"

# Enable after replacing the audit issue and custom-field IDs. Keep dry_run true
# for the first successful report and set it to false only after review.
scheduled_phases:
  backlog_curation:
    enabled: false
    agent: backlog_curator
    daily_at: "02:00"
    timezone: "Europe/Vienna"
    audit_issue: "MYPROJ-25"
    dry_run: true
    jira:
      clarification_label: "needs-clarification"
      review_label: "backlog-agent-review"
      dependency_link_type: "Blocks"
    confidence:
      business_value: 0.85
      dependency: 0.90
      clarification: 0.90
    transitions:
      success:
        do:
          - action: "jira:apply-backlog-curation"
          - action: "jira:attach-curation-outputs"
      blocked:
        do:
          - action: "jira:attach-curation-outputs"

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
