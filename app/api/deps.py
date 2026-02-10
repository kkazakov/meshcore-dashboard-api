"""
Shared FastAPI dependencies.
"""

import logging

from fastapi import Header, HTTPException

from app.api.routes.auth import _token_store

logger = logging.getLogger(__name__)


def require_token(
    x_api_token: str | None = Header(default=None, alias="x-api-token"),
) -> str:
    """
    FastAPI dependency that validates the ``x-api-token`` request header
    against the in-memory token store populated by ``POST /api/login``.

    Returns the authenticated email on success.
    Raises **401** if the token is missing or unknown.
    """
    email = _token_store.get(x_api_token or "")
    if not email:
        logger.warning("Invalid or missing x-api-token")
        raise HTTPException(
            status_code=401,
            detail={"status": "unauthorized", "message": "Invalid or missing token"},
        )
    return email
