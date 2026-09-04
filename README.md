# Symphony Orchestrator

Symphony Orchestrator is a custom multi-agent orchestration framework built from the ground up according to OpenAI's open-source specifications. It provides a lightweight, deterministic execution layer that bridges project management systems (Jira) with automated development environments and VCS platforms (Bitbucket).

By using intelligent, multi-stage agents, Symphony parses repository execution contracts, manages isolated sandboxes, executes code modifications, and handles PR workflows autonomously.

## Features

- **Specification-Driven Orchestration**: Implements OpenAI's architectural concepts to translate repository contracts into deterministic agent actions.
- **Jira Backlog Tracking Client**: Synchronizes with Jira Cloud APIs via JQL to identify, filter, and dispatch candidate issues tagged for AI execution.
- **Bitbucket VCS Integration**: Authenticates via scoped API tokens to clone repositories, isolate workspaces per ticket, check out feature branches, and submit Pull Requests.
- **Isolated Workspace Management**: Creates clean, isolated execution sandboxes (`<WORKSPACE_ROOT>/<TICKET-ID>`) for safe agent code generation and testing.
- **Multi-Stage Agent Pipeline**: Features a modular pipeline architecture (`symphony.agent`) to analyze codebase context, generate targeted patches, and verify changes before committing.
- **Core Domain Normalization**: Converts raw external vendor payloads (Jira/Bitbucket) into a unified, stable internal domain model.
- **Structured Agent Routing**: Agents can emit structured JSON results that drive workflow routing, artifact extraction, and blocked-queue handling.

## Workspace Layout

Each Jira issue gets an issue-level workspace for Symphony execution artifacts and
a nested Git checkout containing only repository files and task changes:

```text
<WORKSPACE_ROOT>/ISSUE-123/
├── issue.json
├── plan.md
├── implementer-result.json
├── log/
└── repository/
```

Agent processes run with `repository/` as their working directory. Agent stdin,
structured results, extracted output files, ordinary output files, and logs are
read from or written to the parent issue workspace, preventing Symphony execution
files from polluting the Git checkout.

## Structured Agent Output

An agent definition in `agents.yaml` may include a `structured` property with a non-empty filename (for example `structured: "planner-result.json"`).

When `structured` is set:

- The agent writes its result to that JSON file in the workspace using the schema contract from `agent-output-schema.json`.
- The orchestrator reads the file after agent completion.
- For either semantic status, each item in `outputs` is written to the workspace (`text` or base64 `binary`). An agent may declare filenames under `required_outputs.success` or `required_outputs.blocked`; a result missing a required file fails before workflow actions or Jira transitions run.
- If `status == "success"`, a later Jira poll dispatches the phase whose `states` contain the issue's new Jira state.
- If `status == "blocked"`, the issue is moved to the orchestrator blocked queue and follows the phase's blocked transition.

Phase-level Jira transitions for structured outcomes are configured in the selected workflow file under each phase:

```yaml
phases:
   plan:
      agent: planner
      states:
         - "To Do"
      transitions:
         on_start: "In Progress"
         success: "In Review"
         blocked: "Clarification Needed"
```

`transitions.on_start`, `transitions.success`, and `transitions.blocked` are optional. When `on_start` is configured, the Jira transition must succeed before the phase agent launches. A failed start transition records an orchestration error, does not launch the agent, and leaves the issue eligible for a later polling retry. No Jira comment is added for this transition. If an outcome transition is omitted, no Jira transition is attempted for that outcome.

Completion transitions also support an expanded form with ordered application actions:

```yaml
phases:
   implement:
      agent: implementer
      states:
         - "In Progress"
      transitions:
         success:
            next: "In Review"
            do:
               - action: "bitbucket:create-pull-request"
```

The existing string form remains supported when no actions are needed. Expanded
transitions are valid for `success` and `blocked`; `on_start` remains a Jira-state
string. Actions run in declaration order before the Jira comment and transition.
If an action fails, the completion remains pending and retries from that action on
the next poll. Completed actions are not repeated. A failed Jira transition is also
retried without repeating actions or posting the agent comment again.

The Bitbucket adapter's `bitbucket:create-pull-request` action stages and commits changes from
the issue's nested `repository/` checkout, pushes its current branch, and creates a
pull request to the Bitbucket default branch. An existing open pull request for the
same source and destination branches is reused. The configured API token therefore
needs repository write and pull-request permissions.

