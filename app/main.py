import logging
import asyncio
import threading
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
import uvicorn

from app.core.config import load_config
from app.core.orchestrator import SymphonyOrchestrator

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("symphony.main")

# Initialize App & Templates
app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

# Global reference to orchestrator for the dashboard
global_orchestrator = None

@app.on_event("startup")
async def startup_event():
    global global_orchestrator
    config = load_config("WORKFLOW.md")
    global_orchestrator = SymphonyOrchestrator(config)
    
    # Run the orchestrator loop in a separate thread so it doesn't block the dashboard API
    daemon_thread = threading.Thread(target=global_orchestrator.start, daemon=True)
    daemon_thread.start()

@app.get("/")
async def dashboard(request: Request):
    active_count = len(global_orchestrator.state.running) if global_orchestrator else 0
    claimed_count = len(global_orchestrator.state.claimed) if global_orchestrator else 0
    
    return templates.TemplateResponse(
        request, 
        "dashboard.html", 
        {
            "active_count": active_count,
            "claimed_count": claimed_count
        }
    )

if __name__ == "__main__":
    logger.info("Starting Symphony Server...")
    # Execute this file to start both the background daemon and the UI on port 8000
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)