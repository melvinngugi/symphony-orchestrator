import argparse
import logging
import asyncio
import threading
import os
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Sequence
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from app.core.config import WorkflowConfigLoadError, load_config, settings
from app.core.orchestrator import SymphonyOrchestrator
from app.core.workflow_validation import (
    WorkflowStateValidationError,
    WorkflowValidationError,
)
from app.services.actions import ActionRegistry
from app.services.agent import (
    FallbackAgentInputProvider,
    ImplementationContextInputProvider,
    SubprocessAgentExecutionController,
)
from app.services.bitbucket import BitbucketService
from app.services.jira import JiraClient
from app.services.backlog import BacklogCurationInputProvider, fetch_strategy_documents
from app.services.confluence import ConfluenceClient
from app.models.usage import UsageSnapshot
from app.services.usage import CodexUsageCollector

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("symphony.main")

# Global reference to orchestrator for the dashboard
global_orchestrator = None
global_usage_collector = None
global_orchestrator_thread = None
global_readiness_error = None
_workflow_path: Path | None = None


def resolve_workflow_path(cli_path: str | None = None) -> Path:
    """Resolve the workflow selected by CLI, environment, or the default."""
    selected_path = cli_path
    if selected_path is None:
        selected_path = os.getenv("WORKFLOW_PATH") or "WORKFLOW.md"
    return Path(selected_path).expanduser().resolve()


def configure_workflow_path(cli_path: str | None = None) -> Path:
    """Select and retain the workflow path for this process."""
    global _workflow_path
    _workflow_path = resolve_workflow_path(cli_path)
    return _workflow_path


def get_workflow_path() -> Path:
    """Return the process workflow path, resolving environment defaults once."""
    if _workflow_path is None:
        return configure_workflow_path()
    return _workflow_path


def ensure_symphony_home() -> None:
    current_value = os.getenv("SYMPHONY_HOME", "")
    if current_value:
        return

    symphony_home = str(Path(__file__).resolve().parents[1])
    os.environ["SYMPHONY_HOME"] = symphony_home
    logger.info(f"SYMPHONY_HOME not set; defaulting to {symphony_home}")


def create_scheduled_document_providers(
    enabled_schedules: list[tuple[str, dict]],
) -> dict[str, ConfluenceClient]:
    providers: dict[str, ConfluenceClient] = {}
    for phase_name, phase in enabled_schedules:
        strategy_pages = phase["input"]["strategy_pages"]
        titles = strategy_pages.get("titles", [])
        urls = strategy_pages.get("urls", [])
        if not titles and not urls:
            continue
        provider = ConfluenceClient(
            space_keys=strategy_pages["space_keys"],
            fail_on_missing_documents=strategy_pages.get("fail_on_missing", True),
        )
        fetch_strategy_documents(provider, strategy_pages)
        providers[phase_name] = provider
    return providers


