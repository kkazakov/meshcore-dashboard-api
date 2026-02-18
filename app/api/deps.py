"""
Shared FastAPI dependencies.
"""

import logging
from datetime import datetime, timezone

from fastapi import Header, HTTPException

from app.db.clickhouse import get_client

logger = logging.getLogger(__name__)

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_CACHE_TTL_SECONDS = 60


def _get_email_from_cache(token: str) -> str | None:
    now = datetime.now(timezone.utc).timestamp()
    cached = _TOKEN_CACHE.get(token)
    if cached and cached[1] > now:
        return cached[0]
    return None


def _cache_token(token: str, email: str) -> None:
    expires = datetime.now(timezone.utc).timestamp() + _CACHE_TTL_SECONDS
    _TOKEN_CACHE[token] = (email, expires)


def require_token(
    x_api_token: str | None = Header(default=None, alias="x-api-token"),
) -> str:
    """
    FastAPI dependency that validates the ``x-api-token`` request header
    against the tokens table in ClickHouse.

    Uses a short-lived in-memory cache to reduce database load.
    Returns the authenticated email on success.
    Raises **401** if the token is missing or unknown.
    """
    if not x_api_token:
        logger.warning("Missing x-api-token header")
        raise HTTPException(
            status_code=401,
            detail={"status": "unauthorized", "message": "Invalid or missing token"},
        )

    cached_email = _get_email_from_cache(x_api_token)
    if cached_email:
        return cached_email

    try:
        client = get_client()
        result = client.query(
            "SELECT t.email FROM tokens t FINAL "
            "INNER JOIN (SELECT email FROM users FINAL WHERE active = true) u "
            "ON t.email = u.email "
            "WHERE t.token = {token:String} AND t.expires_at > now64() "
            "LIMIT 1",
            parameters={"token": x_api_token},
        )
        rows = result.result_rows
        if rows:
            email = rows[0][0]
            _cache_token(x_api_token, email)
            return email
    except Exception as exc:
        logger.error("Failed to validate token in ClickHouse: %s", exc)

    logger.warning("Invalid or expired x-api-token")
    raise HTTPException(
        status_code=401,
        detail={"status": "unauthorized", "message": "Invalid or missing token"},
    )