The `bitbucket:publish-review-comment` action publishes the normalized reviewer
message and required changes as Markdown on the existing pull request. A hidden
issue-and-commit marker makes transition retries update the same comment instead of
creating duplicates. Both passing and blocked reviews publish a readable PR result
before Jira is transitioned, so the configured API token must also be permitted to
read and write pull-request comments.

Action-providing adapters register their handlers in the application-owned action
registry during startup. The orchestrator receives that registry through a read-only
resolver interface and never changes registrations. Action names must be unique;
duplicate registrations fail startup. Every action receives one complete phase
result containing its issue, workspace, phase and agent metadata, configuration,
and normalized execution result.

The Jira adapter's `jira:attach_outputs` action uploads every output reported by the
completed phase from the issue workspace. Ordinary output filenames come from the
agent's `output_file`; structured outputs retain the names declared by the agent.
Files are sent together using Jira's attachment API before the comment and state
transition. A phase without outputs is a successful no-op. Failed uploads retry with
the pending action and may create duplicate same-name attachments if Jira processed
an earlier request whose response was lost.

Review feedback is not attached to Jira as `review.json`. Jira retains a concise
agent comment and the workflow state, while Bitbucket pull-request comments are the
durable human review record. When a ticket returns to `In Progress`, an executor-owned
input provider builds `implementation-context.json` from Jira's latest `plan.md` and
the active Bitbucket PR comments. Resolved comments and Symphony comments for older
source commits are excluded. This synthesized input is refreshed for every
implementation dispatch, so the implementer updates the existing issue branch using
the original plan plus current automated and human review feedback without receiving
Jira or Bitbucket credentials.

## Scheduled Backlog Curation

`scheduled_phases` runs backlog-wide agents independently of issue workflow
states. A daily schedule executes the latest due local-time window, including one
missed window after downtime. Completed run IDs are stored in
`WORKSPACE_ROOT/.scheduled-runs.json`, so the workspace volume must be persistent
in production. Scheduled agents use an artifact-only workspace as their working
directory and never clone the Bitbucket repository.

The shipped `backlog_curation` phase is disabled until its audit issue and Jira
custom-field IDs are configured. Enable it first with `dry_run: true`. Its input
provider pages through the configured JQL, excludes the audit issue and ignore
label, and optionally resolves exact Confluence page titles within configured
spaces. Configure these under `input.strategy_pages` with `titles`, `space_keys`,
and `fail_on_missing` (which defaults to `true`). Duplicate titles in different
configured spaces are all included. An empty title list disables Confluence
enrichment and does not require Confluence credentials. Jira and Confluence
credentials stay in the host process; the agent sees only normalized JSON.
Confluence content is marked as untrusted reference data.

The `backlog_curator` agent emits `backlog-curation.json`, validated against the
scope, source snapshot, evidence references, score weights, and domain schema
before any Jira write. `jira:apply-backlog-curation` then:

- applies only findings meeting the configured confidence threshold;
- refetches issue timestamps and routes stale findings to review;
- preserves human-edited value fields using a Jira issue-property provenance record;
- creates only missing, additive `Blocks` links and routes cycles to review;
- adds deduplicated clarification questions and configured labels; and
- writes `backlog-curation-report.json` with applied, proposed, skipped, and
  review-required operations.

`jira:attach-curation-outputs` gives audit attachments deterministic run-qualified
filenames and checks existing attachments before uploading, making action retries
idempotent. Scheduled completion transitions contain only `do`; they do not change
the audit issue's status. The Jira account needs Browse Projects, Link Issues, Edit
Issues, Add Comments, and Create Attachments permissions. Configure Confluence
credentials with view-only access to the configured strategy spaces. At startup,
enabled curator schedules resolve their configured spaces and titles; strict
lookup prevents scheduling when any title is missing. With `fail_on_missing:
false`, missing titles are logged and pages that were found remain available.

The read-only Confluence adapter also exposes generic
`fetch_documents_by_name(...)` and `fetch_documents_by_id(...)` batch APIs. ID
lookup preserves caller order while removing repeated IDs. Title lookup is
exact and case-sensitive, searches only the configured spaces, and returns all
matching pages (including same-title pages from different spaces) once per page
ID.

