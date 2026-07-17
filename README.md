# Symphony Orchestrator

Symphony Orchestrator is a custom multi-agent orchestration application designed to bridge project management workflows with automated development cycles. By integrating directly with **Jira** and **Bitbucket**, Symphony acts as an intelligent execution layer that tracks backlog states, parses repository execution contracts, and manages autonomous agent workspaces.

## Features

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
