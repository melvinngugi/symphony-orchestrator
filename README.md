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