Each phase must define `states`, a list of Jira state names that trigger that phase. The orchestrator queries Jira using the union of all phase states, keeps applying `tracker.required_labels`, and chooses the first phase whose state list matches the issue state (case-insensitively). Phase order no longer advances execution by itself.

## Startup Workflow Validation

Before the orchestration thread starts, Symphony validates the workflow structure
and registered action names, then delegates state-name validation to the configured
tracker adapter. The Jira adapter requires phase `states`, `on_start`, and both
simple and expanded completion targets to match a status available to the configured
Jira project. Matching is case-insensitive. The adapter resolves `JIRA_PROJECT_KEY`
to its numeric project ID, then reads every page of Jira's status search API for that
project, including global statuses used by company-managed workflows. The Jira user
must have the Administer Projects permission for the project or the Administer Jira
global permission required by the status search API.

Validation loads the selected workflow and project statuses from Jira during every
startup. If a configured state name is unavailable, Symphony logs the incorrect
state and the available Jira states without a stack trace; the dashboard remains
available but the orchestrator
does not start. Other invalid workflow configuration, unavailable Jira credentials
or connectivity, and malformed or empty status responses remain fatal startup
errors. State existence is validated; whether a specific Jira transition is
reachable from a particular issue state remains a runtime concern.

## Tech Stack

- **Backend Framework**: Python 3.14+ / FastAPI (Uvicorn)
- **Data Validation & Settings**: Pydantic v2 & Pydantic Settings
- **Integrations & Operations**: PyYAML, Requests, Git, Python-dotenv

## Prerequisites

- Python 3.14+
- `git` CLI installed on the host system
- An active Jira Cloud workspace and Bitbucket repository

## Setup & Installation

1. **Clone the repository:**

   ```bash
   git clone git@github.com:melvinngugi/symphony-orchestrator.git
   cd symphony-orchestrator
   ```

2. **Set up a virtual environment:**

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

4. **Environment Configuration:**
   Create a .env file in the project root with your credentials:

   ```bash
   #Jira Configuration
   JIRA_HOST="https://your-domain.atlassian.net"
   JIRA_USER_EMAIL="your-email@example.com"
   JIRA_API_TOKEN="your-atlassian-api-token"
   JIRA_PROJECT_KEY="your-key"

   # Confluence strategy context (required only when strategy_pages.titles is non-empty)
   CONFLUENCE_HOST="https://your-domain.atlassian.net"
   CONFLUENCE_USER_EMAIL="your-email@example.com"
   CONFLUENCE_API_TOKEN="your-read-only-atlassian-api-token"

   # Bitbucket Configuration
   BITBUCKET_WORKSPACE="your-workspace"
   BITBUCKET_REPO_SLUG="your-repo"
   BITBUCKET_USER_EMAIL="your-email@domain.com"
   BITBUCKET_API_TOKEN="your-bitbucket-scoped-api-token"

   # Workspace Configuration
   WORKSPACE_ROOT="./workspaces"

   # Optional workflow definition (defaults to WORKFLOW.md)
   WORKFLOW_PATH="WORKFLOW.md"

   # Runtime timeout limits (seconds)
   AGENT_EXECUTION_TIMEOUT_SECONDS=3600
   AGENT_TERMINATION_GRACE_SECONDS=10
   HTTP_CONNECT_TIMEOUT_SECONDS=10
   HTTP_READ_TIMEOUT_SECONDS=60
   GIT_COMMAND_TIMEOUT_SECONDS=300
   ```

## Running the Orchestrator

Start the FastAPI application and background orchestrator daemon:

   ```bash
   python -m app.main
   ```

By default Symphony loads `WORKFLOW.md` from the process working directory. An
alternate Markdown workflow can be selected with `--workflow`:

```bash
python -m app.main --workflow WORKFLOW-deltaflow.md
```

It can also be selected through the environment, including when the FastAPI app
is launched directly through Uvicorn:

```bash
WORKFLOW_PATH=WORKFLOW-deltaflow.md python -m app.main
WORKFLOW_PATH=WORKFLOW-deltaflow.md uvicorn app.main:app
```

`--workflow` takes precedence over `WORKFLOW_PATH`; if neither is provided,
`WORKFLOW.md` is used. Relative paths are resolved from the process working
directory. The selected file must exist, be readable, and contain valid YAML
front matter or startup fails without falling back to the default.

