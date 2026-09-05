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

# Enable after replacing the audit issue and custom-field IDs. Keep dry_run true
# for the first successful report and set it to false only after review.
scheduled_phases:
  backlog_curation:
    enabled: false
    agent: backlog_curator
    daily_at: "02:00"
    timezone: "Europe/Vienna"
    audit_issue: "PROJECT-CURATION"
    dry_run: true
    input:
      jql: ""
      ignore_label: "backlog-curation-ignore"
      strategy_pages:
        titles: []
        urls: []
        space_keys: []
        fail_on_missing: true
      scoring_weights:
        customerImpact: 0.35
        revenueOrCostImpact: 0.25
        strategicAlignment: 0.25
        riskReduction: 0.15
    jira:
      business_value_score_field: "customfield_00000"
      business_value_rationale_field: "customfield_00001"
      epic_field: ""
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
