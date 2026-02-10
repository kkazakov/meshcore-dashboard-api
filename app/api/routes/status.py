"""
GET /status — server + dependency health check.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.db.clickhouse import ping

router = APIRouter()


class StatusResponse(BaseModel):
    status: str  # "ok" | "degraded"
    clickhouse: dict


@router.get("/status", response_model=StatusResponse)
def get_status() -> StatusResponse:
    """
    Returns the overall API status and individual dependency health.

    - **status**: ``"ok"`` if all dependencies are healthy, ``"degraded"`` otherwise.
    - **clickhouse**: connectivity result including latency in milliseconds.
    """
    ch_ok, ch_latency_ms = ping()

    return StatusResponse(
        status="ok" if ch_ok else "degraded",
        clickhouse={
            "connected": ch_ok,
            "latency_ms": ch_latency_ms,
        },
    )