Enable debug-level application logging with:

```bash
python -m app.main --debug
```

The orchestrator will continuously poll Jira for candidate issues (e.g., tickets with the AI label in To Do), set up an isolated Bitbucket workspace, and trigger the agent pipeline.

## Codex Usage Dashboard

The dashboard reads Codex usage limits from the supported local `codex app-server` interface. It uses the ChatGPT account already authenticated on the host and does not read or expose the account's OAuth credentials.

Confirm the host login before starting Symphony:

```bash
codex login status
```

The dashboard polls usage once per minute by default and displays each rate-limit window returned by OpenAI, including its used percentage and reset time. A previously successful snapshot is marked stale when it is more than three minutes old. These intervals can be configured with:

```bash
CODEX_USAGE_POLL_SECONDS=60
CODEX_USAGE_STALE_SECONDS=180
```

The `codex` executable must be available on `PATH`, and the Symphony process must use the same writable `CODEX_HOME` as the workflow agents. For a container deployment, provide the Codex CLI and mount the authenticated Codex home into the container; otherwise the dashboard safely reports usage as unavailable.

## Podman (Using uv)

Build the image:

```bash
podman build --format docker -t symphony-orchestrator:uv .
```

Docker image format is required here because Podman's default OCI format does
not retain the Dockerfile `HEALTHCHECK` instruction.

Run the container:

```bash
podman run --rm -p 8000:8000 --env-file .env \
  -v "${CODEX_HOME:-$HOME/.codex}:/root/.codex" \
  symphony-orchestrator:uv
```

The Codex home mounted at `/root/.codex` must contain a valid Codex login. The
image includes the Codex CLI, Git, `agents.yaml`, and the structured-output
schema required by the configured workflow agents.

To use an alternate workflow in the container, mount it read-only and set its
container path through `WORKFLOW_PATH`:

```bash
podman run --rm -p 8000:8000 --env-file .env \
  -e WORKFLOW_PATH=/config/custom-workflow.md \
  -v "${CODEX_HOME:-$HOME/.codex}:/root/.codex" \
  -v "$(pwd)/WORKFLOW-deltaflow.md:/config/custom-workflow.md:ro" \
  symphony-orchestrator:uv
```

The same mounted file can be selected with the CLI flag by overriding the image
command:

```bash
podman run --rm -p 8000:8000 --env-file .env \
  -v "${CODEX_HOME:-$HOME/.codex}:/root/.codex" \
  -v "$(pwd)/WORKFLOW-deltaflow.md:/config/custom-workflow.md:ro" \
  symphony-orchestrator:uv \
  python -m app.main --workflow /config/custom-workflow.md
```

Liveness and readiness checks:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

`/health` returns success whenever the HTTP process is responsive and does not
contact Jira or Bitbucket. `/ready` returns HTTP 200 only after workflow
validation succeeds and while the orchestrator thread is running; otherwise it
returns HTTP 503 with a non-sensitive reason code. The container health check
uses `/ready`.

If you need workspace persistence for orchestration artifacts, mount the local workspace directory:

```bash
podman run --rm -p 8000:8000 --env-file .env \
  -v "${CODEX_HOME:-$HOME/.codex}:/root/.codex" \
  -v "$(pwd)/workspaces:/app/workspaces" \
  symphony-orchestrator:uv
```

## CI/CD & Container Registry

This project uses GitHub Actions for automated testing and container image publishing to the **GitHub Container Registry (GHCR)**.

### Publish Triggers

- **Push to `main`**: Builds and pushes the image with the commit SHA and the `latest` tag.
- **Version Tags (`v*`)**: Builds and pushes the image with the specific version tag (e.g., `v1.0.0`).
- **Manual**: Can be triggered manually via the "Actions" tab in GitHub.
- **Pull Requests**: Triggers a validation build and runs tests, but does **not** push to the registry.

### Registry Details

- **Image Path**: `ghcr.io/<owner>/symphony-orchestrator`
- **Authentication**: Authentication is handled automatically in the CI pipeline using `GITHUB_TOKEN`.
- **Quality Gate**: Every publish job is gated by `pytest`. If tests fail, the image will not be pushed.

### Deployment Note

Ensure that runtime secrets (Jira, Bitbucket, etc.) are injected into the container environment at deployment time. The published images do not contain these credentials.