@asynccontextmanager
async def lifespan(_: FastAPI):
    global global_orchestrator, global_usage_collector
    global global_orchestrator_thread, global_readiness_error
    global_orchestrator = None
    global_usage_collector = None
    global_orchestrator_thread = None
    global_readiness_error = None
    ensure_symphony_home()
    workflow_path = get_workflow_path()
    logger.info("Loading workflow from %s", workflow_path)
    try:
        config = load_config(str(workflow_path))
    except WorkflowConfigLoadError as exc:
        logger.error("Workflow loading failed: %s", exc)
        raise
    try:
        tracker = JiraClient()
        bitbucket = BitbucketService()
        action_registry = ActionRegistry()
        tracker.register_actions(action_registry)
        bitbucket.register_actions(action_registry)
        implementation_context_provider = ImplementationContextInputProvider(
            plan_provider=tracker,
            review_provider=bitbucket,
        )
        orchestrator_kwargs = dict(
            tracker=tracker,
            bitbucket_service=bitbucket,
            action_registry=action_registry,
            execution_controller=SubprocessAgentExecutionController(
                input_provider=FallbackAgentInputProvider(
                    (implementation_context_provider, tracker, bitbucket)
                ),
                execution_timeout_seconds=settings.AGENT_EXECUTION_TIMEOUT_SECONDS,
                termination_grace_seconds=settings.AGENT_TERMINATION_GRACE_SECONDS,
            ),
        )
        enabled_schedules = [
            (phase_name, phase)
            for phase_name, phase in config.get("scheduled_phases", {}).items()
            if isinstance(phase, dict) and phase.get("enabled", True) is not False
        ]
        document_providers = {}
        if enabled_schedules:
            orchestrator_kwargs["scheduled_input_provider"] = BacklogCurationInputProvider(
                tracker,
                document_providers,
            )
        orchestrator = SymphonyOrchestrator(config, **orchestrator_kwargs)
        document_providers.update(
            create_scheduled_document_providers(enabled_schedules)
        )
        global_orchestrator = orchestrator
    except WorkflowStateValidationError as exc:
        global_orchestrator = None
        global_usage_collector = None
        global_orchestrator_thread = None
        global_readiness_error = "workflow_state_validation_failed"
        logger.error("%s", exc)
        yield
        return
    except WorkflowValidationError as exc:
        logger.error("Workflow validation failed: %s", exc)
        raise
    
    global_usage_collector = CodexUsageCollector(
        poll_interval_seconds=settings.CODEX_USAGE_POLL_SECONDS,
        stale_after_seconds=settings.CODEX_USAGE_STALE_SECONDS,
    )

    # Run the usage collector loop in a separate thread so it doesn't block the dashboard API
    usage_thread = threading.Thread(target=global_usage_collector.run, daemon=True, name="Usage Collector")
    usage_thread.start()

    # Run the orchestrator loop in a separate thread so it doesn't block the dashboard API
    global_orchestrator_thread = threading.Thread(
        target=global_orchestrator.start,
        daemon=True,
        name="Orchestrator",
    )
    global_orchestrator_thread.start()

    try:
        yield
    finally:
        global_usage_collector.stop()

# Initialize App & Templates
app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")


@app.get("/health")
async def health():
    """Liveness check that has no external-service dependencies."""
    return {"status": "ok"}


@app.get("/ready")
async def readiness():
    """Report whether validated orchestration is initialized and running."""
    if global_readiness_error:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": global_readiness_error},
        )
    if global_orchestrator is None:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "orchestrator_not_initialized"},
        )
    if global_orchestrator_thread is None or not global_orchestrator_thread.is_alive():
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "orchestrator_thread_not_running"},
        )
    return {"status": "ready"}


@app.get("/")
async def dashboard(request: Request):
    running_tickets = []
    claimed_tickets = []
    errors = []
    active_count = 0
    claimed_count = 0
    blocked_tickets = []
    blocked_count = 0
    usage = UsageSnapshot()

    if global_orchestrator:
        # running[issue_id] = {"handle": process, "metadata": TicketMetadata}
        running_tickets = [v["metadata"] for v in global_orchestrator.state.running.values()]
        claimed_tickets = list(global_orchestrator.state.claimed.values())
        errors = global_orchestrator.state.errors
        active_count = len(global_orchestrator.state.running)
        claimed_count = len(global_orchestrator.state.claimed)
        blocked_tickets = list(global_orchestrator.state.blocked.values())
        blocked_count = len(global_orchestrator.state.blocked)

    if global_usage_collector:
        usage = global_usage_collector.snapshot()
    
    return templates.TemplateResponse(
        request, 
        "dashboard.html", 
        {
            "active_count": active_count,
            "claimed_count": claimed_count,
            "blocked_count": blocked_count,
            "running_tickets": running_tickets,
            "blocked_tickets": blocked_tickets,
            "claimed_tickets": claimed_tickets,
            "errors": errors,
            "usage": usage,
        }
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Symphony orchestrator")
    parser.add_argument(
        "--workflow",
        metavar="PATH",
        help="workflow Markdown file (overrides WORKFLOW_PATH)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable debug logging",
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    configure_workflow_path(args.workflow)
    logger.info("Starting Symphony Server...")
    ensure_symphony_home()
    # Execute this file to start both the background daemon and the UI on port 8000
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    run()
