"""
Meshcore Dashboard — FastAPI application entry point.

Run with:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

import logging

from fastapi import FastAPI

from app.api.routes import auth as auth_router
from app.api.routes import channels as channels_router
from app.api.routes import status as status_router
from app.api.routes import telemetry as telemetry_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(
    title="Meshcore Dashboard API",
    description="REST API for the Meshcore Dashboard — telemetry, contacts and device status.",
    version="0.1.0",
)

# ── Routers ──────────────────────────────────────────────────────────────────
app.include_router(status_router.router, tags=["health"])
app.include_router(auth_router.router, tags=["auth"])
app.include_router(telemetry_router.router, tags=["telemetry"])
app.include_router(channels_router.router, tags=["messaging"])
