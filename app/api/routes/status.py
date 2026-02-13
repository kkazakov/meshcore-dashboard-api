"""
GET /status — server + dependency health check.
"""

from fastapi import APIRouter, Header
from pydantic import BaseModel

from app.api.routes.auth import _token_store
from app.db.clickhouse import ping

router = APIRouter()


class StatusResponse(BaseModel):
    status: str  # "ok" | "degraded"
    clickhouse: dict
    authenticated: bool


@router.get("/status", response_model=StatusResponse)
def get_status(
    x_api_token: str | None = Header(default=None),
) -> StatusResponse:
    """
    Returns the overall API status and individual dependency health.

    - **status**: ``"ok"`` if all dependencies are healthy, ``"degraded"`` otherwise.
    - **clickhouse**: connectivity result including latency in milliseconds.
    - **authenticated**: ``true`` if the ``x-api-token`` header matches a valid
      in-memory session token issued by ``POST /api/login``.
    """
    ch_ok, ch_latency_ms = ping()

    authenticated = bool(x_api_token and x_api_token in _token_store)

    return StatusResponse(
        status="ok" if ch_ok else "degraded",
        clickhouse={
            "connected": ch_ok,
            "latency_ms": ch_latency_ms,
        },
        authenticated=authenticated,
    )
