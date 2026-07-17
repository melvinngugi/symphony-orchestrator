# Symphony Orchestrator

Symphony Orchestrator is a custom multi-agent orchestration framework, built from the ground up according to OpenAI's open-source specifications. The project is focused on providing a lightweight, deterministic execution layer that bridges high-level project management systems with automated development environments. By shifting the complexity away from brittle, hardcoded pipelines, this framework implements intelligent agents to parse repository execution contracts, manage isolated workspaces, and execute development tasks autonomously.

## Features

- **Specification-Driven Orchestration**: Implements OpenAI's architectural concepts to translate repository contracts into deterministic agent actions.
- **Jira Backlog Tracking Client**: Synchronizes with Jira Cloud APIs using modern JQL queries to fetch candidate issues from specified active states (`To Do`, `In Progress`).
- **Data Normalization**: Transforms raw vendor API payloads into a stable, internal Core Domain Model.
- **Isolated Workspace Management**: Designed to execute secure tasks within deterministic development sandboxes.

## Tech Stack

- **Backend Framework**: Python 3.14+ / FastAPI
- **Data Validation & Settings**: Pydantic v2 & Pydantic Settings
- **Configuration & Integration**: PyYAML, Requests, Python-dotenv

## Prerequisites

Ensure you have Python 3.14+ installed. It is recommended to run the application inside an isolated environment (such as an Arch Linux Distrobox container).

## Setup & Installation

1. **Clone the repository:**
   ```bash
   git clone git@github.com:melvinngugi/symphony-orchestrator.git
   cd symphony-orchestrator

2. **Set up a virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt

4. **Environment Variables:**
   Create a .env file in the root directory and configure your credentials.
   ```bash
   JIRA_HOST="https://your-domain.atlassian.net"
   JIRA_USER_EMAIL="your-email@example.com"
   JIRA_API_TOKEN="your-atlassian-api-token"
   JIRA_PROJECT_KEY="your-key"
