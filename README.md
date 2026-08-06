# Symphony Orchestrator

Symphony Orchestrator is a custom multi-agent orchestration framework built from the ground up according to OpenAI's open-source specifications. It provides a lightweight, deterministic execution layer that bridges project management systems (Jira) with automated development environments and VCS platforms (Bitbucket).

By using intelligent, multi-stage agents, Symphony parses repository execution contracts, manages isolated sandboxes, executes code modifications, and handles PR workflows autonomously.

## Features

- **Specification-Driven Orchestration**: Implements OpenAI's architectural concepts to translate repository contracts into deterministic agent actions.
- **Jira Backlog Tracking Client**: Synchronizes with Jira Cloud APIs via JQL to identify, filter, and dispatch candidate issues tagged for AI execution.
- **Bitbucket VCS Integration**: Authenticates via scoped API tokens to clone repositories, isolate workspaces per ticket, check out feature branches, and submit Pull Requests.
- **Isolated Workspace Management**: Creates clean, isolated execution sandboxes (`/tmp/symphony_workspaces/<TICKET-ID>`) for safe agent code generation and testing.
- **Multi-Stage Agent Pipeline**: Features a modular pipeline architecture (`symphony.agent`) to analyze codebase context, generate targeted patches, and verify changes before committing.
- **Core Domain Normalization**: Converts raw external vendor payloads (Jira/Bitbucket) into a unified, stable internal domain model.
- **Structured Agent Routing**: Agents can emit structured JSON results that drive workflow routing, artifact extraction, and blocked-queue handling.

## Workspace Layout

Each Jira issue gets an issue-level workspace for Symphony execution artifacts and
a nested Git checkout containing only repository files and task changes:

```text
/tmp/symphony_workspaces/ISSUE-123/
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

Phase-level Jira transitions for structured outcomes are configured in `WORKFLOW.md` under each phase:

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

The configured review-blocked transition attaches `review.json` before moving the
ticket to `Clarification Needed`. When the ticket later returns to `In Progress`,
the Jira input provider builds `implementation-context.json` from the latest
`plan.md` and `review.json` attachments. The implementer therefore updates the
existing issue branch using the original plan plus the newest review findings.
This synthesized input is refreshed for every implementation dispatch so repeated
review and remediation cycles cannot reuse stale feedback.

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

Validation loads project statuses from Jira during every startup. If a configured
state name is unavailable, Symphony logs the incorrect state and the available Jira
states without a stack trace; the dashboard remains available but the orchestrator
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

   # Bitbucket Configuration
   BITBUCKET_WORKSPACE="your-workspace"
   BITBUCKET_REPO_SLUG="your-repo"
   BITBUCKET_USER_EMAIL="your-email@domain.com"
   BITBUCKET_API_TOKEN="your-bitbucket-scoped-api-token"

## Running the Orchestrator

Start the FastAPI application and background orchestrator daemon:

   ```bash
   python -m app.main
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
podman build -t symphony-orchestrator:uv .
```

Run the container:

```bash
podman run --rm -p 8000:8000 --env-file .env symphony-orchestrator:uv
```

Health check:

```bash
curl http://localhost:8000/health
```

If you need workspace persistence for orchestration artifacts, mount the local workspace directory:

```bash
podman run --rm -p 8000:8000 --env-file .env \
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
