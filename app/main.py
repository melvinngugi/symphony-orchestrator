import logging
import asyncio
import threading
import os
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
import uvicorn

from app.core.config import load_config
from app.core.orchestrator import SymphonyOrchestrator

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("symphony.main")

# Global reference to orchestrator for the dashboard
global_orchestrator = None


def ensure_symphony_home() -> None:
    current_value = os.getenv("SYMPHONY_HOME", "")
    if current_value:
        return

    symphony_home = str(Path(__file__).resolve().parents[1])
    os.environ["SYMPHONY_HOME"] = symphony_home
    logger.info(f"SYMPHONY_HOME not set; defaulting to {symphony_home}")

@asynccontextmanager
async def lifespan(_: FastAPI):
    global global_orchestrator
    ensure_symphony_home()
    config = load_config("WORKFLOW.md")
    global_orchestrator = SymphonyOrchestrator(config)

    # Run the orchestrator loop in a separate thread so it doesn't block the dashboard API
    daemon_thread = threading.Thread(target=global_orchestrator.start, daemon=True)
    daemon_thread.start()

    yield

# Initialize App & Templates
app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")

@app.get("/")
async def dashboard(request: Request):
    running_tickets = []
    claimed_tickets = []
    errors = []
    active_count = 0
    claimed_count = 0
    blocked_tickets = []
    blocked_count = 0

    if global_orchestrator:
        # running[issue_id] = {"handle": process, "metadata": TicketMetadata}
        running_tickets = [v["metadata"] for v in global_orchestrator.state.running.values()]
        claimed_tickets = list(global_orchestrator.state.claimed.values())
        errors = global_orchestrator.state.errors
        active_count = len(global_orchestrator.state.running)
        claimed_count = len(global_orchestrator.state.claimed)
        blocked_tickets = list(global_orchestrator.state.blocked.values())
        blocked_count = len(global_orchestrator.state.blocked)
    
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
            "errors": errors
        }
    )

if __name__ == "__main__":
    logger.info("Starting Symphony Server...")
    ensure_symphony_home()
    # Execute this file to start both the background daemon and the UI on port 8000
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)