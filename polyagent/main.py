"""FastAPI application entry-point for PolyAgent.

Provides:
- Async lifespan manager (DB initialization, background agent startup/shutdown)
- API router inclusion (REST and WebSockets)
- CORS middleware
- Static-file mounting for the premium dashboard SPA
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from polyagent import __version__
from polyagent.config import get_settings
from polyagent.database import close_db, init_db
from polyagent.api.routes import router as api_router
from polyagent.api.websocket import router as ws_router

# Import agents for lifespan registration
from polyagent.agents.scanner import ScannerAgent
from polyagent.agents.analyst import AnalystAgent
from polyagent.agents.arbitrage import ArbitrageAgent
from polyagent.agents.risk_manager import RiskManagerAgent
from polyagent.agents.executor import ExecutorAgent

logger = logging.getLogger(__name__)

# List to track running agent instances
_running_agents = []


# ── Lifespan ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown resources.

    Startup:
      1. Load settings and log trading mode.
      2. Initialise the database (create tables).
      3. Initialize and start all background agents.

    Shutdown:
      1. Stop all background agents gracefully.
      2. Dispose of the database engine.
    """
    settings = get_settings()
    logger.info("🚀 PolyAgent v%s starting in %s mode", __version__, settings.mode.value)

    # 1. Database
    await init_db()
    logger.info("✅ Database initialised")

    # 2. Start Agents
    global _running_agents
    try:
        # Instantiate the agents
        scanner = ScannerAgent(settings)
        analyst = AnalystAgent(settings)
        arbitrage = ArbitrageAgent(settings)
        risk_manager = RiskManagerAgent(settings)
        executor = ExecutorAgent(settings)

        _running_agents = [risk_manager, executor, scanner, arbitrage, analyst]

        # Start agents in background tasks
        for agent in _running_agents:
            await agent.start()
            logger.info("✅ Started agent: %s", agent.name)
            
        app.state.agents = _running_agents
    except Exception as e:
        logger.critical("⚠️ Failed to start agents: %s", e, exc_info=True)

    yield  # ── application is running ──

    # Shutdown
    logger.info("🛑 Shutting down background agents...")
    for agent in _running_agents:
        try:
            await agent.stop()
            logger.info("🛑 Stopped agent: %s", agent.name)
        except Exception as e:
            logger.error("Error stopping agent %s: %s", agent.name, e)

    await close_db()
    logger.info("🛑 Database connection closed")


# ── Application factory ──────────────────────────────────────────

app = FastAPI(
    title="PolyAgent",
    description="Multi-agent Polymarket trading backend – signals, execution, risk management.",
    version=__version__,
    lifespan=lifespan,
)

# ── CORS (allow the local dashboard during development) ──────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Routes ───────────────────────────────────────────────────
app.include_router(api_router)
app.include_router(ws_router)
logger.info("✅ Include REST and WebSocket API routes")

# ── Static files (dashboard SPA) ─────────────────────────────────
_DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
if _DASHBOARD_DIR.is_dir():
    app.mount("/dashboard", StaticFiles(directory=str(_DASHBOARD_DIR), html=True), name="dashboard")
    logger.info("Dashboard mounted from %s", _DASHBOARD_DIR)
else:
    logger.info("No dashboard directory found at %s – skipping static mount", _DASHBOARD_DIR)


# ── Health check ─────────────────────────────────────────────────

@app.get("/health", tags=["system"])
async def health_check() -> JSONResponse:
    """Lightweight liveness / readiness probe.

    Returns the application version, current trading mode, and whether
    Polymarket credentials are configured.
    """
    settings = get_settings()
    return JSONResponse(
        content={
            "status": "ok",
            "version": __version__,
            "mode": settings.mode.value,
            "poly_credentials_configured": settings.has_poly_credentials,
        }
    )


# ── Convenience runner ───────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "polyagent.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
