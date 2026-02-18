"""
GET /status — server + dependency health check.
"""

import logging

from fastapi import APIRouter, Header
from pydantic import BaseModel

from app.db.clickhouse import get_client, ping

router = APIRouter()
logger = logging.getLogger(__name__)


class StatusResponse(BaseModel):
    status: str
    clickhouse: dict
    authenticated: bool


def _check_token_valid(token: str) -> bool:
    """Check if token exists, is not expired, and belongs to an active user."""
    try:
        client = get_client()
        result = client.query(
            "SELECT 1 FROM tokens t FINAL "
            "INNER JOIN (SELECT email FROM users FINAL WHERE active = true) u "
            "ON t.email = u.email "
            "WHERE t.token = {token:String} AND t.expires_at > now64() "
            "LIMIT 1",
            parameters={"token": token},
        )
        return len(result.result_rows) > 0
    except Exception as exc:
        logger.warning("Failed to check token in ClickHouse: %s", exc)
        return False


@router.get("/status", response_model=StatusResponse)
def get_status(
    x_api_token: str | None = Header(default=None),
) -> StatusResponse:
    """
    Returns the overall API status and individual dependency health.

    - **status**: ``"ok"`` if all dependencies are healthy, ``"degraded"`` otherwise.
    - **clickhouse**: connectivity result including latency in milliseconds.
    - **authenticated**: ``true`` if the ``x-api-token`` header matches a valid
      token stored in ClickHouse.
    """
    ch_ok, ch_latency_ms = ping()

    authenticated = bool(x_api_token and _check_token_valid(x_api_token))

    return StatusResponse(
        status="ok" if ch_ok else "degraded",
        clickhouse={
            "connected": ch_ok,
            "latency_ms": ch_latency_ms,
        },
        authenticated=authenticated,
    )
