"""
Meshcore Dashboard — FastAPI application entry point.

Run with:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import auth as auth_router
from app.api.routes import channels as channels_router
from app.api.routes import messages as messages_router
from app.api.routes import repeaters as repeaters_router
from app.api.routes import status as status_router
from app.api.routes import telemetry as telemetry_router
from app.api.routes import websocket as websocket_router
from app.events import broadcast_task, stop_broadcast_task
from app.workers.message_poller import run_message_poller
from app.workers.repeater_telemetry_poller import run_repeater_telemetry_poller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


class _MeshcoreNoiseFilter(logging.Filter):
    """Drop known high-frequency INFO lines emitted by the meshcore library.

    MeshCore resets its own logger level to INFO on every connection, so
    setLevel(WARNING) is overwritten each poll cycle.  A filter on the root
    handler is the only reliable way to suppress these without modifying the
    library.
    """

    _SUPPRESSED = frozenset(
        [
            "TCP Connection started",
            "TCP Connection closed",
            "connection established",
            "Connected successfully:",
        ]
    )

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name == "meshcore" and record.getMessage() in self._SUPPRESSED
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start background workers on startup; cancel them on shutdown."""
    # Attach a filter to every root handler to suppress meshcore connection
    # chatter.  We can't use setLevel() because MeshCore resets its own logger
    # to INFO on every instantiation.
    _filter = _MeshcoreNoiseFilter()
    for handler in logging.root.handlers:
        handler.addFilter(_filter)

    poller_task = asyncio.create_task(run_message_poller(), name="message_poller")
    repeater_poller_task = asyncio.create_task(
        run_repeater_telemetry_poller(), name="repeater_telemetry_poller"
    )
    broadcast_task_handle = asyncio.create_task(broadcast_task(), name="broadcast_task")
    logger.info("Background workers started")
    try:
        yield
    finally:
        poller_task.cancel()
        repeater_poller_task.cancel()
        broadcast_task_handle.cancel()
        try:
            await poller_task
        except asyncio.CancelledError:
            pass
        try:
            await repeater_poller_task
        except asyncio.CancelledError:
            pass
        try:
            await broadcast_task_handle
        except asyncio.CancelledError:
            pass
        await stop_broadcast_task()
        logger.info("Background workers stopped")


app = FastAPI(
    title="Meshcore Dashboard API",
    description="REST API for the Meshcore Dashboard — telemetry, contacts and device status.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────────────────────────
app.include_router(status_router.router, tags=["health"])
app.include_router(auth_router.router, tags=["auth"])
app.include_router(telemetry_router.router, tags=["telemetry"])
app.include_router(channels_router.router, tags=["messaging"])
app.include_router(messages_router.router, tags=["messaging"])
app.include_router(repeaters_router.router, tags=["repeaters"])
app.include_router(websocket_router.router, tags=["websocket"])
